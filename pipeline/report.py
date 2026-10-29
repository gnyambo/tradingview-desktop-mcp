# -*- coding: utf-8 -*-
"""
Report: compares the /tv plans with the scanner's own levels on the same alerts,
breaks /tv results down by grade and by rubric criterion, compares market entries,
and scores the forward test (scanner @ market, fresh alerts only, vs the wide-stop rule).
Usage: python3 report.py [--since YYYY-MM-DD] [--forward-from "YYYY-MM-DD HH:MM"]
"""
import argparse
import math
import os
from collections import defaultdict

from datetime import timedelta

from common import (FEE_ROUNDTRIP_PCT, NOTIONAL_USD, direction_from_bias, fmt_utc, iter_plan_paths,
                    load_json, parse_utc)

def stop_bucket(entry, sl, atr):
    try:
        m = abs(float(entry) - float(sl)) / float(atr)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return "stop < 1x ATR" if m < 1 else ("stop 1-2x ATR" if m <= 2 else "stop > 2x ATR")


# Forward tests, pass marks fixed in advance (2026-10-25). Alerts created from FORWARD_FROM (UTC),
# repeats of a move already counted left out.
# PRIMARY (from 2026-10-29): rule_wide = Step-7 alert, market entry, SL 3x / TP1 3x 15m ATR, on ALL
#   alerts. Pass = after FORWARD_N resolved trades, >= FORWARD_MIN_R after fees, >= FORWARD_MIN_LONGS longs.
# INFORMATION ONLY: scanner_market on fresh alerts (Step-8 levels, mostly stale, too few trades).
#   Its original pass mark is still printed: also had to beat rule_wide on the same alerts.
FORWARD_FROM = os.environ.get("TV_FORWARD_FROM", "2026-10-26 00:00")
FORWARD_N = 30
FORWARD_MIN_R = 0.10
FORWARD_MIN_LONGS = 5

CRITERIA = ("d1_alignment", "h4_alignment", "location", "confirmation_15m", "rr_tp1", "stop_quality")


def leg_r(leg: dict) -> float | None:
    """R of a finished, entered trade (TP1 counts as the result even if TP2 came later)."""
    if not leg or not leg.get("final") or leg.get("fill_price") is None:
        return None
    return leg.get("r")




def leg_usd(leg: dict, sl) -> float | None:
    """Dollar result of an entered, resolved leg at fixed notional, after fees.
    price move % = r x stop distance % (r already carries the sign)."""
    try:
        fill, r = float(leg["fill_price"]), float(leg["r"])
        stop_pct = abs(fill - float(sl)) / fill
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return NOTIONAL_USD * (r * stop_pct - FEE_ROUNDTRIP_PCT / 100)


def net_r(leg: dict, sl) -> float | None:
    """R after the round-trip fee (fee as a share of the stop distance)."""
    try:
        fill, r = float(leg["fill_price"]), float(leg["r"])
        stop_pct = abs(fill - float(sl)) / fill * 100
        return r - FEE_ROUNDTRIP_PCT / stop_pct
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def verdict(net_rs: list[float], longs: int, rival: float | None = None, rival_name: str = "") -> str:
    """The pre-registered pass mark, applied to after-fee R of resolved trades."""
    n = len(net_rs)
    if n < FORWARD_N:
        return f"not yet - {n}/{FORWARD_N} resolved trades, {longs}/{FORWARD_MIN_LONGS} longs"
    m, _ = mean_se(net_rs)
    fails = []
    if m < FORWARD_MIN_R:
        fails.append(f"avg {m:+.2f}R < {FORWARD_MIN_R:+.2f}R")
    if rival is not None and m <= rival:
        fails.append(f"does not beat {rival_name}")
    if longs < FORWARD_MIN_LONGS:
        fails.append(f"only {longs} longs (< {FORWARD_MIN_LONGS}): one-sided market")
    return "PASS" if not fails else "FAIL - " + "; ".join(fails)


def exposure(intervals: list[tuple[str, str, str]]) -> tuple[int, int]:
    """(max same-direction positions open at once, hours with 2+ same-direction entries).
    Open = filled_at .. closed_at (closed_at is when TP2/SL/timeout ended the leg, so an upper bound)."""
    peak = 0
    for d in ("long", "short"):
        ev = []
        for start, end, dd in intervals:
            if dd == d:
                ev += [(start, 1), (end, -1)]
        cur = 0
        for _, step in sorted(ev, key=lambda x: (x[0], x[1])):  # exits sort before entries at equal times
            cur += step
            peak = max(peak, cur)
    by_hour = defaultdict(int)
    for start, _, d in intervals:
        by_hour[(start[:13], d)] += 1
    return peak, sum(1 for v in by_hour.values() if v >= 2)


