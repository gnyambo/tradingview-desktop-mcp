# -*- coding: utf-8 -*-
"""Shared helpers for the /tv pipeline (worker, resolver, report). Standard library only."""
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLANS_DIR = Path(os.environ.get("TV_PLANS_DIR", "/root/tv_plans"))
FAPI = os.environ.get("BINANCE_FAPI", "https://fapi.binance.com")

LEG_FINAL = {"LOSS", "TP1", "TP2", "TIMEOUT", "EXPIRED", "CANCELLED", "INVALID", "NO_LEVELS", "STALE_LEVELS"}
WAITING_STATES = {"WAITING", "PENDING"}

# Your sizing (fixed margin x leverage) - used by the report and the Telegram risk line
NOTIONAL_USD = float(os.environ.get("TV_NOTIONAL_USD", "2000"))        # $200 margin x 10x
LEVERAGE = float(os.environ.get("TV_LEVERAGE", "10"))
MMR_PCT = float(os.environ.get("TV_MMR_PCT", "0.5"))                    # rough maintenance margin
WARN_STOP_PCT = float(os.environ.get("TV_WARN_STOP_PCT", "5"))
FEE_ROUNDTRIP_PCT = float(os.environ.get("TV_FEE_ROUNDTRIP_PCT", "0.10"))

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
WORD_RE = re.compile(r"^[A-Z_]{1,24}$")


