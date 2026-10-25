# -*- coding: utf-8 -*-
"""
Resolver: replays closed 15m Binance candles for every unresolved plan file and
records what happened to each leg. No Claude, no TradingView.

Legs (all on the same alert):
- tv              the /tv plan (only when /tv ran)
- tv_market       what-if: market entry when /tv issued its plan, /tv's SL/TP
- scanner         the scanner's own levels, entered as the scanner says (LIMIT_ZONE = limit)
- scanner_market  what-if: market entry when the alert is seen, the scanner's own SL/TP
                  (STALE_LEVELS when price is already past the scanner's TP1)
- rule_atr        no-analysis baseline: market, SL 1.5x / TP1 1.5x / TP2 3x 15m ATR
- rule_wide       same with wide stops: SL 3x / TP1 3x / TP2 6x ATR (~2% stops, fees a small share of R)

Rules (agreed):
- Same candle touches SL and a target -> SL counts (conservative).
- On the candle a limit fills, only the SL is checked (we can't know the order inside it).
- Result = TP1 when TP1 is reached first; after that we keep watching TP2 vs SL ("after_tp1").
- Open trades time out 72h after entry, closed at the next candle open.
- Not entered by valid_until -> EXPIRED. 15m close beyond cancel_if before entry -> CANCELLED.
- A limit not yet filled when price touches TP1 -> CANCELLED (the move left without us).
- Price already at/through TP1 on the first candle after issue -> STALE_LEVELS (obsolete plan).

Each run replays from the start (stateless), so results are reproducible.
Usage: python3 resolver.py [--plan PATH] [--verbose]
"""
import argparse
import os
import sys
from pathlib import Path

from common import (LEG_FINAL, PLANS_DIR, add_hours, atr14, klines_15m_before, direction_from_bias, fmt_utc, from_ms, iter_plan_paths,
                    klines_15m, load_json, parse_utc, plan_is_final, save_json, to_ms, utcnow)

TIMEOUT_H = 72
# fixed-rule baseline (no Claude): market entry in the scanner's direction, stop/targets in 15m ATR
RULE_SL_ATR = float(os.environ.get("TV_RULE_SL_ATR", "1.5"))
RULE_TP1_ATR = float(os.environ.get("TV_RULE_TP1_ATR", "1.5"))   # = 1R
RULE_TP2_ATR = float(os.environ.get("TV_RULE_TP2_ATR", "3.0"))   # = 2R
# wide-stop variant: about 2% stops on typical alts, so the 0.10% fee is ~5% of R instead of ~12%
RULE_WIDE_SL_ATR = float(os.environ.get("TV_RULE_WIDE_SL_ATR", "3.0"))
RULE_WIDE_TP1_ATR = float(os.environ.get("TV_RULE_WIDE_TP1_ATR", "3.0"))   # = 1R
RULE_WIDE_TP2_ATR = float(os.environ.get("TV_RULE_WIDE_TP2_ATR", "6.0"))   # = 2R
DEFAULT_VALID_H = 24


# ------------------------------------------------------------------ leg specs

def spec_from_tv(tv: dict, start) -> dict:
    """Normalise a /tv plan into a leg spec."""
    setup = tv.get("setup")
    entry = tv.get("entry") or {}
    etype = entry.get("type")
    if not setup:
        setup = {"market_now": "TAKE_NOW", "limit": "PLACE_LIMIT"}.get(etype, "WAIT_FOR_TRIGGER")
    valid = parse_utc(tv.get("valid_until_utc")) or add_hours(start, DEFAULT_VALID_H)
    valid = min(valid, add_hours(start, DEFAULT_VALID_H))
    return {
        "direction": tv.get("direction"), "setup": setup, "entry_type": etype,
        "entry": entry.get("price"), "sl": tv.get("sl"), "tp1": tv.get("tp1"), "tp2": tv.get("tp2"),
        "trigger": tv.get("trigger"), "cancel_if": tv.get("cancel_if"),
        "start": start, "valid_until": valid,
    }


