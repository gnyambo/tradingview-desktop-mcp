# tradingview-desktop-mcp

Control the **TradingView Desktop app** (Windows) from any MCP-compatible AI agent — Claude Code, Claude Desktop, OpenAI Codex CLI, Cursor, and others. Includes an optional **MetaTrader 5 MCP server** and a **signal bridge** that mirrors chart indicator signals as MT5 orders.

> Ask your agent things like *"switch the chart to BTCUSDT on 15m and show me a screenshot"*, *"add an RSI"*, *"write this Pine strategy and run it"*, or *"what does the Strategy Tester report say?"* — and watch it happen in the real TradingView app.

## How it works

TradingView Desktop is an Electron app. Launched with `--remote-debugging-port=9222`, it exposes the Chrome DevTools Protocol (CDP). The chart page contains TradingView's full internal Charting Library API (`window.TradingViewApi`), which these servers drive over a local websocket:

```
┌──────────────┐   MCP (stdio)   ┌─────────────┐   CDP ws://127.0.0.1:9222   ┌─────────────────┐
│ Claude/Codex │ ◄─────────────► │  server.py  │ ◄─────────────────────────► │ TradingView app │
└──────────────┘                 └─────────────┘                             └─────────────────┘
┌──────────────┐   MCP (stdio)   ┌──────────────┐      MetaTrader5 pkg       ┌─────────────────┐
│ Claude/Codex │ ◄─────────────► │ mt5_server.py│ ◄────────────────────────► │  MT5 terminal   │
└──────────────┘                 └──────────────┘                            └─────────────────┘
```

No TradingView account API, no scraping, no browser extension — it talks to the app you already have open, with your session and your saved charts.

## Requirements

- **Windows 10/11** (TradingView Desktop + the `MetaTrader5` package are Windows-only)
- **TradingView Desktop** (Microsoft Store or installer version — both auto-detected)
- **Python 3.10+**
- For the MT5 parts: any **MT5 desktop terminal** (MetaQuotes, Pepperstone, IC Markets, …) installed and logged in. The MT5 **web terminal and mobile apps are not supported** — they have no local API.

## Install

```bash
git clone https://github.com/Unjoselo/tradingview-desktop-mcp
cd tradingview-desktop-mcp
pip install -r requirements.txt
```

## Register the servers in your client

### Claude Code

```bash
claude mcp add tradingview -- python C:/path/to/tradingview-desktop-mcp/server.py
claude mcp add mt5         -- python C:/path/to/tradingview-desktop-mcp/mt5_server.py
```

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tradingview": {
      "command": "python",
      "args": ["C:/path/to/tradingview-desktop-mcp/server.py"]
    },
    "mt5": {
      "command": "python",
      "args": ["C:/path/to/tradingview-desktop-mcp/mt5_server.py"]
    }
  }
}
```

### OpenAI Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.tradingview]
command = "python"
args = ["C:/path/to/tradingview-desktop-mcp/server.py"]

[mcp_servers.mt5]
command = "python"
args = ["C:/path/to/tradingview-desktop-mcp/mt5_server.py"]
```

### Cursor

`.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` globally) — same JSON shape as Claude Desktop.

## TradingView tools (`server.py`)

| Tool | Description |
|---|---|
| `tv_status` | App running? CDP connected? Current symbol/timeframe/layout |
| `tv_launch` | Start TradingView with the CDP port (detects the no-port case) |
| `tv_screenshot` | PNG of the whole window (returned as image + saved file path) |
| `tv_screenshot_chart` | PNG of the chart area only; resets the view first |
| `tv_reset_view` | Reset zoom and autoscale the price axis, then close popups |
| `tv_get_chart` | Symbol, resolution, chart type, visible range, studies |
| `tv_set_symbol` | Change symbol (`OANDA:XAUUSD`, `BINANCE:BTCUSDT`, `NASDAQ:AAPL`…) |
| `tv_set_resolution` | Change timeframe (`1`, `5`, `15`, `60`, `240`, `1D`, `1W`…) |
| `tv_list_studies` | Indicators/strategies on the chart (id + name) |
| `tv_add_study` / `tv_remove_study` | Add built-in indicators by name / remove by id |
| `tv_strategy_report` | Strategy Tester report of the active strategy |
| `tv_pine_open_editor` / `tv_pine_set_script` / `tv_pine_add_to_chart` | Write Pine Script code and compile it onto the chart |
| `tv_close_popups` | Dismiss plan-limit/promo dialogs |
| `tv_eval_js` | Run arbitrary JS in the chart page (escape hatch — `TradingViewApi` is all yours) |

