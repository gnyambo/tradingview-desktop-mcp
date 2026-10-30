# -*- coding: utf-8 -*-
"""
tv-worker: watches the scanner's SQLite history for NEW MTF-qualified setups and runs
`claude -p "/tv ..."` on each one, one at a time. Saves a plan file per alert with:
  - source:        the scanner observation (symbol, bias, scores, macro)
  - scanner_plan:  the scanner's own entry/SL/TP (baseline, never shown to /tv)
  - tv:            the JSON block /tv returned (or null on failure / cap)
The resolver later tracks all legs on the same candles.

Each new alert also gets a "📐 levels" Telegram message (TV_LEVELS_TELEGRAM=0 turns it off): market
price, the rule_wide SL/TP being forward-tested, $ risk, the scanner's zone (flagged when its TP1 is
already passed) and the same-direction plans already open.

TV_RUN_CLAUDE=0 (in /etc/tv-pipeline.env) switches the /tv runs off: every alert still gets a
plan file (scanner, scanner_market and rule legs), but no Claude, no TradingView, no TV PLAN message.

"NEW" mirrors monitor_scanner_TELEGRAM.notify_changes(): a symbol MTF-qualified in a scan
run that was not MTF-qualified in the previous scan run.

Usage:
  python3 tv_worker.py              # run forever (systemd)
  python3 tv_worker.py --once       # process what's new, then exit
  python3 tv_worker.py --dry-run    # show detected new setups, run nothing, change nothing
  python3 tv_worker.py --dry-run --since-run 4080   # check detection against past alerts
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from common import (FEE_ROUNDTRIP_PCT, PLANS_DIR, RISK_USD, SYMBOL_RE, WORD_RE, atr14, direction_from_bias, fmt_px, fmt_utc,
                    iter_plan_paths, klines_15m_before, load_json, parse_utc, plan_header, price_now,
                    safe_leverage, save_json, sizing_lines, stop_pct, telegram_send, utcnow)
from account import get_account, positions_lines, risk_budget
from resolver import RULE_WIDE_SL_ATR, RULE_WIDE_TP1_ATR, RULE_WIDE_TP2_ATR

SCANNER_DB = os.environ.get("TV_SCANNER_DB", "/opt/binance-futures-scanner/scanner_history.db")
STATE_PATH = PLANS_DIR / ".worker_state.json"
RUN_DIR = Path(os.environ.get("TV_RUN_DIR", "/var/lib/tv-worker"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
POLL_S = int(os.environ.get("TV_POLL_SECONDS", "10"))
DAILY_CAP = int(os.environ.get("TV_DAILY_CAP", "15"))
RUN_TIMEOUT_S = int(os.environ.get("TV_RUN_TIMEOUT", "900"))
SEND_GRADES = set(os.environ.get("TV_TELEGRAM_GRADES", "A,B,C").split(","))
MAX_AGE_MIN = int(os.environ.get("TV_MAX_AGE_MIN", "60"))  # older setups (e.g. after downtime): scanner-only
SHOTS_KEEP_DAYS = int(os.environ.get("TV_SHOTS_KEEP_DAYS", "30"))
RUN_CLAUDE = os.environ.get("TV_RUN_CLAUDE", "1").strip() != "0"  # 0 = record alerts only, no /tv runs
LEVELS_TELEGRAM = os.environ.get("TV_LEVELS_TELEGRAM", "1").strip() != "0"
# The scanner writes a whole scan in ONE transaction, before it sends its Telegram alert, so a visible
# scan run is already complete; a few seconds of margin is enough.
SETTLE_S = int(os.environ.get("TV_SETTLE_SECONDS", "5"))
# Claude gets no shell at all: the worker starts/stops TradingView itself.
ALLOWED_TOOLS = "mcp__binance,mcp__tradingview"
CDP_URL = os.environ.get("TV_CDP_URL", "http://127.0.0.1:9222/json/list")
OBS_FIELDS = ("id, scan_run_id, observed_at, symbol, primary_state, trade_bias, signal_grade, "
              "overall_score, alignment_score, macro_context, entry_type, entry_price, stop_loss, "
              "target_1, target_2, risk_reward, structure_15m_confirmed")


def log(msg: str) -> None:
    print(f"{fmt_utc(utcnow())} {msg}", flush=True)


def alert(st: dict, text: str) -> None:
    """Problem notification to Telegram, at most one per hour."""
    st["last_error"] = {"at": fmt_utc(utcnow()), "msg": text[:500]}
    if time.time() - st.get("last_fail_alert", 0) > 3600:
        telegram_send(f"⚠️ tv-worker: {text[:800]}")
        st["last_fail_alert"] = time.time()


# ------------------------------------------------------------------ state

def load_state() -> dict:
    try:
        return load_json(STATE_PATH)
    except FileNotFoundError:
        return {}


def save_state(st: dict) -> None:
    save_json(STATE_PATH, st)


# ------------------------------------------------------------------ scanner db (read-only)

def db_connect():
    con = sqlite3.connect(f"file:{SCANNER_DB}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def max_run_id(con) -> int:
    return con.execute("SELECT COALESCE(MAX(scan_run_id), 0) FROM symbol_observations").fetchone()[0]


def settled_runs_after(con, last_run: int) -> list[int]:
    cutoff = (utcnow().timestamp() - SETTLE_S)
    rows = con.execute(
        "SELECT scan_run_id, MAX(observed_at) FROM symbol_observations "
        "WHERE scan_run_id > ? GROUP BY scan_run_id ORDER BY scan_run_id", (last_run,)).fetchall()
    out = []
    for run_id, last_obs in rows:
        ts = parse_utc(last_obs)
        if ts and ts.timestamp() <= cutoff:
            out.append(run_id)
        else:
            break  # keep order: never skip past an unsettled run
    return out


def new_mtf_in_run(con, run_id: int) -> list[dict]:
    prev = con.execute("SELECT MAX(scan_run_id) FROM symbol_observations WHERE scan_run_id < ?",
                       (run_id,)).fetchone()[0]
    prev_syms = set()
    if prev is not None:
        prev_syms = {r[0] for r in con.execute(
            "SELECT symbol FROM symbol_observations WHERE scan_run_id = ? AND in_mtf_qualified = 1",
            (prev,))}
    rows = con.execute(f"SELECT {OBS_FIELDS} FROM symbol_observations "
                       "WHERE scan_run_id = ? AND in_mtf_qualified = 1 ORDER BY symbol", (run_id,))
    return [dict(r) for r in rows if r["symbol"] not in prev_syms]


# ------------------------------------------------------------------ plan bookkeeping

def plan_id(obs: dict) -> str:
    t = parse_utc(obs["observed_at"])
    return f"{t.strftime('%Y%m%dT%H%MZ')}_{obs['symbol']}"


DUP_WINDOW_H = 96  # a plan-only alert blocks repeats at most this long (72h timeout + entry slack)


def active_plan_for(symbol: str, bias: str) -> str | None:
    """Same symbol+bias while the earlier plan is still live -> skip, so one move is counted once.
    Live = the /tv leg is not finished (when /tv ran), else ANY leg of the plan is not finished
    (stale alerts close scanner_market at once, but their rule trades keep running)."""
    for p in iter_plan_paths():
        try:
            pl = load_json(p)
        except Exception:
            continue
        src = pl.get("source", {})
        if src.get("symbol") != symbol or src.get("trade_bias") != bias or pl.get("final"):
            continue
        legs = pl.get("legs", {})
        if pl.get("tv"):
            live = not legs.get("tv", {}).get("final")
        else:
            created = parse_utc(pl.get("created_at_utc"))
            live = bool(created) and (utcnow() - created).total_seconds() < DUP_WINDOW_H * 3600
        if live:
            return p.name
    return None


def runs_today() -> int:
    today = utcnow().strftime("%Y-%m-%d")
    n = 0
    for p in iter_plan_paths():
        try:
            pl = load_json(p)
        except Exception:
            continue
        if (pl.get("created_at_utc") or "").startswith(today) and pl.get("run", {}).get("ran"):
            n += 1
    return n


def scanner_plan(obs: dict) -> dict:
    return {"direction": direction_from_bias(obs["trade_bias"]), "entry_type": obs["entry_type"],
            "entry": obs["entry_price"], "sl": obs["stop_loss"], "tp1": obs["target_1"],
            "tp2": obs["target_2"], "rr": obs["risk_reward"], "signal_grade": obs["signal_grade"]}


def base_plan(obs: dict) -> dict:
    return {
        "id": plan_id(obs),
        "created_at_utc": fmt_utc(utcnow()),
        "source": {k: obs[k] for k in ("id", "scan_run_id", "observed_at", "symbol", "primary_state",
                                       "trade_bias", "signal_grade", "overall_score", "alignment_score",
                                       "macro_context", "structure_15m_confirmed")},
        "scanner_plan": scanner_plan(obs),
        "tv": None,
        "run": {"ran": False},
        "legs": {},
        "final": False,
    }


# ------------------------------------------------------------------ levels message

def open_same_direction(direction: str) -> list[str]:
    """Coins with a tracked plan in this direction whose rule_wide trade is still running."""
    out = set()
    for p in iter_plan_paths():
        try:
            pl = load_json(p)
        except Exception:
            continue
        src = pl.get("source") or {}
        if pl.get("final") or direction_from_bias(src.get("trade_bias")) != direction:
            continue
        created = parse_utc(pl.get("created_at_utc"))
        if not created or (utcnow() - created).total_seconds() > DUP_WINDOW_H * 3600:
            continue
        if ((pl.get("legs") or {}).get("rule_wide") or {}).get("final"):
            continue
        out.add(str(src.get("symbol", "?")).removesuffix("USDT"))
    return sorted(out)


def alert_levels(obs: dict, plan: dict) -> dict | None:
    """Levels at alert time, the same rule the forward test tracks (rule_wide). Never raises."""
    try:
        symbol, direction = obs["symbol"], direction_from_bias(obs["trade_bias"])
        sign = 1 if direction == "long" else -1
        px = price_now(symbol)
        atr = atr14(klines_15m_before(symbol, parse_utc(obs["observed_at"])))
        if not px or not atr:
            return None
        sl, tp1, tp2 = (px - sign * RULE_WIDE_SL_ATR * atr, px + sign * RULE_WIDE_TP1_ATR * atr,
                        px + sign * RULE_WIDE_TP2_ATR * atr)
        acct, acct_problem = get_account()
        risk_usd, risk_pct, risk_label = risk_budget(acct)
        equity_txt = f" of ${acct['equity_usdt']:,.0f}" if acct else ""
        lines = [f"📐 {symbol} {direction.upper()} · levels (forward test, not validated yet)",
                 f"Market ≈ {fmt_px(px)} · 15m ATR {fmt_px(atr)} ({atr / px * 100:.2f}%)",
                 f"SL {fmt_px(sl)} · TP1 {fmt_px(tp1)} (1R) · TP2 {fmt_px(tp2)} (2R)",
                 *sizing_lines(px, sl, risk_usd, (risk_label + equity_txt) if risk_usd else None)]
        if acct_problem:
            lines.append(f"⚠️ {acct_problem}" + ("" if acct else f": sized at the fixed ${RISK_USD:.0f} fallback"))
        sp = plan.get("scanner_plan") or {}
        if sp.get("entry") is not None and sp.get("sl") is not None and sp.get("tp1") is not None:
            flag = ""
            if (px - float(sp["tp1"])) * sign >= 0:
                flag = " ⚠️ TP1 already passed"
            elif (px - float(sp["sl"])) * sign <= 0:
                flag = " ⚠️ price beyond the zone's stop"
            lines.append(f"Scanner zone: limit {fmt_px(sp['entry'])} · SL {fmt_px(sp['sl'])} · "
                         f"TP1 {fmt_px(sp['tp1'])}{flag}")
        lines += positions_lines(acct, direction)
        others = open_same_direction(direction)
        if others:
            lines.append(f"Test plans open {direction}: {', '.join(others)}")
        pct = stop_pct(px, sl)
        used = risk_usd or RISK_USD
        return {"at_utc": fmt_utc(utcnow()), "price": px, "atr15m": atr, "sl": sl, "tp1": tp1, "tp2": tp2,
                "stop_pct": pct, "risk_usd": used, "risk_pct": risk_pct if risk_usd else None,
                "equity_usdt": acct["equity_usdt"] if acct else None,
                "drawdown_pct": acct.get("drawdown_pct") if acct else None,
                "real_open_positions": len(acct.get("positions") or []) if acct else None,
                "safe_leverage": safe_leverage(pct),
                "notional_for_risk": used / ((pct + FEE_ROUNDTRIP_PCT) / 100),
                "open_same_direction": others, "text": "\n".join(lines)}
    except Exception as e:
        log(f"levels for {obs.get('symbol')} failed: {e}")
        return None


def send_levels(lv: dict | None) -> None:
    if LEVELS_TELEGRAM and lv:
        telegram_send(lv["text"])


# ------------------------------------------------------------------ running /tv

def sh(*args, timeout=60) -> str:
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def start_tradingview(max_wait_s=60) -> bool:
    """Start the service and wait until the chart page is reachable over CDP."""
    import urllib.request
    sh("systemctl", "start", "tradingview")
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(CDP_URL, timeout=3) as r:
                if "tradingview.com/chart" in r.read().decode():
                    time.sleep(3)  # let the chart finish loading
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def wait_tradingview_free(max_wait_s=600) -> bool:
    """Don't fight a manual /tv session for the single TradingView chart."""
    waited = 0
    while sh("systemctl", "is-active", "tradingview") == "active" and waited < max_wait_s:
        time.sleep(15)
        waited += 15
    return sh("systemctl", "is-active", "tradingview") != "active"