def spec_from_scanner(sp: dict, start) -> dict:
    limit = "LIMIT" in str(sp.get("entry_type") or "").upper()
    return {
        "direction": sp.get("direction"), "setup": "PLACE_LIMIT" if limit else "TAKE_NOW",
        "entry_type": "limit" if limit else "market_now",
        "entry": sp.get("entry"), "sl": sp.get("sl"), "tp1": sp.get("tp1"), "tp2": sp.get("tp2"),
        "trigger": None, "cancel_if": None,
        "start": start, "valid_until": add_hours(start, DEFAULT_VALID_H),
    }


# ------------------------------------------------------------------ replay

def _closes_beyond(c: dict, cond: dict | None) -> bool:
    if not cond or cond.get("price") is None:
        return False
    p = float(cond["price"])
    return c["c"] > p if cond.get("type") == "close_above" else c["c"] < p


def replay(spec: dict, candles: list[dict]) -> dict:
    """Pure function: leg spec + closed 15m candles (ascending, open >= start) -> leg result."""
    long = spec.get("direction") == "long"
    sign = 1 if long else -1
    out = {"state": None, "final": False, "filled_at": None, "fill_price": None,
           "result": None, "r": None, "after_tp1": None, "closed_at": None, "note": None}

    if spec.get("direction") not in ("long", "short") or spec.get("sl") is None or spec.get("tp1") is None:
        out.update(state="NO_LEVELS", final=True, note="missing direction/sl/tp1")
        return out
    sl, tp1 = float(spec["sl"]), float(spec["tp1"])
    tp2 = float(spec["tp2"]) if spec.get("tp2") is not None else None

    def bad_levels(entry):
        return (entry - sl) * sign <= 0 or (tp1 - entry) * sign <= 0

    setup = spec["setup"]
    entry = float(spec["entry"]) if spec.get("entry") is not None else None
    if setup == "TAKE_NOW":
        state = "ENTER_AT_OPEN"
    elif setup == "PLACE_LIMIT":
        state = "PENDING"
    else:
        state = "WAITING"
        if not spec.get("trigger") or spec["trigger"].get("price") is None:
            out.update(state="INVALID", final=True, note="WAIT_FOR_TRIGGER without trigger price")
            return out
    if state == "PENDING" and (entry is None or bad_levels(entry)):
        out.update(state="INVALID", final=True, note="limit entry missing or on wrong side of SL/TP1")
        return out

    valid_ms = to_ms(spec["valid_until"])
    fill_ms = risk = None
    tp1_hit = False

    def touched(c, level, above):  # above=True: high reached level
        return c["h"] >= level if above else c["l"] <= level

    def sl_touched(c):
        return touched(c, sl, above=not long)

    def finish(state_, closed_ms, **kw):
        out.update(state=state_, final=True, closed_at=fmt_utc(from_ms(closed_ms)), **kw)

    if candles and (candles[0]["o"] - tp1) * sign >= 0:
        # price was already at/through TP1 when the plan was issued: the levels were obsolete
        c0 = candles[0]
        out.update(state="STALE_LEVELS", final=True, closed_at=fmt_utc(from_ms(c0["t"])),
                   note=f"price {c0['o']} already past TP1 {tp1} when issued")
        return out

    for c in candles:
        # ---------------- not yet entered
        if state in ("WAITING", "PENDING") and c["t"] >= valid_ms:
            finish("EXPIRED", valid_ms)
            return out

        if state == "ENTER_AT_OPEN":
            entry = c["o"]
            if bad_levels(entry):
                out.update(state="INVALID", final=True, note=f"market fill {entry} on wrong side of SL/TP1")
                return out
            state, fill_ms, risk = "OPEN", c["t"], abs(entry - sl)
            out.update(filled_at=fmt_utc(from_ms(c["t"])), fill_price=entry)
            # fall through: this candle counts for the open trade

        elif state == "WAITING":
            if _closes_beyond(c, spec.get("cancel_if")):
                finish("CANCELLED", c["ct"], note="cancel_if hit before the trigger")
                return out
            if _closes_beyond(c, spec["trigger"]):
                if spec.get("entry_type") == "limit":
                    if entry is None or bad_levels(entry):
                        out.update(state="INVALID", final=True, note="post-trigger limit on wrong side")
                        return out
                    state = "PENDING"
                else:  # market_on_trigger: enter at the trigger close
                    entry = c["c"]
                    if bad_levels(entry):
                        out.update(state="INVALID", final=True, note=f"trigger close {entry} beyond SL/TP1")
                        return out
                    state, fill_ms, risk = "OPEN", c["ct"], abs(entry - sl)
                    out.update(filled_at=fmt_utc(from_ms(c["ct"])), fill_price=entry)
            continue

        elif state == "PENDING":
            if touched(c, entry, above=not long):  # long limit fills when low <= entry
                state, fill_ms, risk = "OPEN", c["t"], abs(entry - sl)
                out.update(filled_at=fmt_utc(from_ms(c["t"])), fill_price=entry)
                if sl_touched(c):  # only SL is checked on the fill candle
                    finish("LOSS", c["ct"], result="LOSS", r=-1.0)
                    return out
                continue
            if touched(c, tp1, above=long):  # moved to TP1 without us: plan is void
                finish("CANCELLED", c["ct"], note="TP1 reached before the limit filled")
                return out
            if _closes_beyond(c, spec.get("cancel_if")):
                finish("CANCELLED", c["ct"], note="cancel_if hit before the limit filled")
                return out
            continue

        # ---------------- open trade
        if c["t"] >= fill_ms + TIMEOUT_H * 3600 * 1000:
            r_now = round((c["o"] - entry) * sign / risk, 3)
            if tp1_hit:
                finish("TP1", c["t"], after_tp1="TIMEOUT")
            else:
                finish("TIMEOUT", c["t"], result="TIMEOUT", r=r_now)
            return out
        if not tp1_hit:
            if sl_touched(c):
                finish("LOSS", c["ct"], result="LOSS", r=-1.0)
                return out
            if touched(c, tp1, above=long):
                tp1_hit = True
                out.update(result="TP1", r=round(abs(tp1 - entry) / risk, 3))
                if tp2 is None:
                    finish("TP1", c["ct"])
                    return out
                if touched(c, tp2, above=long):
                    finish("TP2", c["ct"], after_tp1="TP2")
                    return out
        else:
            if sl_touched(c):
                finish("TP1", c["ct"], after_tp1="SL")
                return out
            if touched(c, tp2, above=long):
                finish("TP2", c["ct"], after_tp1="TP2")
                return out

    # ran out of closed candles: still live
    out["state"] = {"ENTER_AT_OPEN": "PENDING"}.get(state, state)
    if state == "OPEN":
        out["state"] = "OPEN_AFTER_TP1" if tp1_hit else "OPEN"
    return out