Environment variables: `TV_CDP_PORT` (default 9222), `TV_EXE_PATH` (default: auto-discover Store/installer paths).

## MetaTrader 5 tools (`mt5_server.py`)

| Tool | Description |
|---|---|
| `mt5_status` | Server, login, demo/real, balance, equity, margin |
| `mt5_symbol_info` | Bid/ask, spread, contract size, lot limits |
| `mt5_candles` | Last N OHLCV candles (M1…MN1) — last candle is still forming |
| `mt5_positions` | Open positions, filterable by symbol/magic |
| `mt5_order_open` | Market order with optional SL/TP — **demo-only by default** |
| `mt5_close` | Close by ticket/symbol/magic (refuses to close everything blindly) |
| `mt5_history` | Closed deals + realized P&L for the last N days |

Environment variables: `MT5_TERMINAL_PATH` (default: auto-detect), `MT5_ALLOW_REAL=1` to enable trading tools on real accounts (**off by default, on purpose**).

## Signal bridge (`signal_bridge.py`)

Standalone daemon (not an MCP server — a trading loop should not live inside a chat session). It reads the BUY/SELL shapes any indicator paints on the chart and mirrors them in MT5 with stop-and-reverse semantics, acting on **closed bars only** (no intrabar repaint trades).

```bash
# 1. find your indicator's study id and shape plot indexes
python signal_bridge.py --list-studies

# 2. dry-run: print the signals it would read
python signal_bridge.py --study Dl3pBC --once

# 3. verify the full pipeline with a tiny demo round-trip (opens & closes 0.01)
python signal_bridge.py --study Dl3pBC --test

# 4. run it
python signal_bridge.py --study Dl3pBC --symbol XAUUSD --lot 0.01
```

Every action is logged to `bridge_trades.csv`. The bridge tags its positions with its own magic number (`--magic`) and never touches anything else.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `403 Forbidden` on the CDP websocket | Chrome rejects the `Origin` header. Already handled (`suppress_origin`); if you connect with your own tooling, strip the Origin header or launch with `--remote-allow-origins=*` |
| `tv_launch` says the app runs without the port | Electron's single-instance lock ignores flags on a second launch. Close TradingView fully, then `tv_launch` again |
| Studies vanish from the chart | TradingView's free (Basic) plan enforces an indicator limit per chart and may remove extras. The `gopro` popup announces it; `tv_close_popups` dismisses it |
| `exportData is not supported` | The desktop build disables OHLC export. Use `mt5_candles` or any other data source |
| Study ids change | Ids are regenerated when an indicator is re-added. Re-run `--list-studies` and update `--study` |
| `mt5.initialize failed` | Terminal not running/logged in, or auto-detect picked the wrong one — set `MT5_TERMINAL_PATH` |

## Security notes

- The CDP port gives **full control of the TradingView app and its logged-in session** to any local process. The servers bind to `127.0.0.1` only; don't expose the port beyond localhost.
- MT5 trading tools are **demo-only by default**. Enabling `MT5_ALLOW_REAL=1` is your decision and your risk.
- This project is not affiliated with TradingView or MetaQuotes. It drives undocumented internal APIs that may break with any app update.
- Nothing here is financial advice. Signals mirrored by the bridge are only as good as the indicator that paints them — backtest before you trust anything with money, even demo money.

## Quickstart en español

1. `pip install -r requirements.txt`
2. Registrá los servers en tu cliente (ver arriba — Claude Code, Claude Desktop, Codex CLI o Cursor).
3. Pedile a tu agente: *"lanzá TradingView"* (usa `tv_launch`), *"mostrame el chart"*, *"cambiá a XAUUSD en 5m"*, *"agregá un RSI"*, *"escribí esta estrategia en Pine"*.
4. Para el puente de señales a MT5: `python signal_bridge.py --list-studies` para encontrar el id de tu indicador, después `--once` (leer), `--test` (prueba en demo) y sin flags (daemon). Solo opera cuentas demo salvo `--allow-real`.

## License

MIT
