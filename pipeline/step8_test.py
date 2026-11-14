# -*- coding: utf-8 -*-
"""
Step-8 forward test (from 2026-11-09): the scanner's own ACTIONABLE signals, now that Step 8 keeps
watching a zone after Step 7 drops it (scanner zone_watch.py), with three stop rules side by side on
the SAME signals. Rules fixed in advance, before any of these trades existed:

  step8_s0  the scanner as designed: limit at the zone edge, scanner stop (zone + 0.15 x 15m ATR)
  step8_s1  same limit, stop zone far side + 0.5 x ATR of the zone's own timeframe (1h/4h)
  step8_s2  market entry at the signal (confirmation already happened); stop beyond the sweep
            extreme of the last 16 15m candles + 0.25 x 15m ATR

All three target the scanner's TP1/TP2. Limits are valid 24h (same as the scanner leg); trades time
out 72h after entry (resolver). Pass mark per rule, same as before: FORWARD_N resolved trades,
>= FORWARD_MIN_R after fees, >= FORWARD_MIN_LONGS longs.

Shadow group (from 2026-11-14, information only): a complete Step-8 sequence that the scanner rejected
ONLY for the 15m volume trigger and/or the RR minimum (scanner field shadow_sequence). Same three
legs, plan id ..._S8X, plan["shadow"] = True, no Telegram message. It exists to measure whether those
two gates remove losers or winners; it has no pass mark and can never become a tradeable rule without
a fresh test of its own.

Pure functions except new_actionable_in_run / new_shadow_in_run (scanner db, read-only) and plan
bookkeeping on disk.
"""
import json
import os

from common import (FEE_ROUNDTRIP_PCT, LEVERAGE, add_hours, direction_from_bias, fmt_px, fmt_utc, iter_plan_paths,
                    load_json, parse_utc, safe_leverage, stop_pct, utcnow)

ENABLED = os.environ.get("TV_STEP8_TEST", "1").strip() != "0"
TELEGRAM = os.environ.get("TV_STEP8_TELEGRAM", "1").strip() != "0"
FROM = os.environ.get("TV_STEP8_FROM", "2026-11-09 00:00")
KIND = "step8"
VALID_H = 24
LIVE_WINDOW_H = 96  # a signal blocks a repeat of the same symbol+bias at most this long
LEGS = ("step8_s0", "step8_s1", "step8_s2")
LABELS = {"step8_s0": "S0 scanner stop", "step8_s1": "S1 zone-ATR stop", "step8_s2": "S2 after sweep"}
SL_FIELD = {"step8_s0": "sl_s0", "step8_s1": "sl_s1", "step8_s2": "sl_s2"}

OBS_FIELDS = ("id, scan_run_id, observed_at, symbol, primary_state, trade_bias, signal_grade, "
              "overall_score, alignment_score, macro_context, entry_type, entry_price, stop_loss, "
              "target_1, target_2, risk_reward, structure_15m_confirmed, raw_record_json")
SOURCE_KEYS = ("id", "scan_run_id", "observed_at", "symbol", "primary_state", "trade_bias", "signal_grade",
               "overall_score", "alignment_score", "macro_context", "structure_15m_confirmed")


def new_actionable_in_run(con, run_id: int) -> list[dict]:
    """Symbols ACTIONABLE in this scan run (full or 15m recheck) that were not in the previous run."""
    prev = con.execute("SELECT MAX(scan_run_id) FROM symbol_observations WHERE scan_run_id < ?",
                       (run_id,)).fetchone()[0]
    prev_syms = set()
    if prev is not None:
        prev_syms = {r[0] for r in con.execute(
            "SELECT symbol FROM symbol_observations WHERE scan_run_id = ? AND primary_state = 'ACTIONABLE'",
            (prev,))}
    rows = con.execute(f"SELECT {OBS_FIELDS} FROM symbol_observations "
                       "WHERE scan_run_id = ? AND primary_state = 'ACTIONABLE' ORDER BY symbol", (run_id,))
    return [dict(r) for r in rows if r["symbol"] not in prev_syms]


SHADOW_MARK = '"shadow_sequence": true'  # as json.dumps writes it in raw_record_json


def _is_shadow_row(raw: str | None) -> bool:
    if not raw or SHADOW_MARK not in raw:
        return False
    try:
        return json.loads(raw).get("shadow_sequence") is True
    except ValueError:
        return False


def new_shadow_in_run(con, run_id: int) -> list[dict]:
    """Symbols flagged shadow_sequence in this scan run that were not flagged in the previous run."""
    prev = con.execute("SELECT MAX(scan_run_id) FROM symbol_observations WHERE scan_run_id < ?",
                       (run_id,)).fetchone()[0]
    like = f"%{SHADOW_MARK}%"
    prev_syms = set()
    if prev is not None:
        prev_syms = {r[0] for r in con.execute(
            "SELECT symbol, raw_record_json FROM symbol_observations WHERE scan_run_id = ? AND raw_record_json LIKE ?",
            (prev, like)) if _is_shadow_row(r[1])}
    rows = con.execute(f"SELECT {OBS_FIELDS} FROM symbol_observations "
                       "WHERE scan_run_id = ? AND raw_record_json LIKE ? ORDER BY symbol", (run_id, like))
    return [dict(r) for r in rows if _is_shadow_row(r["raw_record_json"]) and r["symbol"] not in prev_syms]


