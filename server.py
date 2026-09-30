# -*- coding: utf-8 -*-
"""
MCP server for TradingView Desktop (Windows; also runs on Linux when TradingView
is started separately with the CDP port, e.g. as a systemd service).

TradingView Desktop is an Electron app, so it can be controlled through the
Chrome DevTools Protocol (CDP). This server connects to the chart page and
exposes TradingView's internal Charting Library API (`window.TradingViewApi`)
as MCP tools: screenshots, symbol/timeframe control, indicators, the Pine
Editor, the Strategy Tester, and a raw JS escape hatch.

The app must be launched with --remote-debugging-port (the tv_launch tool
does this automatically on Windows).

Environment variables:
  TV_CDP_PORT   CDP debugging port (default: 9222)
  TV_EXE_PATH   full path to TradingView.exe (default: auto-discover)
"""
import glob
import json
import os
import subprocess
import tempfile
import time
from base64 import b64decode
from pathlib import Path
from typing import Any

import requests
import websocket
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

CDP_PORT = int(os.environ.get("TV_CDP_PORT", "9222"))
CDP = f"http://127.0.0.1:{CDP_PORT}"

mcp = FastMCP("tradingview")


# ---------------------------------------------------------------- CDP client

class CDPError(RuntimeError):
    pass


class CDPClient:
    """Lazy connection to the chart page target. Reconnects automatically."""

    def __init__(self):
        self._ws = None
        self._msg_id = 0

    # -- app / target discovery -------------------------------------------

    @staticmethod
    def app_running() -> bool:
        """True if the CDP endpoint is responding."""
        try:
            requests.get(f"{CDP}/json/version", timeout=2)
            return True
        except requests.RequestException:
            return False

    @staticmethod
    def process_running() -> bool:
        """True if a TradingView process exists (with or without the CDP port)."""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq TradingView.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return "TradingView.exe" in out
        except (subprocess.SubprocessError, OSError):
            return False

    @staticmethod
    def find_exe() -> str | None:
        env = os.environ.get("TV_EXE_PATH")
        if env and Path(env).exists():
            return env
        candidates = [
            # Microsoft Store (MSIX) install
            r"C:\Program Files\WindowsApps\TradingView.Desktop_*\TradingView.exe",
            # classic installer locations
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\tradingview\TradingView.exe"),
            r"C:\Program Files\TradingView\TradingView.exe",
        ]
        for pattern in candidates:
            hits = glob.glob(pattern)
            if hits:
                return hits[0]
        return None

    @staticmethod
    def chart_target() -> dict | None:
        try:
            targets = requests.get(f"{CDP}/json/list", timeout=5).json()
        except requests.RequestException:
            return None
        for t in targets:
            if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", ""):
                return t
        return None

    # -- websocket ----------------------------------------------------------

    def _connect(self):
        target = self.chart_target()
        if target is None:
            raise CDPError(
                "Chart page not found. Is TradingView running with "
                f"--remote-debugging-port={CDP_PORT} and a chart tab open? Use tv_launch."
            )
        # suppress_origin is required: Chrome rejects the Origin header with 403
        self._ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=30, suppress_origin=True
        )

    def _send(self, method: str, params: dict) -> dict:
        if self._ws is None:
            self._connect()
        self._msg_id += 1
        msg_id = self._msg_id
        try:
            self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
            while True:
                resp = json.loads(self._ws.recv())
                if resp.get("id") == msg_id:
                    return resp
        except (websocket.WebSocketException, ConnectionError, OSError):
            # reconnect once (the app may have restarted or the tab reloaded)
            self._ws = None
            self._connect()
            self._msg_id += 1
            msg_id = self._msg_id
            self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
            while True:
                resp = json.loads(self._ws.recv())
                if resp.get("id") == msg_id:
                    return resp

    # -- high-level ----------------------------------------------------------

    def eval(self, expression: str, await_promise: bool = False) -> Any:
        resp = self._send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "timeout": 30000,
        })
        if "error" in resp:
            raise CDPError(f"CDP error: {resp['error']}")
        result = resp.get("result", {})
        if "exceptionDetails" in result:
            exc = result["exceptionDetails"]
            desc = exc.get("exception", {}).get("description", "") or exc.get("text", "")
            raise CDPError(f"JS exception: {desc[:500]}")
        return result.get("result", {}).get("value")

    def screenshot_png(self, clip: dict | None = None) -> bytes:
        params: dict = {"format": "png"}
        if clip:
            params["clip"] = clip
        resp = self._send("Page.captureScreenshot", params)
        if "error" in resp:
            raise CDPError(f"CDP error: {resp['error']}")
        return b64decode(resp["result"]["data"])


client = CDPClient()


