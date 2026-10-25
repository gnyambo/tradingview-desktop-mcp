# /tv pipeline

Scanner setup (SQLite) → tv-worker runs `claude -p "/tv ..."` → plan file in /root/tv_plans
→ tv-resolver (every 15 min) tracks the /tv plan AND the scanner's own levels on the same 15m candles
→ report.py compares them.

| file | role |
|---|---|
| ../commands/tv.md | the /tv command (always plans in the scanner's bias direction, grades with a fixed rubric) |
| tv_worker.py | detects NEW MTF-qualified setups (same rule as the Telegram alert), runs /tv one at a time, saves plans, Telegram for grades A/B/C |
| resolver.py | replays closed 15m candles: PENDING/WAITING → OPEN → LOSS/TP1/TP2/TIMEOUT, or EXPIRED/CANCELLED. Legs: tv, tv_market, scanner, scanner_market, rule_atr, rule_wide (see its docstring) |
| report.py | /tv vs scanner, /tv by grade, by 4h conflict, by rubric criterion, market entries, forward test verdict |
| common.py | shared helpers (stdlib only) |
| systemd/ | tv-worker.service, tv-resolver.service + .timer |
| tv-pipeline.env.example | copy to /etc/tv-pipeline.env |

Rules: same-candle SL+target counts as SL; on a limit's fill candle only SL is checked; TP1 is the result,
TP2-vs-SL tracked after; open trades time out at 72h; unentered plans expire after 24h; duplicates
(same symbol+bias while a /tv plan is live) are skipped; daily cap TV_DAILY_CAP (scanner leg still tracked).

Useful commands:
    set -a; . /etc/tv-pipeline.env; set +a
    python3 tv_worker.py --dry-run --since-run 4080   # which alerts would be picked up
    journalctl -u tv-worker -f
    python3 resolver.py --verbose
    python3 report.py

Forward test (fixed 2026-10-25, after /tv failed its 0.2R checkpoint on 50 trades):
alerts from 2026-10-26 00:00 UTC, scanner direction + market entry + the scanner's own SL/TP,
skipping alerts already past the scanner's TP1 ("stale"). PASS after 30 resolved fresh trades if
it averages >= +0.10R after fees, beats rule_wide on the same alerts, and includes >= 5 longs.
/tv runs are off for it: TV_RUN_CLAUDE=0 in /etc/tv-pipeline.env.