def mean_se(xs: list[float]) -> tuple[float | None, float | None]:
    if not xs:
        return None, None
    m = sum(xs) / len(xs)
    if len(xs) < 2:
        return m, None
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return m, sd / math.sqrt(len(xs))


REPEAT_WINDOW_H = 96  # same as tv_worker.DUP_WINDOW_H


def mark_repeats(plans: list[dict]) -> int:
    """Plan-only alerts (no /tv run) that repeat the same symbol+bias while an earlier counted plan
    was still running get pl["_repeat"] = True, so one move is counted once. Same rule the worker
    applies from 2026-10-29; this also cleans up the repeats recorded before that fix."""
    end_by_key, n = {}, 0
    for pl in sorted(plans, key=lambda x: x.get("created_at_utc") or ""):
        if pl.get("tv"):
            continue
        src = pl.get("source") or {}
        key, t = (src.get("symbol"), src.get("trade_bias")), pl.get("created_at_utc") or ""
        if t < end_by_key.get(key, ""):
            pl["_repeat"] = True
            n += 1
            continue
        cap = parse_utc(t)
        cap = fmt_utc(cap + timedelta(hours=REPEAT_WINDOW_H)) if cap else t
        legs = (pl.get("legs") or {}).values()
        if pl.get("final") and legs:
            end = max((l.get("closed_at") or "") for l in legs)
        else:
            end = cap
        end_by_key[key] = min(end, cap)
    return n