def _ok(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


# ---------------------------------------------------------------------- tools

@mcp.tool()
def tv_status() -> str:
    """TradingView status: app running, CDP connected, current symbol and timeframe."""
    if not client.app_running():
        if client.process_running():
            return _ok({"running": True, "cdp": False,
                        "hint": "TradingView is running WITHOUT the debugging port. "
                                "Close it and use tv_launch."})
        return _ok({"running": False, "exe_found": client.find_exe(),
                    "hint": "Use tv_launch to start it."})
    info = {"running": True}
    try:
        info.update(client.eval(
            "({symbol: TradingViewApi.activeChart().symbol(),"
            " resolution: String(TradingViewApi.activeChart().resolution()),"
            " chartType: TradingViewApi.activeChart().chartType(),"
            " layout: TradingViewApi.layoutName(),"
            " chartsCount: TradingViewApi.chartsCount()})"
        ))
    except CDPError as e:
        info["chart_error"] = str(e)
    return _ok(info)


@mcp.tool()
def tv_launch(wait_seconds: int = 25) -> str:
    """Launch TradingView Desktop with the CDP debugging port. No-op if already running with CDP."""
    if client.app_running():
        return _ok({"launched": False, "reason": "already running with CDP enabled"})
    if client.process_running():
        return _ok({
            "launched": False,
            "error": "TradingView is already open WITHOUT the debugging port. "
                     "Close it first (Electron's single-instance lock ignores the flag), "
                     "then call tv_launch again.",
        })
    exe = client.find_exe()
    if not exe:
        return _ok({"launched": False,
                    "error": "TradingView.exe not found. Set TV_EXE_PATH env var."})
    subprocess.Popen([exe, f"--remote-debugging-port={CDP_PORT}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if client.app_running() and client.chart_target():
            return _ok({"launched": True, "exe": exe})
        time.sleep(1)
    return _ok({"launched": True, "exe": exe,
                "warning": f"CDP/chart did not respond within {wait_seconds}s; retry tv_status"})


@mcp.tool()
def tv_screenshot() -> list:
    """Capture the whole TradingView window (toolbars included). For chart analysis
    prefer tv_screenshot_chart. Returns the PNG image plus the
    path of a copy saved to the temp dir (for clients that don't render MCP images)."""
    png = client.screenshot_png()
    path = Path(tempfile.gettempdir()) / f"tv_screenshot_{int(time.time())}.png"
    path.write_bytes(png)
    return [Image(data=png, format="png"), _ok({"saved_to": str(path)})]


def _close_popups() -> int:
    return client.eval("""
(() => {
  let n = 0;
  for (const d of document.querySelectorAll('[data-dialog-name], [role="dialog"]')) {
    const b = Array.from(d.querySelectorAll('button')).find(
      x => /cierre|close|cerrar/i.test(x.getAttribute('aria-label') || x.textContent || ''));
    if (b) { b.click(); n++; }
  }
  return n;
})()
""")


def _reset_view() -> dict:
    client.eval("TradingViewApi.activeChart().executeActionById('chartReset')")
    closed = _close_popups()
    time.sleep(0.5)
    return {"reset": True, "popups_closed": closed}


@mcp.tool()
def tv_reset_view() -> str:
    """Reset the chart view (TradingView's 'Reset chart': default zoom, autoscaled
    price axis) and close popups. Fixes a price scale left over from another
    timeframe. tv_screenshot_chart does this automatically."""
    return _ok(_reset_view())


@mcp.tool()
def tv_screenshot_chart(scale: float = 1.5) -> list:
    """Screenshot of the chart area only (no toolbars/sidebars). Resets the view first
    (see tv_reset_view). scale>1 renders the crop at higher resolution for readability."""
    _reset_view()
    box = client.eval("""(() => {
      const el = document.querySelector('.chart-container')
              || document.querySelector('.layout__area--center');
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: r.x, y: r.y, width: r.width, height: r.height};
    })()""")
    clip = {**box, "scale": scale} if box else None
    png = client.screenshot_png(clip)
    path = Path(tempfile.gettempdir()) / f"tv_chart_{int(time.time())}.png"
    path.write_bytes(png)
    return [Image(data=png, format="png"),
            _ok({"saved_to": str(path), "clipped": bool(box), "box": box})]


@mcp.tool()
def tv_get_chart() -> str:
    """Active chart info: symbol, resolution, type, visible range, timezone and studies."""
    data = client.eval(
        "(() => { const c = TradingViewApi.activeChart();"
        " return {symbol: c.symbol(), resolution: String(c.resolution()),"
        " chartType: c.chartType(), timezone: c.getTimezone(),"
        " visibleRange: c.getVisibleRange(),"
        " studies: c.getAllStudies()}; })()"
    )
    return _ok(data)


@mcp.tool()
def tv_set_symbol(symbol: str, wait_seconds: int = 10) -> str:
    """Change the active chart symbol. E.g. 'OANDA:XAUUSD', 'BINANCE:BTCUSDT', 'NASDAQ:AAPL'."""
    client.eval(f"TradingViewApi.activeChart().setSymbol({json.dumps(symbol)})")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        cur = client.eval("TradingViewApi.activeChart().symbol()")
        if symbol.split(":")[-1].upper() in str(cur).upper():
            return _ok({"symbol": cur, "changed": True})
        time.sleep(0.5)
    return _ok({"symbol": client.eval("TradingViewApi.activeChart().symbol()"),
                "changed": False, "warning": "symbol did not confirm the change (valid ticker?)"})


@mcp.tool()
def tv_set_resolution(resolution: str) -> str:
    """Change the chart timeframe. Values: '1','5','15','30','60','240','1D','1W','1M'."""
    client.eval(f"TradingViewApi.activeChart().setResolution({json.dumps(resolution)})")
    time.sleep(1.0)
    return _ok({"resolution": str(client.eval("String(TradingViewApi.activeChart().resolution())"))})


@mcp.tool()
def tv_list_studies() -> str:
    """List the indicators/strategies loaded on the active chart (id + name)."""
    return _ok(client.eval("TradingViewApi.activeChart().getAllStudies()"))


@mcp.tool()
def tv_add_study(name: str, force_overlay: bool = False) -> str:
    """Add an indicator to the chart by name. E.g. 'RSI', 'MACD', 'Bollinger Bands'."""
    expr = (f"TradingViewApi.activeChart().createStudy({json.dumps(name)}, "
            f"{json.dumps(force_overlay)}, false).then(id => ({{id: id}}))")
    return _ok(client.eval(expr, await_promise=True))


@mcp.tool()
def tv_remove_study(study_id: str) -> str:
    """Remove a study from the chart by its id (see tv_list_studies)."""
    client.eval(f"TradingViewApi.activeChart().removeEntity({json.dumps(study_id)})")
    return _ok({"removed": study_id})


@mcp.tool()
def tv_strategy_report(max_chars: int = 8000) -> str:
    """Strategy Tester report for the active strategy (performance, trades, etc.).
    Requires a strategy to be selected in the Strategy Tester panel."""
    data = client.eval(
        "TradingViewApi.backtestingStrategyApi().then(a => {"
        " const st = a.activeStrategyStatus; const rd = a.activeStrategyReportData;"
        " const status = st && st.value ? st.value() : st;"
        " const report = rd && rd.value ? rd.value() : rd;"
        " return {status: status, report: report}; })",
        await_promise=True,
    )
    out = _ok(data)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n... (truncated, {len(out)} chars total; raise max_chars or filter with tv_eval_js)"
    return out


@mcp.tool()
def tv_pine_open_editor() -> str:
    """Open the Pine Editor panel."""
    client.eval("TradingViewApi.pineEditorTestApi().openEditor()")
    return _ok({"opened": True})


@mcp.tool()
def tv_pine_set_script(code: str) -> str:
    """Write Pine Script code into the editor (replaces current content).
    Then use tv_pine_add_to_chart to compile and add it to the chart."""
    client.eval(f"TradingViewApi.pineEditorTestApi().setEditorText({json.dumps(code)})")
    return _ok({"written": True, "chars": len(code)})


@mcp.tool()
def tv_pine_add_to_chart(update_existing: bool = False) -> str:
    """Compile the Pine Editor script and add it to the chart.
    update_existing=True updates the already-added script instead of duplicating it.
    NOTE: TradingView's free (Basic) plan limits indicators per chart; when the limit
    is hit a 'gopro' popup appears and the script is NOT added (reported by this tool)."""
    before = client.eval("TradingViewApi.activeChart().getAllStudies().map(s => s.id)") or []
    method = "updateScriptOnChart" if update_existing else "addScriptOnChart"
    client.eval(f"TradingViewApi.pineEditorTestApi().{method}()")
    time.sleep(3.0)
    studies = client.eval("TradingViewApi.activeChart().getAllStudies()") or []
    result = {"method": method, "studies": studies}
    if not update_existing and len(studies) <= len(before):
        gopro = client.eval("!!document.querySelector('[data-dialog-name=\"gopro\"]')")
        if gopro:
            result["error"] = ("Plan indicator limit reached (gopro popup). "
                               "Remove a study with tv_remove_study or close the popup with tv_close_popups.")
        else:
            result["warning"] = "No new study appeared (Pine compilation error?)."
    return _ok(result)


@mcp.tool()
def tv_close_popups() -> str:
    """Close open TradingView dialogs/popups (e.g. 'gopro' plan-limit promos)."""
    n = _close_popups()
    return _ok({"closed": n})


@mcp.tool()
def tv_eval_js(expression: str, await_promise: bool = False, max_chars: int = 8000) -> str:
    """Evaluate arbitrary JavaScript in the TradingView chart page (escape hatch).
    Useful objects: TradingViewApi, TradingViewApi.activeChart(),
    TradingViewApi.activeChart().chartModel().dataSources().
    Use await_promise=true if the expression returns a Promise."""
    result = client.eval(expression, await_promise=await_promise)
    out = _ok(result)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n... (truncated, {len(out)} chars total)"
    return out


if __name__ == "__main__":
    mcp.run()