# ------------------------------------------------------------------ driver

def leg_specs(plan: dict) -> dict:
    specs = {}
    tv = plan.get("tv")
    if tv:
        start = parse_utc(plan.get("created_at_utc"))
        specs["tv"] = spec_from_tv(tv, start)
        # what-if: enter at market when the plan is issued, with /tv's own SL and targets
        m = dict(specs["tv"], setup="TAKE_NOW", entry_type="market_now", entry=None,
                 trigger=None, cancel_if=None)
        specs["tv_market"] = m
    src = plan.get("source") or {}
    direction = direction_from_bias(src.get("trade_bias"))
    # all market legs start together: when /tv issued its plan if it ran, else the scanner's observation time
    mstart = parse_utc(plan.get("created_at_utc")) if tv else parse_utc(src.get("observed_at"))
    if direction and mstart:
        for name, mult in (("rule_atr", (RULE_SL_ATR, RULE_TP1_ATR, RULE_TP2_ATR)),
                           ("rule_wide", (RULE_WIDE_SL_ATR, RULE_WIDE_TP1_ATR, RULE_WIDE_TP2_ATR))):
            specs[name] = {"direction": direction, "setup": "TAKE_NOW", "entry_type": "market_now",
                           "entry": None, "sl": None, "tp1": None, "tp2": None, "trigger": None,
                           "cancel_if": None, "start": mstart, "valid_until": add_hours(mstart, DEFAULT_VALID_H),
                           "atr_mult": mult}
    sp = plan.get("scanner_plan")
    if sp:
        start = parse_utc(plan["source"]["observed_at"])
        specs["scanner"] = spec_from_scanner(sp, start)
        if mstart:
            # what-if: market entry when the alert is seen, with the scanner's own SL/TP
            specs["scanner_market"] = dict(spec_from_scanner(sp, mstart), setup="TAKE_NOW",
                                           entry_type="market_now", entry=None,
                                           direction=sp.get("direction") or direction)
    return specs


