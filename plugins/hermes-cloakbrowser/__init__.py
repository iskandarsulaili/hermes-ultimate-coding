"""
hermes-cloakbrowser — Stealth browser automation for Hermes via CloakBrowser.

Provides a browser instance with:
- Fingerprint rotation (user agent, platform, timezone, locale, WebRTC)
- Proxy support (SOCKS5, HTTP)
- Humanized interaction (Bezier mouse, realistic typing)
- CDP-based automation (navigate, screenshot, HTML, click, type)

ARCHITECTURE:
  Manages a patched Chromium process as a managed subprocess (same pattern as
  the LSP plugin manages language servers). The CloakBrowser npm package handles
  binary download, fingerprint injection via Chromium flags, and stealth patches.

  The plugin spawns Node.js to run a small CDP server script, then connects
  via Python's websockets library to the Chrome DevTools Protocol endpoint.

THREAD SAFETY:
  All browser state (process handle, CDP connection) is protected by a lock.
  The WebSocket reader runs in a daemon thread.

ENVIRONMENT VARIABLES:
  HERMES_CLOAKBROWSER_PORT=9222     CDP port (default: auto-allocate)
  HERMES_CLOAKBROWSER_FINGERPRINT=   Random seed (omit for random)
  HERMES_CLOAKBROWSER_PROXY=         Proxy URL (socks5://user:pass@host:port)
  HERMES_CLOAKBROWSER_VIEWPORT=      1920x1080
  HERMES_CLOAKBROWSER_HEADLESS=true  Run headless
  HERMES_CLOAKBROWSER_TIMEOUT=30     Default navigation timeout (seconds)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────────
_DEFAULT_PORT = int(os.environ.get("HERMES_CLOAKBROWSER_PORT", "0"))  # 0 = auto
_DEFAULT_TIMEOUT = int(os.environ.get("HERMES_CLOAKBROWSER_TIMEOUT", "30"))
_DEFAULT_VIEWPORT = os.environ.get("HERMES_CLOAKBROWSER_VIEWPORT", "1920x1080")
_DEFAULT_HEADLESS = os.environ.get("HERMES_CLOAKBROWSER_HEADLESS", "true").lower() == "true"


def _find_free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Process management (like LSP plugin manages language servers) ───────────
class _CloakBrowserManager:
    """Manages the CloakBrowser subprocess lifecycle and CDP connection.

    Singleton pattern — one browser per Hermes session.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._port: int = 0
        self._ws_url: Optional[str] = None
        self._ws_conn: Any = None  # Persistent WebSocket connection
        self._msg_id: int = 0
        self._stopped = False
        self._node_path: Optional[str] = None
        self._npm_checked = False

    # ── Node.js / npm JIT check ──────────────────────────────────────────
    def _ensure_node(self) -> Optional[str]:
        """Verify Node.js and cloakbrowser package are available. Returns error or None."""
        if self._node_path:
            return None

        try:
            result = subprocess.run(
                ["node", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return "Node.js not found — install Node.js 20+"
            self._node_path = "node"
            logger.info("Node.js available: %s", result.stdout.strip())
        except FileNotFoundError:
            return "Node.js not found — install Node.js 20+"
        except subprocess.TimeoutExpired:
            return "Node.js check timed out"

        # Check if cloakbrowser npm package is installed
        try:
            result = subprocess.run(
                ["node", "-e", "require('cloakbrowser')"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                # Try installing it
                logger.info("cloakbrowser npm package not found — installing...")
                install = subprocess.run(
                    ["npm", "install", "cloakbrowser", "playwright-core", "websocket"],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(Path.home() / ".hermes" / "plugins" / "hermes-cloakbrowser"),
                )
                if install.returncode != 0:
                    return f"npm install failed: {install.stderr[:500]}"
                logger.info("cloakbrowser installed successfully")
        except subprocess.TimeoutExpired:
            return "npm install timed out"

        return None

    # ── Browser lifecycle ─────────────────────────────────────────────────
    def start(self, fingerprint: Optional[str] = None, proxy: Optional[str] = None) -> str:
        """Start a CloakBrowser instance. Returns the CDP WebSocket URL on success.

        Raises RuntimeError on failure.
        """
        if self._process and self._process.poll() is None:
            return f"Browser already running on port {self._port}"

        with self._lock:
            err = self._ensure_node()
            if err:
                raise RuntimeError(err)

            port = _DEFAULT_PORT if _DEFAULT_PORT else _find_free_port()

            # Build the Node.js script that launches CloakBrowser
            script = f"""
const {{ launch }} = require('cloakbrowser');
const {{ chromium }} = require('playwright-core');

(async () => {{
  const browser = await launch({{
    headless: {str(_DEFAULT_HEADLESS).lower()},
    args: ['--remote-debugging-port={port}'],
    fingerprint: '{fingerprint or ""}',
    proxy: '{proxy or ""}',
    viewport: {{ width: {_DEFAULT_VIEWPORT.split("x")[0]}, height: {_DEFAULT_VIEWPORT.split("x")[1]} }},
  }});
  console.log('CDP_ENDPOINT=' + browser.browser.wsEndpoint());
  console.log('BROWSER_PID=' + browser.browser.process().pid);

  // Keep alive until stdin closes or SIGTERM
  process.stdin.on('data', () => {{}});
  process.on('SIGTERM', () => browser.close().then(() => process.exit(0)));
}})();
"""
            try:
                self._process = subprocess.Popen(
                    [self._node_path, "-e", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(Path.home() / ".hermes" / "plugins" / "hermes-cloakbrowser"),
                )

                # Wait for the CDP endpoint URL
                start_time = time.time()
                timeout = 30
                cdp_url = None
                pid = None
                while time.time() - start_time < timeout:
                    line = self._process.stdout.readline()  # type: ignore
                    if not line:
                        break
                    line = line.strip()
                    if line.startswith("CDP_ENDPOINT="):
                        cdp_url = line.split("=", 1)[1]
                    elif line.startswith("BROWSER_PID="):
                        pid = line.split("=", 1)[1]
                    if cdp_url and pid:
                        break

                if not cdp_url:
                    stderr = self._process.stderr.read() if self._process.stderr else ""
                    raise RuntimeError(f"Browser failed to start: {stderr[:500]}")

                self._port = port
                self._ws_url = cdp_url
                logger.info("Browser started: PID=%s, CDP=%s", pid, cdp_url)
                return cdp_url

            except Exception as e:
                self._cleanup()
                raise RuntimeError(f"Failed to start browser: {e}")

    def stop(self) -> None:
        """Stop the browser process and close CDP connection."""
        with self._lock:
            self._close_ws()
            self._cleanup()

    def _close_ws(self) -> None:
        """Close the persistent WebSocket connection."""
        if self._ws_conn:
            try:
                self._ws_conn.close()
            except Exception:
                pass
            self._ws_conn = None

    def _ensure_ws(self) -> Any:
        """Get or create a persistent WebSocket connection to the CDP endpoint."""
        import websockets.sync.client

        if self._ws_conn:
            try:
                # Quick health check — send a simple CDP command
                self._send_cdp("Browser.getVersion")
                return self._ws_conn
            except Exception:
                # Connection died, reconnect
                self._close_ws()

        if not self._ws_url:
            raise RuntimeError("Browser not running — call cloakbrowser_launch first")

        self._ws_conn = websockets.sync.client.connect(self._ws_url)
        return self._ws_conn

    def _send_cdp(self, method: str, params: Optional[Dict] = None,
                   session_id: Optional[str] = None) -> Dict:
        """Send a CDP command over the persistent connection and return the result."""
        self._msg_id += 1
        cmd = {
            "id": self._msg_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            cmd["sessionId"] = session_id

        import json as _json
        ws = self._ensure_ws()
        ws.send(_json.dumps(cmd))
        response = _json.loads(ws.recv())

        if "error" in response:
            raise RuntimeError(f"CDP error: {response['error']}")
        return response.get("result", {})

    def _cleanup(self) -> None:
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
            except Exception:
                pass
            self._process = None
        self._port = 0
        self._ws_url = None

    def is_running(self) -> bool:
        """Check if the browser is still running."""
        if self._process and self._process.poll() is None:
            return True
        return False

    def health(self) -> Dict[str, Any]:
        """Return browser health status."""
        return {
            "running": self.is_running(),
            "port": self._port,
            "ws_url": self._ws_url,
            "process": self._process.pid if self._process else None,
        }


_manager = _CloakBrowserManager()


# ── CDP client helper ──────────────────────────────────────────────────────
def _cdp_send(method: str, params: Optional[Dict] = None,
               session_id: Optional[str] = None) -> Dict:
    """Send a CDP command via the manager's persistent connection."""
    return _manager._send_cdp(method, params, session_id)


def _get_first_target() -> Optional[str]:
    """Get the first page target ID from the browser."""
    targets = _cdp_send("Target.getTargets")
    targets_list = targets.get("targetInfos", [])
    page_targets = [t for t in targets_list if t.get("type") == "page"]
    if page_targets:
        return page_targets[0]["targetId"]
    return None


def _attach_to_target(target_id: str) -> str:
    """Attach to a target and return the session ID."""
    result = _cdp_send("Target.attachToTarget", {
        "targetId": target_id,
        "flatten": True,
    })
    return result.get("sessionId", "")


# ── Tool handlers ───────────────────────────────────────────────────────────
def _handle_cloakbrowser_launch(args: dict, **kwargs: Any) -> str:
    """Launch a CloakBrowser instance."""
    if _manager.is_running():
        return json.dumps({"status": "already_running", "ws_url": _manager._ws_url})

    try:
        cdp_url = _manager.start(
            fingerprint=args.get("fingerprint"),
            proxy=args.get("proxy"),
        )
        return json.dumps({"status": "started", "ws_url": cdp_url}, default=str)
    except RuntimeError as e:
        return json.dumps({"status": "error", "error": str(e)}, default=str)


def _handle_cloakbrowser_navigate(args: dict, **kwargs: Any) -> str:
    """Navigate to a URL and return the page content."""
    if not _manager.is_running():
        return json.dumps({"error": "Browser not running — call cloakbrowser_launch first"})

    url = args.get("url", "")
    if not url:
        return json.dumps({"error": "url is required"})

    try:
        # Create a new page via CDP
        target_id = args.get("target_id")
        if not target_id:
            target_id = _get_first_target()
        if not target_id:
            # Create a new page
            result = _cdp_send("Target.createTarget", {"url": "about:blank"})
            target_id = result.get("targetId")

        session_id = _attach_to_target(target_id)

        # Navigate
        _cdp_send("Page.enable", session_id=session_id)
        nav_result = _cdp_send("Page.navigate", {
            "url": url,
        }, session_id=session_id)

        # Wait for page load
        timeout = int(args.get("timeout", _DEFAULT_TIMEOUT))
        _cdp_send("Page.loadEventFired", {"timeout": timeout * 1000}, session_id=session_id)

        # Get page title
        title_result = _cdp_send("Runtime.evaluate", {
            "expression": "document.title",
            "returnByValue": True,
        }, session_id=session_id)

        # Get page text
        text_result = _cdp_send("Runtime.evaluate", {
            "expression": "document.body.innerText",
            "returnByValue": True,
        }, session_id=session_id)

        title = title_result.get("result", {}).get("value", "")
        text = text_result.get("result", {}).get("value", "")

        return json.dumps({
            "url": url,
            "title": title,
            "target_id": target_id,
            "frame_id": nav_result.get("frameId", ""),
            "text_preview": text[:5000] if text else "",
            "text_length": len(text) if text else 0,
        }, default=str)

    except RuntimeError as e:
        return json.dumps({"error": str(e)}, default=str)


def _handle_cloakbrowser_screenshot(args: dict, **kwargs: Any) -> str:
    """Take a screenshot of the current page."""
    if not _manager.is_running():
        return json.dumps({"error": "Browser not running — call cloakbrowser_launch first"})

    try:
        target_id = args.get("target_id") or _get_first_target()
        if not target_id:
            return json.dumps({"error": "No page target found"})

        session_id = _attach_to_target(target_id)

        result = _cdp_send("Page.captureScreenshot", {
            "format": "png",
            "fromSurface": True,
        }, session_id=session_id)

        screenshot_data = result.get("data", "")
        if not screenshot_data:
            return json.dumps({"error": "Screenshot returned empty data"})

        # Save to temp file
        import tempfile
        import base64
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(base64.b64decode(screenshot_data))
        screenshot_path = tmp.name
        tmp.close()

        return json.dumps({
            "screenshot_path": screenshot_path,
            "format": "png",
            "data_length": len(screenshot_data),
        }, default=str)

    except RuntimeError as e:
        return json.dumps({"error": str(e)}, default=str)


def _handle_cloakbrowser_html(args: dict, **kwargs: Any) -> str:
    """Get the full HTML of the current page."""
    if not _manager.is_running():
        return json.dumps({"error": "Browser not running — call cloakbrowser_launch first"})

    try:
        target_id = args.get("target_id") or _get_first_target()
        if not target_id:
            return json.dumps({"error": "No page target found"})

        session_id = _attach_to_target(target_id)

        result = _cdp_send("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        }, session_id=session_id)

        html = result.get("result", {}).get("value", "")
        max_chars = int(args.get("max_chars", 50000))

        return json.dumps({
            "html": html[:max_chars],
            "truncated": len(html) > max_chars,
            "total_length": len(html),
        }, default=str)

    except RuntimeError as e:
        return json.dumps({"error": str(e)}, default=str)


def _handle_cloakbrowser_close(args: dict, **kwargs: Any) -> str:
    """Close the browser instance."""
    if not _manager.is_running():
        return json.dumps({"status": "not_running"})

    _manager.stop()
    return json.dumps({"status": "stopped"}, default=str)


def _handle_cloakbrowser_status(args: dict, **kwargs: Any) -> str:
    """Check browser status."""
    return json.dumps(_manager.health(), default=str)


# ── Slash command handler ──────────────────────────────────────────────────
def _cmd_cloakbrowser(raw_args: str) -> str:
    """Handle /cloakbrowser slash command."""
    parts = raw_args.strip().split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""

    if not subcmd or subcmd == "status":
        return json.dumps(_manager.health(), default=str, indent=2)
    elif subcmd == "launch":
        try:
            cdp_url = _manager.start()
            return json.dumps({"status": "started", "ws_url": cdp_url}, default=str, indent=2)
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)}, default=str, indent=2)
    elif subcmd == "close":
        _manager.stop()
        return json.dumps({"status": "stopped"}, default=str, indent=2)
    else:
        return (
            "Usage: /cloakbrowser [launch|close|status]\n"
            "  launch  — Start a new CloakBrowser instance\n"
            "  close   — Close the browser\n"
            "  status  — Check browser status"
        )


# ── Plugin entry point ─────────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    """Register the hermes-cloakbrowser plugin."""
    logger.info("Registering hermes-cloakbrowser plugin")

    # Register tools
    ctx.register_tool(
        name="cloakbrowser_launch",
        toolset="cloakbrowser",
        schema={
            "name": "cloakbrowser_launch",
            "description": "Launch a CloakBrowser stealth browser instance. Starts a patched Chromium with fingerprint rotation and optional proxy. Returns the CDP WebSocket URL. Call this first before any other cloakbrowser tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fingerprint": {
                        "type": "string",
                        "description": "Fingerprint seed (omit for random). Controls user agent, platform, timezone, locale.",
                    },
                    "proxy": {
                        "type": "string",
                        "description": "Proxy URL. Format: socks5://user:pass@host:port or http://host:port",
                    },
                },
            },
        },
        handler=_handle_cloakbrowser_launch,
    )

    ctx.register_tool(
        name="cloakbrowser_navigate",
        toolset="cloakbrowser",
        schema={
            "name": "cloakbrowser_navigate",
            "description": "Navigate the browser to a URL. Creates a new page if none exists, waits for full page load, and returns the page title and text content preview. Best for: browsing to a URL and extracting its readable content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "Existing page target ID (omit to auto-select or create)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Navigation timeout in seconds (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["url"],
            },
        },
        handler=_handle_cloakbrowser_navigate,
    )

    ctx.register_tool(
        name="cloakbrowser_screenshot",
        toolset="cloakbrowser",
        schema={
            "name": "cloakbrowser_screenshot",
            "description": "Capture a PNG screenshot of the current page. Returns the screenshot file path and data length. The agent can share the screenshot path with the user using MEDIA:path syntax.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "Page target ID (omit to auto-select)",
                    },
                },
            },
        },
        handler=_handle_cloakbrowser_screenshot,
    )

    ctx.register_tool(
        name="cloakbrowser_html",
        toolset="cloakbrowser",
        schema={
            "name": "cloakbrowser_html",
            "description": "Get the full HTML of the current page. Useful for DOM analysis, form discovery, and structured content extraction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "Page target ID (omit to auto-select)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return (default: 50000)",
                        "default": 50000,
                    },
                },
            },
        },
        handler=_handle_cloakbrowser_html,
    )

    ctx.register_tool(
        name="cloakbrowser_close",
        toolset="cloakbrowser",
        schema={
            "name": "cloakbrowser_close",
            "description": "Close the browser instance and release all resources. Safe to call even if not running.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_cloakbrowser_close,
    )

    ctx.register_tool(
        name="cloakbrowser_status",
        toolset="cloakbrowser",
        schema={
            "name": "cloakbrowser_status",
            "description": "Check the browser status: running/stopped, port, PID, CDP WebSocket URL.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_cloakbrowser_status,
    )

    # Register slash command
    ctx.register_command(
        name="cloakbrowser",
        description=(
            "CloakBrowser stealth browser commands. "
            "Subcommands: launch, close, status"
        ),
        handler=_cmd_cloakbrowser,
    )

    logger.info("hermes-cloakbrowser: registered 6 tools + 1 command")
    return {"name": "hermes-cloakbrowser", "version": "1.0.0"}
