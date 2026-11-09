# -*- coding: utf-8 -*-
"""
Daily health message for the /tv pipeline (and the scanner feeding it).
If this message stops arriving, something is down.
Usage: python3 heartbeat.py          # send to Telegram
       python3 heartbeat.py --print  # print only
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
from datetime import timedelta

import step8_test
from common import PLANS_DIR, fmt_utc, iter_plan_paths, load_json, parse_utc, telegram_send, utcnow

SCANNER_DB = os.environ.get("TV_SCANNER_DB", "/opt/binance-futures-scanner/scanner_history.db")
SCANNER_SIGNALS = os.environ.get("TV_SCANNER_SIGNALS", "/opt/binance-futures-scanner/signals.json")
# legs whose results the daily message reports (the /tv leg only exists when /tv runs)
TRACKED_LEGS = (("step8_s0", "S0"), ("step8_s1", "S1"), ("step8_s2", "S2"), ("rule_wide", "rule wide"), ("tv", "/tv"))


def minutes_ago(ts: str | None) -> float | None:
    t = parse_utc(ts) if ts else None
    return None if t is None else (utcnow() - t).total_seconds() / 60


def ago(m: float | None) -> str:
    if m is None:
        return "never"
    return f"{m:.0f} min ago" if m < 120 else f"{m / 60:.1f} h ago"


def safe_load(path):
    try:
        return load_json(path)
    except Exception:
        return {}


def resolved_since(plans: list[dict], since) -> dict[str, dict[str, int]]:
    """{leg label: {result: count}} for tracked legs that finished at/after `since`.
    Result = TP1 / LOSS / TIMEOUT for entered trades, else the state (EXPIRED, CANCELLED, ...)."""
    out: dict[str, dict[str, int]] = {}
    for pl in plans:
        legs = pl.get("legs") or {}
        for name, label in TRACKED_LEGS:
            leg = legs.get(name) or {}
            closed = parse_utc(leg.get("closed_at")) if leg.get("closed_at") else None
            if leg.get("final") and closed and closed >= since:
                key = leg.get("result") or leg.get("state") or "?"
                out.setdefault(label, {})
                out[label][key] = out[label].get(key, 0) + 1
    return out


def resolved_text(res: dict[str, dict[str, int]]) -> str:
    if not res:
        return "none"
    order = [label for _, label in TRACKED_LEGS if label in res]
    return " · ".join(f"{label} " + ", ".join(f"{k} {v}" for k, v in sorted(res[label].items())) for label in order)


def zone_watch_count() -> int | None:
    try:
        return int((load_json(SCANNER_SIGNALS).get("summary") or {}).get("zone_watch_active"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args()
    issues = []

    def active(unit):
        try:
            return subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                                  text=True, timeout=10).stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    worker_state = active("tv-worker")
    timer_state = active("tv-resolver.timer")
    if worker_state != "active":
        issues.append(f"tv-worker is {worker_state}")
    if timer_state != "active":
        issues.append(f"tv-resolver.timer is {timer_state}")

    ws = safe_load(PLANS_DIR / ".worker_state.json")
    rs = safe_load(PLANS_DIR / ".resolver_state.json")
    tick_m, res_m = minutes_ago(ws.get("last_tick_at")), minutes_ago(rs.get("last_run_at"))
    if tick_m is None or tick_m > 10:
        issues.append(f"worker last polled {ago(tick_m)}")
    if res_m is None or res_m > 40:
        issues.append(f"resolver last ran {ago(res_m)}")
    if rs.get("errors"):
        issues.append(f"resolver errors: {rs['errors']} ({rs.get('last_error')})")
    err = ws.get("last_error") or {}
    if err and (minutes_ago(err.get("at")) or 1e9) < 24 * 60:
        issues.append(f"worker error {ago(minutes_ago(err['at']))}: {err.get('msg', '')[:200]}")

    try:  # is the scanner itself still writing?
        con = sqlite3.connect(f"file:{SCANNER_DB}?mode=ro", uri=True, timeout=10)
        last_obs = con.execute("SELECT MAX(observed_at) FROM symbol_observations").fetchone()[0]
        con.close()
        scan_m = minutes_ago(last_obs)
        if scan_m is None or scan_m > 90:
            issues.append(f"scanner last wrote {ago(scan_m)}")
    except Exception as e:
        scan_m = None
        issues.append(f"scanner db unreadable: {e}")

    acct_txt = "account: unknown"
    try:  # the levels message sizes positions from the dashboard's balance
        from account import _staleness, fetch_account
        acct = fetch_account()
        stale = _staleness(acct)
        dd = acct.get("drawdown_pct")
        acct_txt = (f"account ${acct['equity_usdt']:,.0f}" + (f", {dd:.1f}% below 30-day peak" if dd is not None else "")
                    + f", {len(acct['positions'])} open")
        if stale:
            issues.append(f"{stale}: is the crypto-risk-app running?")
    except Exception as e:
        issues.append(f"dashboard database unreadable, levels use the fixed $ fallback: {str(e)[:120]}")

    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb < 2:
        issues.append(f"disk free only {free_gb:.1f} GB")

    since = utcnow() - timedelta(hours=24)
    alerts = s8_signals = runs = fails = live = s8_live = 0
    cost = 0.0
    plans = []
    for p in iter_plan_paths():
        pl = safe_load(p)
        plans.append(pl)
        created = parse_utc(pl.get("created_at_utc"))
        run = pl.get("run", {})
        is_s8 = step8_test.is_step8(pl)
        if created and created >= since:
            if is_s8:
                s8_signals += 1
            else:
                alerts += 1
            if run.get("ran"):
                runs += 1
                fails += bool(run.get("error"))
                cost += run.get("cost_usd") or 0
        if not pl.get("final"):
            live += 1
            s8_live += is_s8

    res_txt = resolved_text(resolved_since(plans, since))
    watch = zone_watch_count()
    watch_txt = f" · zone watch {watch}" if watch is not None else ""
    runs_txt = f", /tv runs {runs} (failed {fails}), est. cost ${cost:.2f}" if runs else ""
    head = "🫀 tv-pipeline OK" if not issues else "🚨 tv-pipeline NEEDS ATTENTION"
    msg = (f"{head} · {fmt_utc(utcnow())} UTC\n"
           f"worker {worker_state}, polled {ago(tick_m)} · resolver ran {ago(res_m)} · "
           f"scanner wrote {ago(scan_m)}\n"
           f"last 24h: Step-8 signals {s8_signals}, MTF alerts {alerts}{runs_txt}{watch_txt}\n"
           f"resolved 24h: {res_txt}\n"
           f"live plans: {live} (Step-8 {s8_live}) · disk free {free_gb:.0f} GB\n"
           f"{acct_txt}")
    if issues:
        msg += "\n\nIssues:\n- " + "\n- ".join(issues)
    print(msg)
    if not a.print:
        telegram_send(msg)


if __name__ == "__main__":
    main()
