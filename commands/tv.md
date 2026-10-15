---
description: Build and grade a Step-8 setup in the scanner's bias direction (or review given levels) from Binance data + TradingView 1D/4h/1h/15m
argument-hint: SYMBOL bias=bullish|bearish [MTF=.. alignment=.. macro=..]   or   SYMBOL LONG|SHORT entry=.. sl=.. tp=..
allowed-tools: mcp__binance, mcp__tradingview, Bash(systemctl start tradingview), Bash(systemctl stop tradingview)
---

Input: $ARGUMENTS

The first word is the Binance USDT-M symbol (SOLUSDT, or BINANCE:SOLUSDT.P). Decide the mode from the rest:
- **Plan mode**: a bias is given and no entry/sl/tp (this is how the scanner pipeline calls you).
  ALWAYS build a setup in the scanner's bias direction (bullish = long, bearish = short), even when the chart argues
  against it. The grade, not a refusal, carries your judgement. Grade strictly; never round up.
- **Review mode**: entry/sl/tp given. Check those levels and grade them with the same rubric.

MTF/alignment/macro values from the scanner are context, not evidence.

## 1. Exact numbers first (binance tools)
- `binance_market` for the symbol.
- `binance_swings` on: 1d (strength 3, limit 300), 4h (strength 5, limit 300), 1h (strength 5, limit 300), 15m (strength 3, limit 300).
- Every price you output (trigger, entry, SL, TP, cancel) must come from these results, never from reading a screenshot.

## 2. Pictures (TradingView)
- Call `tv_status` first. If it responds, TradingView is already running (the scanner pipeline starts it for you):
  do NOT start or stop it, and do not use the shell for it.
- Only if it is not running and you have shell access: run exactly `systemctl start tradingview` (nothing else on
  the command line), wait until `tv_status` responds (up to ~30s), and run exactly `systemctl stop tradingview` at the end.
  Never call `tv_launch`.
- Check the chart timezone with `tv_eval_js` `TradingViewApi.activeChart().getTimezone()`. If it is not `Etc/UTC`, run `TradingViewApi.activeChart().setTimezone("Etc/UTC")` and re-check.
- `tv_set_symbol` BINANCE:<SYMBOL>.P, then for each of 1D, 240, 60, 15:
  `tv_set_resolution` → reset the view (`tv_reset_view` if it exists, else `tv_eval_js` `TradingViewApi.activeChart().executeActionById("chartReset")`) → `tv_close_popups` → `tv_screenshot_chart`.
- If TradingView fails at any point, continue from the numbers alone and set `"charts": false`. Never stop without a plan.

## 3. Analysis (top-down)
- 1D and 4h: trend; last BOS/CHoCH with exact price and time; premium/discount of the current 4h dealing range.
- 4h counts as AGAINST a long if it broke its last HL and has not closed back above the last LH since (mirror for shorts). Then `htf_conflict` is true.
- 1h: aligned with 4h or pulling back? Nearest untested POI (order block / FVG) with exact bounds.
- 15m: has a BOS/CHoCH in the bias direction already closed? Liquidity just above/below price.
- Screenshots confirm the shape of structure; numbers give the levels. If they disagree, say so and trust the numbers.

## 4. The setup
Pick exactly one:
- `TAKE_NOW`: 15m confirmation already closed and price is at a valid entry now. Entry type `market_now`.
- `PLACE_LIMIT`: confirmation closed, enter on a pullback. Entry type `limit` at an exact POI / swing level.
- `WAIT_FOR_TRIGGER`: confirmation not yet. Trigger = ONE condition on a closed 15m candle: `close_above` or `close_below` an exact swing price. Entry type `market_on_trigger` (enter at that close) or `limit` (a limit placed only after the trigger).
  `TAKE_NOW` is not allowed when `htf_conflict` is true.

Levels:
- SL: beyond the swing that invalidates the idea (exact swing price) plus a buffer.
- TP1 / TP2: next opposing liquidity / swing levels on 1h and 4h. R:R for each from the entry.
- `cancel_if` (for PLACE_LIMIT and WAIT_FOR_TRIGGER): a 15m close that cancels the plan before entry (e.g. close beyond TP1 = move left without you; close beyond SL side = idea broken). Use null if none.
- `valid_until_utc`: expiry if not entered, at most 24h from now.
- If you quote R:R for any entry you are not recommending, state the SL you assumed.

## 5. Grade (fixed rubric, score each 0-2)
| key | 2 | 1 | 0 |
|---|---|---|---|
| d1_alignment | 1D trending with bias | ranging | against |
| h4_alignment | 4h with bias | ranging | against (htf_conflict) |
| location | entry in deep discount (long) / premium (short) of the 4h range | middle | wrong side |
| confirmation_15m | BOS/CHoCH already closed | trigger defined, not yet | none |
| rr_tp1 | >= 2 | 1.5 to 2 | < 1.5 |
| stop_quality | >= 1x 15m atr14 beyond a real swing AND outside obvious liquidity | one fails | both fail |
Score = sum (0-12). Grade: A = 10-12, B = 7-9, C = 4-6, D = 0-3.

## 6. Output (read on a phone: under ~35 lines)
1. `GRADE X (score/12) · SETUP · long|short`
2. One line per timeframe: 1D, 4h, 1h, 15m.
3. Plan: Trigger | Entry | SL | TP1 | TP2 | R:R, each price with its source (timeframe + swing time, UTC).
4. Cancel if / valid until.
5. Best argument AGAINST the trade, in one or two lines.

Then end with exactly one fenced ```json block, numbers as numbers, null where not applicable:
{"symbol":"","mode":"plan|review","direction":"long|short","setup":"TAKE_NOW|PLACE_LIMIT|WAIT_FOR_TRIGGER",
 "trigger":{"timeframe":"15m","type":"close_above|close_below","price":0},
 "entry":{"type":"market_now|limit|market_on_trigger","price":0},"sl":0,"tp1":0,"tp2":0,"rr1":0,"rr2":0,
 "cancel_if":{"type":"close_above|close_below","price":0},"valid_until_utc":"YYYY-MM-DD HH:MM",
 "grade":"A|B|C|D","score":0,
 "criteria":{"d1_alignment":0,"h4_alignment":0,"location":0,"confirmation_15m":0,"rr_tp1":0,"stop_quality":0},
 "htf_conflict":false,"charts":true,"atr15m":0,"price_at_analysis":0,"analysed_at_utc":"YYYY-MM-DD HH:MM",
 "scanner":{"bias":"","mtf":null,"alignment":null,"macro":""}}

All times UTC. This is analysis for me to decide on, not an instruction to trade.
