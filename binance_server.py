# -*- coding: utf-8 -*-
"""
MCP server for Binance USDT-M futures public market data (no API key needed).

Gives exact numbers to go with TradingView screenshots: screenshots are for
visual structure, these tools are for precise prices and times.
Works without TradingView running.

Endpoints: public https://fapi.binance.com (override with BINANCE_FAPI).
All times are UTC ("YYYY-MM-DD HH:MM", candle open time).
"""
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

FAPI = os.environ.get("BINANCE_FAPI", "https://fapi.binance.com")
VALID_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
                   "8h", "12h", "1d", "3d", "1w", "1M"}

mcp = FastMCP("binance")
_session = requests.Session()


# ------------------------------------------------------------------ helpers

def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _get(path: str, params: dict) -> Any:
    r = _session.get(f"{FAPI}{path}", params=params, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Binance {path} HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _symbol(s: str) -> str:
    """Accept 'BTCUSDT', 'btcusdt', 'BINANCE:BTCUSDT.P' -> 'BTCUSDT'."""
    s = s.upper().strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith(".P"):
        s = s[:-2]
    return s


def _interval(i: str) -> str:
    i = i.strip()
    aliases = {"15": "15m", "60": "1h", "240": "4h", "D": "1d", "1D": "1d", "W": "1w", "1W": "1w"}
    i = aliases.get(i, i)
    if i not in VALID_INTERVALS:
        raise ValueError(f"interval must be one of {sorted(VALID_INTERVALS)}")
    return i


def _t(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _klines(symbol: str, interval: str, limit: int) -> list[dict]:
    raw = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    now = int(time.time() * 1000)
    return [{
        "t": row[0], "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
        "c": float(row[4]), "v": float(row[5]), "closed": row[6] < now,
    } for row in raw]


def _find_swings(bars: list[dict], strength: int) -> tuple[list[dict], list[dict]]:
    """Mechanical fractal pivots on closed bars: a swing high is a bar whose high is
    above the `strength` bars before it and not exceeded by the `strength` bars after it
    (mirror for lows). Needs `strength` closed bars to the right to confirm."""
    closed = [b for b in bars if b["closed"]]
    highs, lows = [], []
    for i in range(strength, len(closed) - strength):
        left, right = closed[i - strength:i], closed[i + 1:i + 1 + strength]
        b = closed[i]
        if b["h"] > max(x["h"] for x in left) and b["h"] >= max(x["h"] for x in right):
            highs.append({"time": _t(b["t"]), "price": b["h"]})
        if b["l"] < min(x["l"] for x in left) and b["l"] <= min(x["l"] for x in right):
            lows.append({"time": _t(b["t"]), "price": b["l"]})
    for seq, up, down in ((highs, "HH", "LH"), (lows, "HL", "LL")):
        for j in range(1, len(seq)):
            seq[j]["label"] = up if seq[j]["price"] > seq[j - 1]["price"] else down
    return highs, lows


# -------------------------------------------------------------------- tools

@mcp.tool()
def binance_klines(symbol: str, interval: str = "15m", limit: int = 150) -> str:
    """Exact OHLCV candles for a Binance USDT-M perpetual (e.g. 'BTCUSDT' or
    'BINANCE:BTCUSDT.P'). interval: 1m,5m,15m,30m,1h,2h,4h,1d,1w... limit max 500.
    Rows are [open_time_utc, open, high, low, close, volume]; the last row may be
    the still-forming candle (see last_candle_closed)."""
    sym, iv = _symbol(symbol), _interval(interval)
    bars = _klines(sym, iv, max(1, min(int(limit), 500)))
    return _ok({
        "symbol": sym, "interval": iv, "tz": "UTC",
        "columns": ["time", "open", "high", "low", "close", "volume"],
        "rows": [[_t(b["t"]), b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars],
        "last_candle_closed": bars[-1]["closed"] if bars else None,
    })


@mcp.tool()
def binance_swings(symbol: str, interval: str = "15m", limit: int = 300,
                   strength: int = 3, keep: int = 8) -> str:
    """Exact recent swing highs/lows (fractal pivots) with HH/HL/LH/LL labels, plus
    the range high/low and current price. Use these numbers for levels instead of
    reading prices off a screenshot. strength = bars each side needed to confirm a
    swing (higher = fewer, more significant swings). This is a mechanical definition,
    not a full SMC structure read."""
    sym, iv = _symbol(symbol), _interval(interval)
    bars = _klines(sym, iv, max(2 * strength + 2, min(int(limit), 1500)))
    highs, lows = _find_swings(bars, max(1, int(strength)))
    hi = max(bars, key=lambda b: b["h"])
    lo = min(bars, key=lambda b: b["l"])
    return _ok({
        "symbol": sym, "interval": iv, "tz": "UTC", "bars_scanned": len(bars),
        "from": _t(bars[0]["t"]), "to": _t(bars[-1]["t"]),
        "last_price": bars[-1]["c"],
        "range_high": {"time": _t(hi["t"]), "price": hi["h"]},
        "range_low": {"time": _t(lo["t"]), "price": lo["l"]},
        "swing_highs": highs[-keep:], "swing_lows": lows[-keep:],
        "note": "swings use closed candles only; the newest may be unconfirmed until "
                f"{strength} more bars close",
    })


@mcp.tool()
def binance_market(symbol: str) -> str:
    """Current market snapshot for a Binance USDT-M perpetual: last and mark price,
    24h stats, funding rate and next funding time, open interest."""
    sym = _symbol(symbol)
    tick = _get("/fapi/v1/ticker/24hr", {"symbol": sym})
    prem = _get("/fapi/v1/premiumIndex", {"symbol": sym})
    oi = _get("/fapi/v1/openInterest", {"symbol": sym})
    return _ok({
        "symbol": sym, "tz": "UTC",
        "last_price": float(tick["lastPrice"]),
        "mark_price": float(prem["markPrice"]),
        "index_price": float(prem["indexPrice"]),
        "change_24h_pct": float(tick["priceChangePercent"]),
        "high_24h": float(tick["highPrice"]), "low_24h": float(tick["lowPrice"]),
        "quote_volume_24h": float(tick["quoteVolume"]),
        "funding_rate": float(prem["lastFundingRate"]),
        "next_funding": _t(int(prem["nextFundingTime"])),
        "open_interest": float(oi["openInterest"]),
    })


if __name__ == "__main__":
    mcp.run()