class Bucket:
    def __init__(self):
        self.n = self.entered = self.wins = self.losses = self.timeouts = self.live = 0
        self.not_entered = defaultdict(int)
        self.rs = []
        self.net_rs = []
        self.usd = []
        self.risk_usd = []

    def add(self, leg: dict | None, sl=None):
        self.n += 1
        if not leg:
            self.live += 1
            return
        entered = leg.get("fill_price") is not None
        if not leg.get("final") and not (entered and leg.get("result")):
            self.live += 1  # still waiting, or open before TP1/SL
            return
        st = leg.get("state")
        if not entered:
            if st == "CANCELLED":  # split: right direction but missed, vs idea invalidated
                st = "MISSED" if (leg.get("note") or "").startswith("TP1 reached") else "INVALIDATED"
            self.not_entered[st] += 1
            return
        self.entered += 1
        res = leg.get("result")
        self.wins += res == "TP1"
        self.losses += res == "LOSS"
        self.timeouts += res == "TIMEOUT"
        if leg.get("r") is not None:
            self.rs.append(leg["r"])
            sl = sl if sl is not None else (leg.get("levels") or {}).get("sl")
            nr = net_r(leg, sl) if sl is not None else None
            if nr is not None:
                self.net_rs.append(nr)
            usd = leg_usd(leg, sl) if sl is not None else None
            if usd is not None:
                self.usd.append(usd)
                try:
                    self.risk_usd.append(NOTIONAL_USD * abs(float(leg["fill_price"]) - float(sl))
                                         / float(leg["fill_price"]))
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

    def line(self, label: str) -> str:
        done = self.entered
        wr = f"{100 * self.wins / done:.0f}%" if done else "-"
        avg = f"{sum(self.rs) / len(self.rs):+.2f}R" if self.rs else "-"
        tot = f"{sum(self.rs):+.1f}R" if self.rs else "-"
        ne = ", ".join(f"{k.lower()} {v}" for k, v in sorted(self.not_entered.items())) or "-"
        usd = (f"${sum(self.usd):+.0f} (avg ${sum(self.usd) / len(self.usd):+.1f})") if self.usd else "-"
        risk = f"${sum(self.risk_usd) / len(self.risk_usd):.0f}" if self.risk_usd else "-"
        return (f"{label:<20} plans {self.n:>3} | live {self.live:>2} | entered {done:>3} | "
                f"TP1 {self.wins:>3} SL {self.losses:>3} TO {self.timeouts:>2} | win {wr:>4} | "
                f"avg {avg:>7} | total {tot:>7} | {usd:>18} | risk/trade {risk:>4} | not entered: {ne}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only plans created on/after this UTC date")
    ap.add_argument("--forward-from", default=FORWARD_FROM,
                    help=f"start of the forward test, UTC (default {FORWARD_FROM})")
    a = ap.parse_args()

    plans = []
    for p in iter_plan_paths():
        pl = load_json(p)
        if a.since and (pl.get("created_at_utc") or "") < a.since:
            continue
        plans.append(pl)
    if not plans:
        print("no plans yet")
        return

    tv_all, sc_all, tv_mkt = Bucket(), Bucket(), Bucket()
    rule_all, rule_tv = Bucket(), Bucket()
    sc_where_tv = Bucket()  # scanner leg only on alerts where /tv also produced a plan
    by_grade, by_conflict = defaultdict(Bucket), defaultdict(Bucket)
    by_crit = {c: defaultdict(Bucket) for c in CRITERIA}
    sc_by_stop, tv_by_stop = defaultdict(Bucket), defaultdict(Bucket)
    sc_mkt_all, wide_all, mk_lim, mk_rule = Bucket(), Bucket(), Bucket(), Bucket()  # market-entry table
    fw_sc, fw_wide, fw_rule = Bucket(), Bucket(), Bucket()  # forward test: fresh alerts only
    fw_alerts = fw_stale = fw_longs = 0
    pw_wide, pw_rule = Bucket(), Bucket()  # primary forward test: rule_wide on all alerts
    pw_longs, pw_intervals = 0, []
    failed = capped = 0
    cost = 0.0
    repeats = mark_repeats(plans)
    for pl in plans:
        legs = pl.get("legs", {})
        sp = pl.get("scanner_plan") or {}
        sc_all.add(legs.get("scanner"), sp.get("sl"))
        rule_all.add(legs.get("rule_atr"))
        smk = legs.get("scanner_market")
        if pl.get("_repeat"):
            smk = None  # repeat of a move already counted: kept out of the market-entry and forward tables
        elif smk:
            sc_mkt_all.add(smk, sp.get("sl"))
            wide_all.add(legs.get("rule_wide"))
            mk_lim.add(legs.get("scanner"), sp.get("sl"))
            mk_rule.add(legs.get("rule_atr"))
        if (pl.get("created_at_utc") or "") >= a.forward_from and not pl.get("_repeat"):
            fw_alerts += 1
            wleg = legs.get("rule_wide")
            pw_wide.add(wleg)
            pw_rule.add(legs.get("rule_atr"))
            pdir = direction_from_bias((pl.get("source") or {}).get("trade_bias"))
            if wleg and wleg.get("result"):
                pw_longs += pdir == "long"
            if wleg and wleg.get("filled_at"):
                pw_intervals.append((wleg["filled_at"], wleg.get("closed_at") or "9999-12-31 00:00", pdir))
            if smk and smk.get("state") == "STALE_LEVELS":
                fw_stale += 1
            elif smk and smk.get("fill_price") is not None:
                fw_sc.add(smk, sp.get("sl"))
                fw_wide.add(legs.get("rule_wide"))
                fw_rule.add(legs.get("rule_atr"))
                if smk.get("result"):
                    fw_longs += sp.get("direction") == "long"
        run = pl.get("run", {})
        cost += run.get("cost_usd") or 0
        if run.get("skipped"):
            capped += 1
        tv = pl.get("tv")
        if not tv:
            failed += bool(run.get("ran"))
            continue
        tsl = tv.get("sl")
        tv_all.add(legs.get("tv"), tsl)
        tv_mkt.add(legs.get("tv_market"), tsl)
        rule_tv.add(legs.get("rule_atr"))
        sc_where_tv.add(legs.get("scanner"), sp.get("sl"))
        by_grade[tv.get("grade", "?")].add(legs.get("tv"), tsl)
        by_conflict["4h against" if tv.get("htf_conflict") else "4h not against"].add(legs.get("tv"), tsl)
        for c in CRITERIA:
            by_crit[c][(tv.get("criteria") or {}).get(c, "?")].add(legs.get("tv"), tsl)
        atr = tv.get("atr15m")  # 15m ATR measured by /tv at the same moment
        b = stop_bucket(sp.get("entry"), sp.get("sl"), atr)
        if b:
            sc_by_stop[b].add(legs.get("scanner"), sp.get("sl"))
        tv_entry = (tv.get("entry") or {}).get("price") or (tv.get("trigger") or {}).get("price")
        b = stop_bucket(tv_entry, tv.get("sl"), atr)
        if b:
            tv_by_stop[b].add(legs.get("tv"), tsl)

    print(f"Alerts: {len(plans)}  | /tv failed: {failed} | skipped by cap: {capped} | "
          f"est. Claude cost: ${cost:.2f}\n")
    print("== /tv vs scanner (same alerts) ==")
    print(tv_all.line("/tv"))
    print(tv_mkt.line("/tv @ market"))
    print(rule_tv.line("rule @ market"))
    print(sc_where_tv.line("scanner"))
    print(sc_all.line("scanner (all)"))
    print(rule_all.line("rule (all)"))
    print("\n== /tv by grade ==")
    for g in sorted(by_grade):
        print(by_grade[g].line(f"grade {g}"))
    print("\n== /tv by 4h conflict ==")
    for k in sorted(by_conflict):
        print(by_conflict[k].line(k))
    print("\n== stop distance in 15m ATR (ATR from the /tv run at alert time) ==")
    for k in sorted(sc_by_stop):
        print(sc_by_stop[k].line(f"scanner {k[5:]}"))
    for k in sorted(tv_by_stop):
        print(tv_by_stop[k].line(f"/tv {k[5:]}"))
    print("\n== /tv by rubric criterion (score 0/1/2) ==")
    for c in CRITERIA:
        for score in sorted(by_crit[c], key=str):
            print(by_crit[c][score].line(f"{c}={score}"))
    print(f"\n== market entries, all alerts (scanner_market: stale alerts are not entered; "
          f"{repeats} repeat alerts of a move already counted left out) ==")
    print(mk_lim.line("scanner @ limit"))
    print(sc_mkt_all.line("scanner @ market"))
    print(wide_all.line("rule wide @ market"))
    print(mk_rule.line("rule @ market"))

    print(f"\n== FORWARD TEST (PRIMARY) from {a.forward_from} UTC: Step-7 alert, market entry, "
          f"rule wide stops, all alerts ==")
    print(f"alerts {fw_alerts} (repeats of a move already counted left out)")
    print(pw_wide.line("rule wide @ market"))
    print(pw_rule.line("rule (tight, ref)"))
    m_pw, se_pw = mean_se(pw_wide.net_rs)
    if m_pw is not None:
        se_txt = f" ± {se_pw:.2f}" if se_pw is not None else ""
        print(f"after fees: rule wide {m_pw:+.2f}R{se_txt} per trade | longs resolved {pw_longs}")
    peak, clusters = exposure(pw_intervals)
    print(f"correlation: up to {peak} same-direction positions open at once | "
          f"{clusters} hours with 2+ same-direction entries")
    print(f"VERDICT (rule wide): {verdict(pw_wide.net_rs, pw_longs)}")

    print(f"\n== scanner @ market, fresh alerts only (INFORMATION ONLY: Step-8 levels) ==")
    print(f"alerts {fw_alerts} | stale (skipped) {fw_stale} | fresh entered {fw_sc.entered + fw_sc.live}")
    print(fw_sc.line("scanner @ market"))
    print(fw_wide.line("rule wide (same)"))
    print(fw_rule.line("rule (same)"))
    m_sc, se_sc = mean_se(fw_sc.net_rs)
    m_wd, _ = mean_se(fw_wide.net_rs)
    if m_sc is not None:
        se_txt = f" ± {se_sc:.2f}" if se_sc is not None else ""
        wd_txt = f"{m_wd:+.2f}R" if m_wd is not None else "-"
        print(f"after fees: scanner @ market {m_sc:+.2f}R{se_txt} per trade | rule wide {wd_txt} | "
              f"longs resolved {fw_longs}")
    print(f"info verdict (scanner @ market): {verdict(fw_sc.net_rs, fw_longs, m_wd, 'rule wide')}")

    print("\n/tv @ market = what-if: entered at market when the plan was issued, same SL/TP as /tv.")
    print("scanner @ market = what-if: market entry when the alert is seen, the scanner's own SL/TP; "
          "stale = price already past the scanner's TP1 at that moment (not entered).")
    print("rule wide = same as rule but SL 3x / TP1 3x / TP2 6x 15m ATR (~2% stops). "
          "Forward-test R is after fees (fee / stop distance).")
    print("rule @ market = no Claude: market entry in the scanner's direction, SL 1.5x ATR, TP1 1R, TP2 2R "
          "(compare avg R, not win rate: the targets differ).")
    print(f"$ = your sizing: ${NOTIONAL_USD:.0f} notional per trade (margin x leverage), "
          f"minus ~{FEE_ROUNDTRIP_PCT}% round-trip fees. Funding not included. "
          "risk/trade = average $ lost if a trade hits its stop.")
    print("not entered: missed = TP1 reached before entry (direction right, entry too far); "
          "invalidated = cancel level hit first; stale_levels = already past TP1 when issued.")
    print("win = TP1 before SL, among entered trades. R uses TP1 as the result "
          "(a trade that hit TP1 counts even while TP2 is still open). "
          "Small samples: treat differences under ~30 trades per row as noise.")


if __name__ == "__main__":
    main()
