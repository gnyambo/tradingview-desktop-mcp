# -*- coding: utf-8 -*-
"""
Account state for position sizing, read from the crypto-risk-app database (read-only).

The dashboard already polls Binance every 60 s and stores what we need in its Postgres:
  equity_snapshots  wallet balance (the app's equity definition: no unrealized P&L, so a
                    winning open trade does not inflate the next position size)
  transfers         deposits/withdrawals, so a withdrawal is not mistaken for a drawdown
  live_positions    open positions with side and current risk

Queries run through `docker exec <postgres container> psql` (no driver, no password: the
container's local socket) inside a READ ONLY transaction. Results are cached in
PLANS_DIR/.account_cache.json; if the database is unreachable the last good result is used and
the message says how old it is.
"""
import json
import os
import subprocess

from common import PLANS_DIR, parse_utc, save_json, utcnow

PG_CONTAINER = os.environ.get("TV_RISK_PG_CONTAINER", "crypto-risk-app-postgres-1")
PG_DB = os.environ.get("TV_RISK_PG_DB", "crypto_risk")
PG_USER = os.environ.get("TV_RISK_PG_USER", "crypto_risk_app")
RISK_PCT = float(os.environ.get("TV_RISK_PCT", "1"))
DRAWDOWN_HALVE_PCT = float(os.environ.get("TV_DRAWDOWN_HALVE_PCT", "10"))  # 0 = off
DRAWDOWN_WINDOW_DAYS = int(os.environ.get("TV_DRAWDOWN_WINDOW_DAYS", "30"))
MAX_OPEN = int(os.environ.get("TV_MAX_OPEN", "3"))
MAX_SAME_DIRECTION = int(os.environ.get("TV_MAX_SAME_DIRECTION", "2"))
CACHE_PATH = PLANS_DIR / ".account_cache.json"
CACHE_TTL_S = 300
STALE_SNAPSHOT_MIN = 10  # the app writes a snapshot every 60 s; older than this = app not updating

# One row: latest wallet equity, its time, and the transfer-adjusted peak / current over the window.
EQUITY_SQL = f"""
WITH w AS (SELECT now() - interval '{DRAWDOWN_WINDOW_DAYS} days' AS s),
t AS (SELECT transferred_at,
             CASE WHEN transfer_type = 'DEPOSIT' THEN amount_usdt ELSE -amount_usdt END AS d
      FROM transfers, w WHERE transferred_at >= w.s),
e AS (SELECT e0.snapshot_at,
             e0.equity_usdt - COALESCE((SELECT SUM(d) FROM t WHERE t.transferred_at <= e0.snapshot_at), 0) AS adj
      FROM equity_snapshots e0, w WHERE e0.snapshot_at >= w.s)
SELECT (SELECT equity_usdt FROM equity_snapshots ORDER BY snapshot_at DESC LIMIT 1),
       (SELECT to_char(snapshot_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
          FROM equity_snapshots ORDER BY snapshot_at DESC LIMIT 1),
       (SELECT MAX(adj) FROM e),
       (SELECT adj FROM e ORDER BY snapshot_at DESC LIMIT 1);
"""
POSITIONS_SQL = """
SELECT symbol, side, COALESCE(current_risk_usdt, 0), has_stop_loss FROM live_positions ORDER BY symbol;
"""


