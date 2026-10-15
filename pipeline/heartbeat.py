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

from common import PLANS_DIR, fmt_utc, iter_plan_paths, load_json, parse_utc, telegram_send, utcnow

SCANNER_DB = os.environ.get("TV_SCANNER_DB", "/opt/binance-futures-scanner/scanner_history.db")


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

    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb < 2:
        issues.append(f"disk free only {free_gb:.1f} GB")

    since = utcnow() - timedelta(hours=24)
    alerts = runs = fails = live = 0
    cost = 0.0
    resolved = {}
    for p in iter_plan_paths():
        pl = safe_load(p)
        created = parse_utc(pl.get("created_at_utc"))
        run = pl.get("run", {})
        if created and created >= since:
            alerts += 1
            if run.get("ran"):
                runs += 1
                fails += bool(run.get("error"))
                cost += run.get("cost_usd") or 0
        if not pl.get("final"):
            live += 1
        leg = (pl.get("legs") or {}).get("tv") or {}
        closed = parse_utc(leg.get("closed_at")) if leg.get("closed_at") else None
        if leg.get("final") and closed and closed >= since:
            key = leg.get("result") or leg.get("state")
            resolved[key] = resolved.get(key, 0) + 1

    res_txt = ", ".join(f"{k} {v}" for k, v in sorted(resolved.items())) or "none"
    head = "🫀 tv-pipeline OK" if not issues else "🚨 tv-pipeline NEEDS ATTENTION"
    msg = (f"{head} · {fmt_utc(utcnow())} UTC\n"
           f"worker {worker_state}, polled {ago(tick_m)} · resolver ran {ago(res_m)} · "
           f"scanner wrote {ago(scan_m)}\n"
           f"last 24h: alerts {alerts}, /tv runs {runs} (failed {fails}), est. cost ${cost:.2f}\n"
           f"/tv plans resolved 24h: {res_txt} · live plans: {live} · disk free {free_gb:.0f} GB")
    if issues:
        msg += "\n\nIssues:\n- " + "\n- ".join(issues)
    print(msg)
    if not a.print:
        telegram_send(msg)


if __name__ == "__main__":
    main()