def resolve_plan(path, verbose=False, recompute=False) -> bool:
    plan = load_json(path)
    if plan.get("final") and not recompute:
        return False
    symbol = plan["source"]["symbol"]
    legs = plan.setdefault("legs", {})
    specs = leg_specs(plan)
    if not specs:
        if not plan.get("final"):
            plan["final"] = True
            save_json(path, plan)
        return False
    changed = False
    candle_cache, atr_cache = {}, {}  # legs sharing a start time share one download
    for name, spec in specs.items():
        if legs.get(name, {}).get("final") and not recompute:
            continue
        start = spec["start"]
        if start not in candle_cache:
            candle_cache[start] = klines_15m(symbol, start, limit=1000)
        candles = candle_cache[start]
        extra = {}
        mult = spec.get("atr_mult")
        if mult:
            if not candles:
                continue  # first candle after start not closed yet
            if start not in atr_cache:
                atr_cache[start] = atr14(klines_15m_before(symbol, start))
            atr = atr_cache[start]
            if not atr:
                continue
            sl_m, tp1_m, tp2_m = mult
            e, s = candles[0]["o"], (1 if spec["direction"] == "long" else -1)
            spec = dict(spec, sl=e - s * sl_m * atr, tp1=e + s * tp1_m * atr, tp2=e + s * tp2_m * atr)
            extra = {"atr": round(atr, 10), "levels": {"sl": spec["sl"], "tp1": spec["tp1"], "tp2": spec["tp2"]}}
        res = replay(spec, candles)
        res.update(extra)
        res["checked_at"] = fmt_utc(utcnow())
        if res != legs.get(name):
            legs[name] = res
            changed = True
        if verbose:
            print(f"{path.name} {name}: {res['state']} result={res['result']} r={res['r']} after_tp1={res['after_tp1']}")
    if changed:
        plan["final"] = plan_is_final(plan)
        save_json(path, plan)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="resolve one plan file only")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--recompute", action="store_true", help="re-resolve finished plans too (after rule changes)")
    a = ap.parse_args()
    paths = [Path(a.plan)] if a.plan else iter_plan_paths()
    errors, last_error = 0, None
    for p in paths:
        try:
            resolve_plan(p, a.verbose, a.recompute)
        except Exception as e:
            errors += 1
            last_error = f"{p.name}: {e}"
            print(f"[resolver] {last_error}", file=sys.stderr)
    if not a.plan:
        save_json(PLANS_DIR / ".resolver_state.json",
                  {"last_run_at": fmt_utc(utcnow()), "plans_checked": len(paths),
                   "errors": errors, "last_error": last_error})
    sys.exit(1 if errors and errors == len(paths) else 0)


if __name__ == "__main__":
    main()
