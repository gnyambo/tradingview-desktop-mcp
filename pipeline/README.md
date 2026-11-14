# /tv pipeline

Scanner setup (SQLite) → tv-worker runs `claude -p "/tv ..."` → plan file in /root/tv_plans
→ tv-resolver (every 15 min) tracks the /tv plan AND the scanner's own levels on the same 15m candles
→ report.py compares them.

| file | role |
|---|---|
| ../commands/tv.md | the /tv command (always plans in the scanner's bias direction, grades with a fixed rubric) |
| tv_worker.py | detects NEW MTF-qualified setups (same rule as the Telegram alert), runs /tv one at a time, saves plans, Telegram for grades A/B/C |
| resolver.py | replays closed 15m candles: PENDING/WAITING → OPEN → LOSS/TP1/TP2/TIMEOUT, or EXPIRED/CANCELLED. Legs: tv, tv_market, scanner, scanner_market, rule_atr, rule_wide (see its docstring) |
| report.py | /tv vs scanner, /tv by grade, by 4h conflict, by rubric criterion, market entries, forward test verdicts; `--brief` = forward tests only |
| step8_test.py | Step-8 forward test: NEW scanner ACTIONABLE signals → plan `<time>_<SYMBOL>_S8.json` with legs step8_s0/s1/s2 |
| heartbeat.py | daily health message: services, scanner, account, Step-8 signals, trades resolved in the last 24h, zone watch count |
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

Step-8 forward test (fixed 2026-11-09, after the Step-7 + ATR rule FAILED at +0.01R after fees on 36 trades,
and after finding that Step 8 almost never saw a zone retest because Step 7 dropped setups within ~4h):
the scanner now keeps watching a qualified zone for up to 96h (scanner zone_watch.py) and every NEW
ACTIONABLE signal is tracked three ways on the same signal:
  S0  scanner limit at the zone edge + scanner stop (zone + 0.15x 15m ATR)
  S1  same limit, stop zone far side + 0.5x ATR of the zone's timeframe (1h/4h)
  S2  market entry at the signal, stop beyond the sweep extreme of the last 16 15m candles + 0.25x 15m ATR
PASS per rule after 30 resolved trades: >= +0.10R after fees and >= 5 longs. Quick status:
    python3 report.py --brief
TV_STEP8_TEST=0 stops new Step-8 plans; TV_STEP8_TELEGRAM=0 stops the "🧪 STEP-8 SIGNAL" message.

Shadow group (from 2026-11-14, information only, no pass mark): complete Step-8 sequences the scanner
rejected ONLY for the 15m volume trigger and/or the RR minimum (scanner field shadow_sequence) are saved
silently as `<time>_<SYMBOL>_S8X.json` with the same three legs, to measure whether those two gates
remove losers or winners. It cannot become a tradeable rule without a fresh test of its own.

Rule wide @ market (Step-7 alert, market entry, 3x ATR stop, 1R target):
  Test 1 (2026-10-26 to 2026-11-09) FAILED at its 30-trade mark: +0.01R ± 0.16 after fees. Its result is
    frozen in the report; the running average crossing the mark afterwards is optional stopping.
  Test 2 from 2026-11-14 10:00 UTC, registered before its trades existed: PASS = the FIRST 60 resolved
    trades average >= +0.10R after fees with >= 10 longs. Trades between the two tests are shown but
    count in neither. Verdicts are judged on the first N resolved trades only and never move afterwards.

Earlier forward test (fixed 2026-10-25, after /tv failed its 0.2R checkpoint on 50 trades; rule_wide FAILED 2026-11-09):
alerts from 2026-10-26 00:00 UTC, scanner direction + market entry + the scanner's own SL/TP,
skipping alerts already past the scanner's TP1 ("stale"). PASS after 30 resolved fresh trades if
it averages >= +0.10R after fees, beats rule_wide on the same alerts, and includes >= 5 longs.
/tv runs are off for it: TV_RUN_CLAUDE=0 in /etc/tv-pipeline.env.

Market legs are booked at the first 1m open after the alert (followed on 1m candles to the next 15m
boundary, then 15m), not the next 15m open. Each alert also gets a "📐 levels" Telegram message about
10-20 s after the scanner's alert (rule_wide SL/TP, $ risk, scanner zone with a stale flag, open
same-direction plans); TV_LEVELS_TELEGRAM=0 turns it off.

Sizing: the levels message sizes each trade at TV_RISK_PCT (default 1%) of the futures wallet balance,
read read-only from the crypto-risk-app Postgres (account.py, via docker exec psql in a READ ONLY
transaction). Drawdown is transfer-adjusted (a withdrawal is not a loss); at TV_DRAWDOWN_HALVE_PCT below
the 30-day peak the risk is halved. Your real open positions are listed against TV_MAX_OPEN /
TV_MAX_SAME_DIRECTION. If the database is unreachable the last good balance is used, then TV_RISK_USD.
