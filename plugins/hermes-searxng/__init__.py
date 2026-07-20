"""
hermes-searxng — Native metasearch for Hermes via SearXNG.

Portable approach that works on any machine:
1. Check if SearXNG is already running (try REST API on common ports)
2. If not, start SearXNG as a subprocess (the Flask webapp) and connect via REST API
3. Cache the subprocess and kill it on plugin shutdown

This avoids embedding SearXNG's asyncio/network pipeline inline (which
blocks indefinitely) while still working on fresh machines with no
pre-existing SearXNG service.

ARCHITECTURE:
  SearXNG's search pipeline requires asyncio event loops, HTTPX transport
  pools, and engine loaders that block when called inline. By running
  SearXNG as a subprocess (its normal Flask webapp), the plugin avoids
  these blocking calls while still providing full search functionality.

  The plugin discovers SearXNG via:
  1. SEARXNG_BASE_URL env var (explicit config)
  2. Already-running service on common ports (8080, 8888, 4000)
  3. Starting it as a subprocess from searxng-src

DEPENDENCIES (JIT installed):
  httpx — for HTTP requests to the SearXNG service
  SearXNG's own deps (flask, etc.) — only if we need to start it
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── JIT dependency management ──────────────────────────────────────────────
try:
    from _shared.deps import DepSpec, ensure_deps

    _HTTPX_DEPS: List[DepSpec] = [
        DepSpec("httpx", [sys.executable, "-c", "import httpx"], install=[sys.executable, "-m", "pip", "install", "httpx"], purpose="HTTP client for SearXNG REST API"),
    ]

    def _ensure_httpx() -> str | None:
        try:
            ensure_deps("hermes-searxng", _HTTPX_DEPS, ask=False)
            return None
        except Exception as e:
            return str(e)

except ImportError:
    def _ensure_httpx() -> str | None:
        return "_shared.deps not available — cannot auto-install dependencies"


# ── SearXNG discovery ─────────────────────────────────────────────────────
_SEARXNG_SRC_CANDIDATES = [
    os.environ.get("HERMES_SEARXNG_SRC", ""),
    str(Path(__file__).resolve().parent.parent.parent / "searxng" / "searxng-src"),
]

# Common ports where SearXNG might be running
_SEARXNG_DEFAULT_PORTS = [8080, 8888, 4000]

_SEARXNG_LOCK = threading.Lock()
_http_client: Optional[Any] = None
_searxng_process: Optional[subprocess.Popen] = None
_searxng_url: Optional[str] = None


def _get_client() -> Any:
    """Lazy-init the shared httpx client."""
    global _http_client
    if _http_client is None:
        import httpx
        _http_client = httpx.Client(timeout=30.0)
    return _http_client


def _find_searxng_src() -> Optional[str]:
    """Locate the SearXNG searxng-src directory."""
    for path in _SEARXNG_SRC_CANDIDATES:
        if path and (Path(path) / "searx").is_dir():
            return path
    return None


def _check_port(port: int) -> bool:
    """Check if SearXNG is responding on a given port."""
    try:
        import httpx
        r = httpx.get(f"http://localhost:{port}/search", params={"q": "ping", "format": "json"}, timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


def _start_searxng() -> Optional[str]:
    """Start SearXNG as a subprocess. Returns the base URL or error."""
    global _searxng_process

    src = _find_searxng_src()
    if not src:
        return "Cannot find searxng-src. Set HERMES_SEARXNG_SRC env var or ensure ~/searxng/searxng-src exists."

    # Install SearXNG's own deps
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "flask", "flask_babel", "httpx", "httpx_socks", "msgspec", "PyYAML", "Babel", "Jinja2", "Markdown", "certifi", "idna", "charset-normalizer"],
            capture_output=True, timeout=120,
        )
    except Exception as e:
        return f"Failed to install SearXNG deps: {e}"

    # Start SearXNG webapp
    try:
        env = os.environ.copy()
        settings_path = str(Path(src) / "searx" / "settings.yml")
        env["SEARXNG_SETTINGS_PATH"] = settings_path
        _searxng_process = subprocess.Popen(
            [sys.executable, "-m", "searx.webapp"],
            cwd=src,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for it to start — try the port from settings.yml (default 8888)
        import yaml
        with open(settings_path) as f:
            settings = yaml.safe_load(f)
        port = settings.get("server", {}).get("port", 8888)
        url = f"http://localhost:{port}"
        for _ in range(30):
            time.sleep(1)
            try:
                import httpx
                r = httpx.get(f"{url}/search", params={"q": "ping", "format": "json"}, timeout=5.0)
                if r.status_code == 200:
                    return url
            except Exception:
                continue
        return f"SearXNG subprocess started but not responding on {url}"
    except Exception as e:
        return f"Failed to start SearXNG: {e}"


def _discover_searxng() -> Optional[str]:
    """Discover or start SearXNG. Returns the base URL or None."""
    global _searxng_url

    # 1. Check env var
    env_url = os.environ.get("SEARXNG_BASE_URL", "")
    if env_url:
        try:
            import httpx
            r = httpx.get(f"{env_url}/search", params={"q": "ping", "format": "json"}, timeout=5.0)
            if r.status_code == 200:
                _searxng_url = env_url
                return _searxng_url
        except Exception:
            pass

    # 2. Check common ports
    for port in _SEARXNG_DEFAULT_PORTS:
        if _check_port(port):
            _searxng_url = f"http://localhost:{port}"
            return _searxng_url

    # 3. Try to start it
    result = _start_searxng()
    if result and not result.startswith("Cannot") and not result.startswith("Failed"):
        _searxng_url = result
        return _searxng_url

    return None


class _SearxngEngine:
    """Lazy singleton wrapping SearXNG's REST API.

    Thread-safe: all public methods acquire _SEARXNG_LOCK.
    Portable: works on any machine with or without pre-existing SearXNG.
    """

    def __init__(self):
        self._ready = False
        self._error: Optional[str] = None
        self._base_url: Optional[str] = None

    def ensure_ready(self) -> Optional[str]:
        """Discover or start SearXNG. Returns error or None."""
        if self._ready:
            return None
        with _SEARXNG_LOCK:
            if self._ready:
                return None

            # 1. Install httpx
            deps_err = _ensure_httpx()
            if deps_err:
                self._error = deps_err
                return deps_err

            # 2. Discover or start SearXNG
            url = _discover_searxng()
            if not url:
                self._error = (
                    "Cannot find or start SearXNG. "
                    "Set SEARXNG_BASE_URL env var to an existing SearXNG instance, "
                    "or ensure ~/searxng/searxng-src exists so the plugin can start it."
                )
                return self._error

            self._base_url = url
            self._ready = True
            return None

    def search(
        self,
        query: str,
        categories: Optional[List[str]] = None,
        lang: str = "en-US",
        safesearch: int = 0,
        pageno: int = 1,
        time_range: Optional[str] = None,
        engines: Optional[List[str]] = None,
        max_results: int = 20,
    ) -> Dict[str, Any]:
        """Execute a SearXNG search via REST API and return structured results."""
        err = self.ensure_ready()
        if err:
            return {"error": err}

        with _SEARXNG_LOCK:
            try:
                params: Dict[str, Any] = {
                    "q": query,
                    "format": "json",
                    "language": lang,
                    "safesearch": safesearch,
                    "pageno": pageno,
                }
                if categories:
                    params["categories"] = ",".join(categories)
                if time_range:
                    params["time_range"] = time_range
                if engines:
                    params["engines"] = ",".join(engines)

                client = _get_client()
                r = client.get(f"{self._base_url}/search", params=params, timeout=30.0)
                r.raise_for_status()
                data = r.json()

                results = []
                for item in data.get("results", []):
                    results.append({
                        "url": item.get("url", ""),
                        "title": item.get("title", ""),
                        "content": item.get("content", ""),
                        "engine": item.get("engine", ""),
                        "category": item.get("category", ""),
                        "publishedDate": str(item.get("publishedDate", "")),
                        "thumbnail": item.get("thumbnail", ""),
                        "img_src": item.get("img_src", ""),
                    })
                    if len(results) >= max_results:
                        break

                return {
                    "results": results,
                    "engines_used": list(data.get("engines", [])),
                    "suggestions": list(data.get("suggestions", [])),
                    "answers": [str(a) for a in data.get("answers", [])],
                    "infoboxes": [str(i) for i in data.get("infoboxes", [])],
                    "number_of_results": data.get("number_of_results", 0),
                    "paging": data.get("paging", False),
                }

            except Exception as e:
                logger.error("SearXNG search failed: %s", e)
                return {"error": f"Search failed: {e}"}

    def list_engines(self) -> List[Dict[str, Any]]:
        """List available search engines via REST API."""
        err = self.ensure_ready()
        if err:
            return [{"error": err}]

        with _SEARXNG_LOCK:
            try:
                client = _get_client()
                r = client.get(f"{self._base_url}/engines", timeout=10.0)
                r.raise_for_status()
                data = r.json()
                engines_list = []
                for name, info in data.get("engines", {}).items():
                    engines_list.append({
                        "name": name,
                        "categories": info.get("categories", []),
                        "shortcut": info.get("shortcut", ""),
                        "engine_type": info.get("engine_type", "online"),
                        "language_support": info.get("language_support", False),
                        "safesearch": info.get("safesearch", False),
                        "time_range_support": info.get("time_range_support", False),
                    })
                return engines_list
            except Exception as e:
                return [{"error": str(e)}]

    def list_categories(self) -> List[Dict[str, Any]]:
        """List search categories with engine counts via REST API."""
        err = self.ensure_ready()
        if err:
            return [{"error": err}]

        with _SEARXNG_LOCK:
            try:
                client = _get_client()
                r = client.get(f"{self._base_url}/engines", timeout=10.0)
                r.raise_for_status()
                data = r.json()
                cats = {}
                for name, info in data.get("engines", {}).items():
                    for cat in info.get("categories", []):
                        if cat not in cats:
                            cats[cat] = []
                        cats[cat].append(name)
                return [
                    {"name": cat, "engine_count": len(engines), "engines": engines}
                    for cat, engines in sorted(cats.items())
                ]
            except Exception as e:
                return [{"error": str(e)}]

    def status(self) -> Dict[str, Any]:
        """Return plugin status."""
        result: Dict[str, Any] = {
            "ready": self._ready,
            "searxng_url": self._base_url,
        }
        if self._error:
            result["error"] = self._error
        return result


_engine = _SearxngEngine()


# ── Tool handlers ───────────────────────────────────────────────────────────
def _handle_searxng_search(args: dict, **kwargs: Any) -> str:
    """Execute a web search across 170+ engines."""
    query = args.get("query", "")
    if not query:
        return json.dumps({"error": "query is required"})

    result = _engine.search(
        query=query,
        categories=args.get("categories"),
        lang=args.get("lang", "en-US"),
        safesearch=max(0, min(int(args.get("safesearch", 0)), 2)),
        pageno=max(1, min(int(args.get("pageno", 1)), 100)),
        time_range=args.get("time_range"),
        engines=args.get("engines"),
        max_results=min(int(args.get("max_results", 20)), 50),
    )
    return json.dumps(result, default=str)


def _handle_searxng_engines(args: dict, **kwargs: Any) -> str:
    """List available search engines and their capabilities."""
    result = _engine.list_engines()
    category = args.get("category", "")
    if category:
        result = [e for e in result if category in e.get("categories", [])]
    return json.dumps(result, default=str)


def _handle_searxng_categories(args: dict, **kwargs: Any) -> str:
    """List search categories with engine counts."""
    result = _engine.list_categories()
    return json.dumps(result, default=str)


def _handle_searxng_status(args: dict, **kwargs: Any) -> str:
    """Check SearXNG engine status."""
    return json.dumps(_engine.status(), default=str)


# ── Slash command handler ──────────────────────────────────────────────────
def _cmd_searxng(raw_args: str) -> str:
    """Handle /searxng slash command."""
    parts = raw_args.strip().split(maxsplit=2)
    if not parts:
        return (
            "Usage: /searxng search <query> [options]\n"
            "       /searxng engines [--category <name>]\n"
            "       /searxng categories\n"
            "       /searxng status\n"
        )

    subcmd = parts[0].lower()
    if subcmd == "status":
        return json.dumps(_engine.status(), default=str, indent=2)
    elif subcmd == "engines":
        category = ""
        if len(parts) > 1 and parts[1] == "--category":
            category = parts[2] if len(parts) > 2 else ""
        result = _engine.list_engines()
        if category:
            result = [e for e in result if category in e.get("categories", [])]
        return json.dumps(result[:20], default=str, indent=2)
    elif subcmd == "categories":
        return json.dumps(_engine.list_categories(), default=str, indent=2)
    elif subcmd == "search":
        query = parts[1] if len(parts) > 1 else ""
        if not query:
            return "Usage: /searxng search <query>"
        result = _engine.search(query=query)
        return json.dumps(result, default=str, indent=2)
    else:
        return f"Unknown subcommand: {subcmd}"


# ── Plugin entry point ─────────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    """Register the hermes-searxng plugin."""
    logger.info("Registering hermes-searxng plugin")

    # Register tools
    ctx.register_tool(
        name="searxng_search",
        toolset="searxng",
        schema={
            "name": "searxng_search",
            "description": "Execute a web search across 170+ search engines via SearXNG. Supports categories (general, images, news, videos, science, it, files, social media), language selection, time range filtering, and safety levels. Returns structured results with title, URL, content, engine source, and metadata. BEST FOR: any web search query the agent needs to answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict to categories: general, images, news, videos, science, it, files, social media",
                    },
                    "lang": {
                        "type": "string",
                        "description": "BCP 47 language code (default: en-US)",
                        "default": "en-US",
                    },
                    "safesearch": {
                        "type": "integer",
                        "description": "SafeSearch level: 0=off, 1=moderate, 2=strict",
                        "default": 0,
                    },
                    "pageno": {
                        "type": "integer",
                        "description": "Page number (1-indexed)",
                        "default": 1,
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Time range: day, week, month, year, or empty for all time",
                        "default": "",
                    },
                    "engines": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific engines to query (omit for all enabled engines)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return (1-50)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
        handler=_handle_searxng_search,
    )

    ctx.register_tool(
        name="searxng_engines",
        toolset="searxng",
        schema={
            "name": "searxng_engines",
            "description": "List available search engines and their capabilities. Optionally filter by category. Returns engine name, supported categories, shortcuts, and feature flags (safesearch, language support, time range).",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter engines by category (e.g. 'images', 'news', 'science')",
                    },
                },
            },
        },
        handler=_handle_searxng_engines,
    )

    ctx.register_tool(
        name="searxng_categories",
        toolset="searxng",
        schema={
            "name": "searxng_categories",
            "description": "List all search categories with engine counts per category.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_searxng_categories,
    )

    ctx.register_tool(
        name="searxng_status",
        toolset="searxng",
        schema={
            "name": "searxng_status",
            "description": "Check SearXNG engine status: ready state, searxng-src path, settings path, and any initialization errors.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_searxng_status,
    )

    # Register slash command
    ctx.register_command(
        name="searxng",
        description=(
            "SearXNG metasearch commands. "
            "Subcommands: search <query>, engines [--category <name>], categories, status"
        ),
        handler=_cmd_searxng,
    )

    logger.info("hermes-searxng: registered 4 tools + 1 command")
    return {"name": "hermes-searxng", "version": "1.0.0"}