def _psql(sql: str, timeout: int = 20) -> list[list[str]]:
    cmd = ["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", PG_CONTAINER,
           "psql", "-U", PG_USER, "-d", PG_DB, "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300])
    return [line.split("|") for line in p.stdout.splitlines() if line.strip()]


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_account() -> dict:
    """Fresh read from the dashboard's database. Raises on failure."""
    row = _psql(EQUITY_SQL)[0]
    equity, snap_at, peak_adj, adj_now = _num(row[0]), row[1] or None, _num(row[2]), _num(row[3])
    if not equity or equity <= 0:
        raise RuntimeError("no equity snapshot in the dashboard database")
    positions = []
    for sym, side, risk, has_stop in _psql(POSITIONS_SQL):
        positions.append({"symbol": sym, "side": side.lower(), "risk_usdt": _num(risk) or 0.0,
                          "has_stop": has_stop.strip().lower() in ("t", "true", "1")})
    drawdown_pct = None
    if peak_adj and adj_now is not None and peak_adj > 0:
        drawdown_pct = max(0.0, (peak_adj - adj_now) / peak_adj * 100)
    return {"fetched_at": utcnow().strftime("%Y-%m-%d %H:%M:%S"), "equity_usdt": equity,
            "snapshot_at_utc": snap_at, "drawdown_pct": drawdown_pct, "positions": positions}


def get_account(max_age_s: int = CACHE_TTL_S) -> tuple[dict | None, str | None]:
    """(account, problem). Uses the cache when it is fresh; on a failed read falls back to the
    last good result and returns a short problem text for the message."""
    cached = None
    try:
        cached = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        pass
    if cached:
        age = (utcnow() - parse_utc(cached["fetched_at"])).total_seconds()
        if age <= max_age_s:
            return cached, _staleness(cached)
    try:
        acct = fetch_account()
        save_json(CACHE_PATH, acct)
        return acct, _staleness(acct)
    except Exception as e:
        if cached:
            age_min = (utcnow() - parse_utc(cached["fetched_at"])).total_seconds() / 60
            return cached, f"dashboard unreachable, balance from {age_min:.0f} min ago ({str(e)[:60]})"
        return None, f"dashboard unreachable ({str(e)[:80]})"


def _staleness(acct: dict) -> str | None:
    t = parse_utc(acct.get("snapshot_at_utc"))
    if t is None:
        return "no snapshot time"
    age_min = (utcnow() - t).total_seconds() / 60
    return f"dashboard balance is {age_min:.0f} min old" if age_min > STALE_SNAPSHOT_MIN else None


def risk_budget(acct: dict | None) -> tuple[float | None, float, str]:
    """($ to risk, % used, label). None $ when the account is unknown."""
    if not acct:
        return None, RISK_PCT, f"{RISK_PCT:g}% risk"
    pct, label = RISK_PCT, f"{RISK_PCT:g}% risk"
    dd = acct.get("drawdown_pct")
    if DRAWDOWN_HALVE_PCT > 0 and dd is not None and dd >= DRAWDOWN_HALVE_PCT:
        pct = RISK_PCT / 2
        label = (f"{pct:g}% risk (halved: account {dd:.0f}% below its {DRAWDOWN_WINDOW_DAYS}-day peak)")
    return acct["equity_usdt"] * pct / 100, pct, label


def positions_lines(acct: dict | None, direction: str) -> list[str]:
    """Your real open positions and the caps (TV_MAX_OPEN, TV_MAX_SAME_DIRECTION)."""
    if not acct:
        return []
    pos = acct.get("positions") or []
    equity = acct["equity_usdt"]
    if not pos:
        return ["Your open positions: none"]
    same = [p for p in pos if p["side"] == direction]
    risk = sum(p["risk_usdt"] for p in pos)
    names = ", ".join(f"{p['symbol'].removesuffix('USDT')} {p['side']}" + ("" if p["has_stop"] else " ⚠️no stop")
                      for p in pos)
    lines = [f"Your open positions ({len(pos)}): {names} · at risk ${risk:,.0f} ({risk / equity * 100:.1f}%)"]
    if len(same) >= MAX_SAME_DIRECTION:
        lines.append(f"⛔ already {len(same)} {direction} open: your cap is {MAX_SAME_DIRECTION} same-direction")
    elif len(pos) >= MAX_OPEN:
        lines.append(f"⛔ already {len(pos)} positions open: your cap is {MAX_OPEN}")
    return lines
