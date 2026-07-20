"""
hermes-searxng — Native metasearch for Hermes via SearXNG.

Queries the already-running SearXNG systemd service (port 8080) via its
REST API instead of embedding the full SearXNG Python pipeline. This
avoids blocking on asyncio event loop initialization, engine loading,
and HTTPX transport pool creation.

ARCHITECTURE:
  SearXNG runs as a systemd service (searxng.service) on port 8080.
  This plugin is a thin HTTP client that sends search queries to
  http://localhost:8080/search and parses the JSON response.

  No SearXNG Python imports needed — just httpx for HTTP requests.

THREAD SAFETY:
  httpx.Client is thread-safe. A module-level lock serializes access
  to the shared client instance.

DEPENDENCIES (JIT installed):
  httpx — for HTTP requests to the SearXNG service.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── JIT dependency management ──────────────────────────────────────────────
try:
    from _shared.deps import DepSpec, ensure_deps

    _SEARXNG_DEPS: List[DepSpec] = [
        DepSpec("httpx", ["python3", "-c", "import httpx"], install=["pip3", "install", "httpx"], purpose="HTTP client for SearXNG REST API"),
    ]

    def _ensure_searxng_deps() -> str | None:
        """Install SearXNG dependencies. Returns error string or None on success."""
        try:
            ensure_deps("hermes-searxng", _SEARXNG_DEPS, ask=False)
            return None
        except Exception as e:
            return str(e)

except ImportError:
    def _ensure_searxng_deps() -> str | None:
        return "_shared.deps not available — cannot auto-install dependencies"


# ── SearXNG REST client ──────────────────────────────────────────────────
_SEARXNG_BASE_URL = os.environ.get("SEARXNG_BASE_URL", "http://localhost:8080")
_SEARXNG_LOCK = threading.Lock()
_http_client: Optional[Any] = None


def _get_client() -> Any:
    """Lazy-init the shared httpx client."""
    global _http_client
    if _http_client is None:
        import httpx
        _http_client = httpx.Client(timeout=30.0)
    return _http_client


class _SearxngEngine:
    """Lazy singleton wrapping SearXNG's REST API.

    Thread-safe: all public methods acquire _SEARXNG_LOCK.
    """

    def __init__(self):
        self._ready = False
        self._error: Optional[str] = None

    def ensure_ready(self) -> Optional[str]:
        """Check if SearXNG service is reachable. Returns error or None."""
        if self._ready:
            return None
        with _SEARXNG_LOCK:
            if self._ready:
                return None

            # 1. Install deps
            deps_err = _ensure_searxng_deps()
            if deps_err:
                self._error = deps_err
                return deps_err

            # 2. Check if SearXNG is reachable
            try:
                client = _get_client()
                r = client.get(f"{_SEARXNG_BASE_URL}/search", params={"q": "ping", "format": "json"}, timeout=10.0)
                if r.status_code == 200:
                    self._ready = True
                    return None
                else:
                    self._error = f"SearXNG returned status {r.status_code}"
                    return self._error
            except Exception as e:
                self._error = f"SearXNG not reachable at {_SEARXNG_BASE_URL}: {e}"
                logger.error(self._error)
                return self._error

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
                r = client.get(f"{_SEARXNG_BASE_URL}/search", params=params, timeout=30.0)
                r.raise_for_status()
                data = r.json()

                # Extract results
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
                r = client.get(f"{_SEARXNG_BASE_URL}/engines", timeout=10.0)
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
                r = client.get(f"{_SEARXNG_BASE_URL}/engines", timeout=10.0)
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
        return {
            "ready": self._ready,
            "error": self._error,
            "searxng_url": _SEARXNG_BASE_URL,
        }


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