def plan_id(obs: dict, shadow: bool = False) -> str:
    t = parse_utc(obs["observed_at"])
    return f"{t.strftime('%Y%m%dT%H%MZ')}_{obs['symbol']}_S8" + ("X" if shadow else "")


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def build_plan(obs: dict, shadow: bool = False) -> dict:
    try:
        rec = json.loads(obs.get("raw_record_json") or "{}")
    except ValueError:
        rec = {}
    rec = rec if isinstance(rec, dict) else {}
    return {
        "id": plan_id(obs, shadow),
        "kind": KIND,
        "shadow": shadow,
        "shadow_blockers": list(rec.get("shadow_blockers") or []) if shadow else [],
        "created_at_utc": fmt_utc(utcnow()),
        "source": {k: obs.get(k) for k in SOURCE_KEYS},
        "step8": {
            "direction": direction_from_bias(obs.get("trade_bias")),
            "entry": _num(obs.get("entry_price")),
            "sl_s0": _num(obs.get("stop_loss")),
            "sl_s1": _num(rec.get("stop_s1")),
            "sl_s2": _num(rec.get("stop_s2")),
            "s2_reference": _num(rec.get("s2_entry_reference")),
            "s2_sweep_extreme": _num(rec.get("s2_sweep_extreme")),
            "tp1": _num(obs.get("target_1")),
            "tp2": _num(obs.get("target_2")),
            "rr_s0": _num(obs.get("risk_reward")),
            "zone_timeframe": rec.get("zone_timeframe"),
            "zone_tf_atr": _num(rec.get("zone_tf_atr")),
            "via_zone_watch": bool(rec.get("zone_watch_only")),
        },
        "run": {"ran": False, "skipped": "step8 test (no /tv)"},
        "legs": {},
        "final": False,
    }


def is_shadow(plan: dict) -> bool:
    return bool(plan.get("shadow"))


def active_plan_for(symbol: str, bias: str, plans: list[dict] | None = None,
                    shadow: bool | None = False) -> str | None:
    """A Step-8 plan for the same symbol+bias that is still running -> skip, one move counted once.
    shadow=False looks at real signals only, True at shadows only, None at both."""
    if plans is None:
        plans = []
        for p in iter_plan_paths():
            try:
                plans.append(load_json(p))
            except Exception:
                continue
    for pl in plans:
        src = pl.get("source") or {}
        if pl.get("kind") != KIND or pl.get("final"):
            continue
        if shadow is not None and is_shadow(pl) != shadow:
            continue
        if src.get("symbol") != symbol or src.get("trade_bias") != bias:
            continue
        created = parse_utc(pl.get("created_at_utc"))
        if created and (utcnow() - created).total_seconds() < LIVE_WINDOW_H * 3600:
            return pl.get("id") or "?"
    return None


def leg_specs(plan: dict) -> dict:
    s, src = plan.get("step8") or {}, plan.get("source") or {}
    start = parse_utc(src.get("observed_at"))
    if not start:
        return {}
    base = {"direction": s.get("direction"), "tp1": s.get("tp1"), "tp2": s.get("tp2"), "trigger": None,
            "cancel_if": None, "start": start, "valid_until": add_hours(start, VALID_H)}
    return {
        "step8_s0": dict(base, setup="PLACE_LIMIT", entry_type="limit", entry=s.get("entry"), sl=s.get("sl_s0")),
        "step8_s1": dict(base, setup="PLACE_LIMIT", entry_type="limit", entry=s.get("entry"), sl=s.get("sl_s1")),
        "step8_s2": dict(base, setup="TAKE_NOW", entry_type="market_now", entry=None, sl=s.get("sl_s2")),
    }


def _variant_line(name: str, kind: str, entry, sl, risk_usd: float | None) -> str:
    if entry is None or sl is None:
        return f"{name} {kind} · no valid stop (not tracked)"
    pct = stop_pct(entry, sl)
    line = f"{name} {kind} {fmt_px(entry)} · SL {fmt_px(sl)} ({pct:.1f}%)"
    if risk_usd:
        line += f" · notional ${risk_usd / ((pct + FEE_ROUNDTRIP_PCT) / 100):,.0f}"
    lev = safe_leverage(pct)
    if lev < LEVERAGE:
        line += f" · use ≤{lev}x"
    return line


def message(plan: dict, price: float | None, risk_usd: float | None, risk_label: str,
            extra_lines: list[str] | None = None) -> str:
    s, src = plan["step8"], plan["source"]
    direction = (s.get("direction") or "?").upper()
    ref = price if price else s.get("s2_reference")
    lines = [f"🧪 STEP-8 SIGNAL · {src.get('symbol')} {direction} (forward test, not validated)"]
    if s.get("via_zone_watch"):
        lines.append("Found by the zone watch (Step 7 had dropped it)")
    tp = f"TP1 {fmt_px(s.get('tp1'))}" + (f" · TP2 {fmt_px(s.get('tp2'))}" if s.get("tp2") is not None else "")
    lines.append(f"Market ≈ {fmt_px(ref)} · {tp}")
    lines.append(_variant_line("S0", "limit", s.get("entry"), s.get("sl_s0"), risk_usd))
    lines.append(_variant_line("S1", "limit", s.get("entry"), s.get("sl_s1"), risk_usd))
    lines.append(_variant_line("S2", "market ≈", ref, s.get("sl_s2"), risk_usd))
    if risk_usd:
        lines.append(f"Notional sized for {risk_label} = ${risk_usd:,.0f} per variant (fees included)")
    lines += extra_lines or []
    lines.append("Three stop rules tested side by side; no rule has passed yet.")
    return "\n".join(lines)


def is_step8(plan: dict) -> bool:
    return plan.get("kind") == KIND