def collect_screenshots(pid: str, since: float) -> list[str]:
    """Move the chart screenshots taken during this run next to the plan, for later review."""
    dest = PLANS_DIR / "shots" / pid
    moved = []
    for pat in ("tv_chart_*.png", "tv_screenshot_*.png"):
        for f in Path(tempfile.gettempdir()).glob(pat):
            try:
                if f.stat().st_mtime >= since - 5:
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), dest / f.name)
                    moved.append(f"shots/{pid}/{f.name}")
                elif f.stat().st_mtime < time.time() - 2 * 86400:
                    f.unlink()  # stray old screenshot (e.g. manual /tv runs)
            except OSError:
                pass
    return sorted(moved)


def prune_shots() -> None:
    root = PLANS_DIR / "shots"
    if not root.exists():
        return
    cutoff = time.time() - SHOTS_KEEP_DAYS * 86400
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
        except OSError:
            pass


JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
REQUIRED = ("direction", "setup", "entry", "sl", "tp1", "grade")


def extract_plan(text: str) -> tuple[dict | None, str]:
    blocks = JSON_BLOCK.findall(text or "")
    if not blocks:
        return None, text
    try:
        plan = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None, text
    summary = JSON_BLOCK.sub("", text).strip()
    return plan, summary


def build_prompt(obs: dict) -> str:
    return (f"/tv {obs['symbol']} bias={obs['trade_bias']} MTF={obs['overall_score']} "
            f"alignment={obs['alignment_score']} macro={obs['macro_context']}")