# ------------------------------------------------------------------ time

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(s: str | None) -> datetime | None:
    """Accepts 'YYYY-MM-DD HH:MM' (UTC) or ISO 8601 with offset."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_utc(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") if dt else None


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ------------------------------------------------------------------ http

def http_json(url: str, params: dict | None = None, data: dict | None = None, timeout: int = 20):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "tv-pipeline/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def klines_15m(symbol: str, start: datetime, limit: int = 1000) -> list[dict]:
    """Closed 15m candles whose open time is >= start (Binance USDT-M perpetual)."""
    raw = http_json(f"{FAPI}/fapi/v1/klines",
                    {"symbol": symbol, "interval": "15m", "startTime": to_ms(start), "limit": limit})
    now_ms = to_ms(utcnow())
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "ct": int(k[6])}
            for k in raw if int(k[6]) < now_ms and int(k[0]) >= to_ms(start)]


def klines_15m_before(symbol: str, end: datetime, n: int = 16) -> list[dict]:
    """The last n closed 15m candles that closed before `end` (for ATR at that moment)."""
    raw = http_json(f"{FAPI}/fapi/v1/klines",
                    {"symbol": symbol, "interval": "15m", "endTime": to_ms(end) - 1, "limit": n})
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "ct": int(k[6])}
            for k in raw if int(k[6]) < to_ms(end)]


def atr14(candles: list[dict]) -> float | None:
    """Simple average of the last 14 true ranges (same definition as binance_swings atr14)."""
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"]))
           for p, c in zip(candles, candles[1:])]
    return sum(trs[-14:]) / len(trs[-14:]) if trs else None


def stop_pct(entry, sl) -> float | None:
    try:
        e, s = float(entry), float(sl)
        return abs(e - s) / e * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def risk_line(label: str, entry, sl) -> str | None:
    """'entry 1.1367: stop 6.8% = ~$136 at your sizing' plus a warning near liquidation."""
    pct = stop_pct(entry, sl)
    if pct is None:
        return None
    usd = NOTIONAL_USD * (pct + FEE_ROUNDTRIP_PCT) / 100
    liq = 100 / LEVERAGE - MMR_PCT
    line = f"{label}: stop {pct:.1f}% = ~${usd:.0f} at risk (${NOTIONAL_USD:.0f} notional)"
    if pct >= liq:
        line = f"⛔ {line} — BEYOND ~{liq:.1f}% liquidation at {LEVERAGE:.0f}x: you'd be liquidated before the stop"
    elif pct >= WARN_STOP_PCT:
        line = f"⚠️ {line} — wide stop, liquidation ~{liq:.1f}% away at {LEVERAGE:.0f}x"
    return line


def fmt_px(x) -> str:
    try:
        return f"{float(x):.6g}"
    except (TypeError, ValueError):
        return "-"


def rr_text(entry, sl, tp1, tp2) -> str:
    """R:R to TP1 / TP2 from this entry; flags levels that no longer make sense."""
    try:
        e, s = float(entry), float(sl)
        risk = abs(e - s)
        out = []
        for tp in (tp1, tp2):
            if tp is None:
                continue
            reward = (float(tp) - e) * (1 if s < e else -1)
            out.append(f"{reward / risk:.2f}" if reward > 0 else "past")
        return " / ".join(out) if out else "-"
    except (TypeError, ValueError, ZeroDivisionError):
        return "-"


def risk_text(entry, sl) -> str | None:
    pct = stop_pct(entry, sl)
    if pct is None:
        return None
    usd = NOTIONAL_USD * (pct + FEE_ROUNDTRIP_PCT) / 100
    liq = 100 / LEVERAGE - MMR_PCT
    txt = f"stop {pct:.1f}% = ~${usd:.0f} at risk"
    if pct >= liq:
        return f"⛔ {txt} — BEYOND ~{liq:.1f}% liquidation at {LEVERAGE:.0f}x"
    if pct >= WARN_STOP_PCT:
        return f"⚠️ {txt} — wide stop, liquidation ~{liq:.1f}% at {LEVERAGE:.0f}x"
    return txt


def plan_header(tv: dict) -> str:
    """Planned entry and at-market-now entry, each with SL/TP/R:R and $ risk, for the Telegram message."""
    sl, tp1, tp2 = tv.get("sl"), tv.get("tp1"), tv.get("tp2")
    levels = f"SL {fmt_px(sl)} · TP1 {fmt_px(tp1)}" + (f" · TP2 {fmt_px(tp2)}" if tp2 is not None else "")
    setup = tv.get("setup")
    entry = tv.get("entry") or {}
    trig = tv.get("trigger") or {}
    blocks = []
    if setup != "TAKE_NOW":
        if setup == "WAIT_FOR_TRIGGER":
            cond = f"15m close {'above' if trig.get('type') == 'close_above' else 'below'} {fmt_px(trig.get('price'))}"
            how = (f"limit {fmt_px(entry.get('price'))} after {cond}" if entry.get("type") == "limit"
                   else f"market on {cond}")
            e = entry.get("price") or trig.get("price")
        else:
            how, e = f"limit {fmt_px(entry.get('price'))}", entry.get("price")
        blocks.append(f"📍 Planned ({setup}): {how}\n   {levels} · R:R {rr_text(e, sl, tp1, tp2)}\n   "
                      f"{risk_text(e, sl) or ''}")
    m = tv.get("price_at_analysis") if setup != "TAKE_NOW" else (entry.get("price") or tv.get("price_at_analysis"))
    if m is not None:
        blocks.append(f"⚡ At market now: entry ~{fmt_px(m)}\n   {levels} · R:R {rr_text(m, sl, tp1, tp2)}\n   "
                      f"{risk_text(m, sl) or ''}")
    blocks.append(f"(analysed {tv.get('analysed_at_utc') or '?'} UTC · risk at ${NOTIONAL_USD:.0f} notional)")
    return "\n".join(blocks)


def telegram_send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    if not token or not chat:
        print("[telegram disabled] " + text.replace("\n", " | ")[:300], flush=True)
        return False
    try:
        http_json(f"https://api.telegram.org/bot{token}/sendMessage",
                  data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"})
        return True
    except Exception as e:  # never let a notification break the pipeline
        print(f"[telegram error] {e}", flush=True)
        return False


# ------------------------------------------------------------------ plan files

def save_json(path: Path, obj: dict) -> None:
    """Atomic write: never leaves a half-written plan file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def iter_plan_paths():
    if not PLANS_DIR.exists():
        return []
    return sorted(p for p in PLANS_DIR.glob("*.json") if not p.name.startswith("."))


def plan_is_final(plan: dict) -> bool:
    legs = plan.get("legs", {})
    return bool(legs) and all(l.get("final") for l in legs.values())


def direction_from_bias(bias: str) -> str | None:
    return {"bullish": "long", "bearish": "short"}.get((bias or "").lower())


def add_hours(dt: datetime, h: float) -> datetime:
    return dt + timedelta(hours=h)
