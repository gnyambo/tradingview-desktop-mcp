---
description: Turn a scanner setup into a Step-8 plan (trigger, entry, SL, TP) or review given levels, using Binance data + TradingView 1D/4h/1h/15m
argument-hint: SYMBOL bias=bullish|bearish [MTF=.. macro=..]   or   SYMBOL LONG|SHORT entry=.. sl=.. tp=..
allowed-tools: mcp__binance, mcp__tradingview, Bash(systemctl start tradingview), Bash(systemctl stop tradingview), Bash(systemctl is-active tradingview)
---

Input: $ARGUMENTS

The first word is the Binance USDT-M symbol (SOLUSDT, or BINANCE:SOLUSDT.P). Decide the mode from the rest:
- **Plan mode**: no entry/sl/tp given (e.g. `bias=bullish MTF=81.75 macro=SUPPORTIVE`, straight from a scanner setup alert).
  You act as the Step-8 15m confirmation: first test whether the bias holds, then define a mechanical trigger and the levels.
- **Review mode**: entry/sl/tp given. Check those levels.

In both modes your job is to try to REJECT the idea. MTF/macro values from the scanner are context, not evidence.

## 1. Exact numbers first (binance tools, no TradingView needed)
- `binance_market` for the symbol.
- `binance_swings` on: 1d (strength 3, limit 300), 4h (strength 5, limit 300), 1h (strength 5, limit 300), 15m (strength 3, limit 300).
- Every price you quote (trigger, entry, SL, TP, invalidation) must come from these results, never from reading a screenshot.

## 2. Pictures (TradingView)
- `systemctl start tradingview`, wait until `tv_status` responds (up to ~30s). Never call `tv_launch`.
- Check the chart timezone with `tv_eval_js` `TradingViewApi.activeChart().getTimezone()`. If it is not `Etc/UTC`, run `TradingViewApi.activeChart().setTimezone("Etc/UTC")` and re-check. If that fails, say so and remember the chart is UTC+3.
- `tv_set_symbol` BINANCE:<SYMBOL>.P, then for each of 1D, 240, 60, 15:
  `tv_set_resolution` → reset the view (`tv_reset_view` if it exists, else `tv_eval_js` `TradingViewApi.activeChart().executeActionById("chartReset")`) → `tv_close_popups` → `tv_screenshot_chart`.
- `systemctl stop tradingview` when the four screenshots are done, even if something failed.

## 3. Analysis (top-down)
- 1D and 4h: trend and bias; last BOS/CHoCH with exact price and time; premium or discount of the current dealing range.
  In plan mode: if 1D/4h contradict the scanner's bias, the verdict is SKIP. Say why in one line.
- 1h: aligned with 4h or pulling back against it? Nearest untested POI (order block / FVG) with exact bounds.
- 15m: has the confirmation already happened (CHoCH/BOS in the bias direction)? Liquidity just above/below price.
- Screenshots confirm the shape of structure; numbers give the levels. If they disagree, say so and trust the numbers.

## 4. Levels
- **Trigger** (plan mode): ONE condition a script can check from closed 15m candles: "15m close above P" or "15m close below P", where P is an exact swing level (usually the 15m swing that must break for a CHoCH/BOS in the bias direction). If the confirmation already happened and price is still near a valid entry, say TAKE_NOW instead.
- **Entry**: either `market_on_trigger` (price of the trigger close) or `limit` at an exact POI level.
- **SL**: beyond the swing that invalidates the idea (exact swing price) plus a buffer. Flag it if SL distance < 1.0 × the 15m atr14 (my scanner has had a tight-stop defect) or if it sits inside obvious liquidity.
- **TP1 / TP2**: next opposing liquidity / swing levels on 1h and 4h. Give R:R for each.
- **Invalidation before trigger** (plan mode): the price that cancels the plan if hit first (e.g. a 15m close beyond the SL side).
- **Valid until**: when the plan expires if not triggered (max 24h from now).
- Review mode: compare the given entry/SL/TP with yours and say plainly which are weak and why.

## 5. Output (I read this on a phone: under ~35 lines, no long prose)
1. Verdict: TAKE_NOW / WAIT_FOR_TRIGGER / SKIP, and the direction.
2. One line per timeframe: 1D, 4h, 1h, 15m.
3. Plan: Trigger | Entry | SL | TP1 | TP2 | R:R, each price with its source (timeframe + swing time, UTC).
4. Cancel if: the invalidation-before-trigger price, and the expiry time.
5. Best argument AGAINST the trade, in one or two lines.

Then end with exactly one fenced ```json block (for logging; use null where not applicable, numbers as numbers):
{"symbol":"","mode":"plan|review","verdict":"TAKE_NOW|WAIT_FOR_TRIGGER|SKIP","direction":"long|short|null",
 "trigger":{"timeframe":"15m","type":"close_above|close_below","price":0},
 "entry":{"type":"market_on_trigger|limit","price":0},"sl":0,"tp1":0,"tp2":0,"rr1":0,"rr2":0,
 "cancel_if":{"type":"close_above|close_below","price":0},"valid_until_utc":"YYYY-MM-DD HH:MM",
 "atr15m":0,"price_at_analysis":0,"analysed_at_utc":"YYYY-MM-DD HH:MM","scanner":{"bias":"","mtf":null,"macro":""}}

All times UTC. This is analysis for me to decide on, not an instruction to trade.
