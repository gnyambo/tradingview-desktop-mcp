#!/usr/bin/env bash
# tvstatus: forward-test results, pipeline health and the scanner's zone watch. Read-only.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
set -a; . /etc/tv-pipeline.env 2>/dev/null; set +a
python3 report.py --brief
echo
python3 heartbeat.py --print
echo
python3 - <<'PY'
import json, time
from common import price_now
try:
    w = json.load(open("/opt/binance-futures-scanner/zone_watch.json")).get("watches", {})
except Exception as e:
    print(f"zone watch: not readable yet ({e.__class__.__name__})"); raise SystemExit
now = time.time() * 1000
print(f"== zone watch: {len(w)} zones ==")
for s, x in sorted(w.items()):
    lo, hi, short = x["zone_low"], x["zone_high"], x["bias"] == "bearish"
    try:
        px = price_now(s)
    except Exception:
        px = None
    if not px:
        where = "price n/a"
    elif lo <= px <= hi:
        where = f"price {px:.6g} IN ZONE"
    elif (px > hi) if short else (px < lo):
        where = f"price {px:.6g} past the zone: a sweep, or broken if a {x['zone_timeframe']} candle closes there"
    else:
        gap = ((lo - px) if short else (px - hi)) / px * 100
        where = f"price {px:.6g}, zone {gap:.1f}% {'above' if short else 'below'}"
    print(f"{s:<14} {x['bias']:<8} {x['zone_timeframe']} zone {lo:.6g}-{hi:.6g} | {where} | "
          f"watched {(now - x['started_ms']) / 3.6e6:.0f}h | last MTF {(now - x['last_qualified_ms']) / 3.6e6:.0f}h ago"
          + (" | SIGNALLED" if x.get("signalled_ms") else ""))
PY