def run_tv(obs: dict, pid: str) -> dict:
    """Returns {'tv': plan|None, 'summary': str, 'run': {...}}."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    run = {"ran": True, "prompt": build_prompt(obs), "started_at_utc": fmt_utc(utcnow()),
           "exit_code": None, "duration_s": None, "cost_usd": None, "error": None}
    if not wait_tradingview_free():
        run["error"] = "tradingview service busy (manual session?) for 10 min"
        return {"tv": None, "summary": "", "run": run}
    t0 = time.time()
    run["tradingview_ready"] = start_tradingview()
    cmd = [CLAUDE_BIN, "-p", run["prompt"], "--output-format", "json",
           "--permission-mode", "dontAsk", "--allowedTools", ALLOWED_TOOLS]
    try:
        p = subprocess.run(cmd, cwd=RUN_DIR, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
        run["exit_code"] = p.returncode
        stdout = p.stdout
    except subprocess.TimeoutExpired:
        run["error"] = f"timeout after {RUN_TIMEOUT_S}s"
        stdout = ""
    except OSError as e:
        run["error"] = f"cannot start claude: {e}"
        stdout = ""
    finally:
        sh("systemctl", "stop", "tradingview")  # always free the chart and the RAM
        run["duration_s"] = round(time.time() - t0, 1)
        run["screenshots"] = collect_screenshots(pid, t0)

    text = ""
    if stdout:
        try:
            res = json.loads(stdout)
            text = res.get("result") or ""
            run["cost_usd"] = res.get("total_cost_usd")
            if res.get("is_error"):
                run["error"] = run["error"] or f"claude reported error: {text[:300]}"
        except json.JSONDecodeError:
            run["error"] = run["error"] or f"unparseable claude output: {stdout[:300]}"
    elif not run["error"]:
        run["error"] = f"no output (exit {run['exit_code']})"

    tv, summary = extract_plan(text)
    if tv is None and not run["error"]:
        run["error"] = "no JSON plan block in /tv output"
    if tv is not None:
        missing = [k for k in REQUIRED if tv.get(k) in (None, "")]
        if missing:
            run["error"] = f"plan missing {missing}"
        want = direction_from_bias(obs["trade_bias"])
        if tv.get("direction") != want:
            run["error"] = f"plan direction {tv.get('direction')} != scanner bias {want}"
    if run["error"]:
        tv = None
    return {"tv": tv, "summary": summary, "run": run}


# ------------------------------------------------------------------ processing

def valid_obs(obs: dict) -> str | None:
    if not SYMBOL_RE.match(str(obs.get("symbol") or "")):
        return "bad symbol"
    if obs.get("trade_bias") not in ("bullish", "bearish"):
        return "bad bias"
    if obs.get("macro_context") and not WORD_RE.match(str(obs["macro_context"])):
        return "bad macro"
    for k in ("overall_score", "alignment_score"):
        if obs.get(k) is not None and not isinstance(obs[k], (int, float)):
            return f"bad {k}"
    return None


def process(obs: dict, st: dict, dry_run: bool) -> None:
    pid = plan_id(obs)
    path = PLANS_DIR / f"{pid}.json"
    tag = f"{obs['symbol']} {obs['trade_bias']} (run {obs['scan_run_id']}, obs {obs['id']})"
    bad = valid_obs(obs)
    if bad:
        log(f"skip {tag}: {bad}")
        return
    if path.exists():
        log(f"skip {tag}: plan exists")
        return
    dup = active_plan_for(obs["symbol"], obs["trade_bias"])
    if dup:
        log(f"skip {tag}: active plan {dup}")
        return
    if dry_run:
        age = (utcnow() - parse_utc(obs["observed_at"])).total_seconds() / 60
        note = f"  [would be scanner-only: {age:.0f} min old]" if age > MAX_AGE_MIN else ""
        if not RUN_CLAUDE:
            note += "  [/tv disabled: plan file only]"
        log(f"NEW {tag}  prompt: {build_prompt(obs)}{note}")
        return

    age_min = (utcnow() - parse_utc(obs["observed_at"])).total_seconds() / 60
    plan = base_plan(obs)
    if age_min > MAX_AGE_MIN:
        plan["run"] = {"ran": False, "skipped": f"stale: setup {age_min:.0f} min old"}
        save_json(path, plan)  # scanner leg is still tracked
        log(f"stale ({age_min:.0f} min), saved scanner-only {pid}")
        return
    lv = alert_levels(obs, plan)
    if lv:
        plan["alert_levels"] = {k: v for k, v in lv.items() if k != "text"}
    if not RUN_CLAUDE:
        plan["run"] = {"ran": False, "skipped": "tv disabled"}
        save_json(path, plan)  # scanner, scanner_market and rule legs are still tracked
        log(f"/tv disabled, saved {pid}")
        send_levels(lv)
        return
    if runs_today() >= DAILY_CAP:
        plan["run"] = {"ran": False, "skipped": "daily cap"}
        save_json(path, plan)  # scanner leg is still tracked
        log(f"cap reached, saved scanner-only {pid}")
        send_levels(lv)
        return
    send_levels(lv)  # before the 1-2 minute /tv run

    log(f"running /tv for {tag}")
    out = run_tv(obs, pid)
    plan["tv"], plan["run"] = out["tv"], out["run"]
    plan["created_at_utc"] = fmt_utc(utcnow())  # tv leg starts when the plan exists
    save_json(path, plan)
    r = out["run"]
    log(f"done {pid}: grade={(out['tv'] or {}).get('grade')} err={r['error']} "
        f"{r['duration_s']}s cost={r['cost_usd']}")

    if out["tv"] and out["tv"].get("grade") in SEND_GRADES:
        tv = out["tv"]
        head = (f"🧪 TV PLAN {tv['grade']} · {obs['symbol']} {tv['direction']} · {tv['setup']}\n"
                f"(experiment, not a trade signal)\n\n")
        telegram_send(head + plan_header(tv) + "\n\n" + out["summary"])
    elif r["error"]:
        alert(st, f"/tv failed for {obs['symbol']}: {r['error']}")


def tick(st: dict, dry_run: bool, since_run: int | None = None) -> None:
    con = db_connect()
    try:
        if since_run is not None:
            last = since_run
        elif "last_scan_run_id" not in st:
            st["last_scan_run_id"] = max_run_id(con)  # first start: no backfill
            log(f"first start: watching scan runs after {st['last_scan_run_id']}")
            if not dry_run:
                save_state(st)
            return
        else:
            last = st["last_scan_run_id"]
        runs = settled_runs_after(con, last)
        found = 0
        for run_id in runs:
            for obs in new_mtf_in_run(con, run_id):
                found += 1
                try:
                    process(obs, st, dry_run)
                except Exception as e:  # one bad setup must never block the queue
                    log(f"error on {obs.get('symbol')}: {e}\n{traceback.format_exc()}")
                    if not dry_run:
                        alert(st, f"crash while processing {obs.get('symbol')}: {e}")
            if not dry_run and since_run is None:
                st["last_scan_run_id"] = run_id
                save_state(st)
        if not dry_run and since_run is None:
            st["last_tick_at"] = fmt_utc(utcnow())
            save_state(st)
            prune_shots()
        if dry_run:
            span = f"{runs[0]}..{runs[-1]}" if runs else "none"
            log(f"dry-run: checked {len(runs)} settled scan runs ({span}), {found} new setups")
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since-run", type=int, help="with --dry-run: re-check detection from this scan run")
    a = ap.parse_args()
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    st = load_state()
    if a.since_run is not None and not a.dry_run:
        sys.exit("--since-run is only allowed with --dry-run")
    if not a.dry_run:
        # after a crash or reboot TradingView may still be running from an unfinished run
        sh("systemctl", "stop", "tradingview")
        log(f"tv-worker started (state: last scan run {st.get('last_scan_run_id')}, "
            f"/tv runs {'on' if RUN_CLAUDE else 'OFF'}, levels msg {'on' if LEVELS_TELEGRAM else 'OFF'}, "
            f"poll {POLL_S}s, settle {SETTLE_S}s)")
    while True:
        try:
            tick(st, a.dry_run, a.since_run)
        except sqlite3.Error as e:
            log(f"scanner db error: {e}")
            if not a.dry_run:
                alert(st, f"scanner database error: {e}")
        except Exception as e:  # never die silently: log, alert, retry next poll
            log(f"unexpected error: {e}\n{traceback.format_exc()}")
            if not a.dry_run:
                alert(st, f"unexpected error: {e}")
        if a.once or a.dry_run:
            break
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
