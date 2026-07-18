"""
hermes-searxng — Native metasearch for Hermes via SearXNG.

Embed SearXNG's search pipeline directly as a Python library (no Docker, no Flask server).
Query 170+ search engines through a single Hermes tool.

ARCHITECTURE:
  Instead of running SearXNG as a Flask web app, this plugin imports searx.search
  directly and calls SearchWithPlugins.initialize() + search() programmatically.
  This eliminates the entire HTTP layer, reduces latency, and removes the need
  for a separate server process.

  Settings are loaded from SearXNG's YAML config files. The plugin discovers
  the searxng-src checkout via HERMES_SEARXNG_SRC or the default repo path.

THREAD SAFETY:
  SearXNG has global mutable state (settings, registered engines). A module-level
  lock serializes all access. This is acceptable since SearXNG is called
  infrequently (agent searches, not user-driven queries).

DEPENDENCIES (JIT installed):
  flask, httpx, msgspec, pyyaml, babel, jinja2, markdown, certifi, idna, charset-normalizer
  These are SearXNG's runtime deps. The plugin installs them on first use.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── JIT dependency management ──────────────────────────────────────────────
try:
    from _shared.deps import DepSpec, ensure_deps

    _SEARXNG_DEPS: List[DepSpec] = [
        DepSpec("flask", ">=3.0.0"),
        DepSpec("httpx", ">=0.27.0"),
        DepSpec("msgspec", ">=0.18.0"),
        DepSpec("PyYAML", ">=6.0"),
        DepSpec("Babel", ">=2.14.0"),
        DepSpec("Jinja2", ">=3.1.0"),
        DepSpec("Markdown", ">=3.5.0"),
        DepSpec("certifi", ">=2024.0.0"),
        DepSpec("idna", ">=3.6"),
        DepSpec("charset-normalizer", ">=3.3.0"),
    ]

    def _ensure_searxng_deps() -> str | None:
        """Install SearXNG dependencies. Returns error string or None on success."""
        for spec in _SEARXNG_DEPS:
            err = ensure_deps([spec])
            if err:
                return err
        return None

except ImportError:
    def _ensure_searxng_deps() -> str | None:
        return "_shared.deps not available — cannot auto-install dependencies"


# ── SearXNG path discovery ─────────────────────────────────────────────────
_SEARXNG_SRC_CANDIDATES = [
    os.environ.get("HERMES_SEARXNG_SRC", ""),
    os.path.expanduser("~/.hermes/searxng/searxng-src"),
    os.path.expanduser("~/searxng/searxng-src"),
    "/usr/local/share/searxng/searxng-src",
    str(Path(__file__).resolve().parent.parent.parent / "searxng" / "searxng-src"),
]

_CACHED_SEARXNG_SRC: Optional[str] = None
_SEARXNG_LOCK = threading.Lock()


def _find_searxng_src() -> Optional[str]:
    """Locate the SearXNG searxng-src directory."""
    global _CACHED_SEARXNG_SRC
    if _CACHED_SEARXNG_SRC:
        return _CACHED_SEARXNG_SRC
    for path in _SEARXNG_SRC_CANDIDATES:
        if path and (Path(path) / "searx").is_dir():
            _CACHED_SEARXNG_SRC = path
            return path
    return None


def _resolve_settings_path() -> Optional[str]:
    """Resolve SearXNG settings.yml path. Respects SEARXNG_SETTINGS_PATH env."""
    env_path = os.environ.get("SEARXNG_SETTINGS_PATH", "")
    if env_path:
        return env_path
    src = _find_searxng_src()
    if src:
        return str(Path(src) / "searx" / "settings.yml")
    return None


# ── SearXNG engine singleton ───────────────────────────────────────────────
class _SearxngEngine:
    """Lazy singleton wrapping SearXNG's search pipeline.

    Thread-safe: all public methods acquire _SEARXNG_LOCK.
    Initialization happens once on first use.
    """

    def __init__(self):
        self._ready = False
        self._error: Optional[str] = None
        self._searx = None  # Module reference to searx package
        self._search_mod = None  # searx.search module

    def ensure_ready(self) -> Optional[str]:
        """Initialize SearXNG if not already loaded. Returns error or None."""
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

            # 2. Find searxng-src and add to path
            src = _find_searxng_src()
            if not src:
                self._error = (
                    "Cannot find searxng-src. Set HERMES_SEARXNG_SRC env var "
                    "or ensure ~/searxng/searxng-src exists."
                )
                return self._error

            sys.path.insert(0, src)

            # 3. Initialize SearXNG settings (must happen before imports)
            try:
                from searx import init_settings, settings
                from searx.settings_loader import load_settings

                settings_path = _resolve_settings_path()
                if settings_path and Path(settings_path).exists():
                    os.environ["SEARXNG_SETTINGS_PATH"] = settings_path

                init_settings()
                self._searx = __import__("searx")
            except Exception as e:
                self._error = f"SearXNG settings init failed: {e}"
                logger.error(self._error)
                return self._error

            # 4. Load engines
            try:
                from searx.engines import load_engines
                from searx.enginelib.traits import EngineTraitsMap

                load_engines()
                self._search_mod = __import__("searx.search", fromlist=["SearchWithPlugins", "SearchQuery"])
            except Exception as e:
                self._error = f"SearXNG engine load failed: {e}"
                logger.error(self._error)
                return self._error

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
        """Execute a SearXNG search and return structured results.

        Args:
            query: Search query string
            categories: Restrict to categories (e.g. ["general", "images"])
            lang: BCP 47 language code
            safesearch: 0=off, 1=moderate, 2=strict
            pageno: Page number (1-indexed)
            time_range: None or "day", "week", "month", "year"
            engines: Restrict to specific engine names
            max_results: Max results to return

        Returns:
            Dict with keys: results, engines_used, suggestions, answers, infoboxes
        """
        err = self.ensure_ready()
        if err:
            return {"error": err}

        with _SEARXNG_LOCK:
            try:
                from searx.search import SearchWithPlugins, SearchQuery
                from searx.query import RawTextQuery

                # Build search query
                disabled_engines = []
                raw_query = RawTextQuery(query, disabled_engines)
                raw_query.parse()

                search_query = SearchQuery(
                    query=query,
                    lang=lang,
                    safesearch=safesearch,
                    pageno=pageno,
                    time_range=time_range or "",
                    engines=engines or [],
                    categories=categories or [],
                )

                # Execute search
                search = SearchWithPlugins(search_query)
                search.search()

                # Extract results
                result_container = search.result_container
                results = []
                for r in result_container.get_ordered():
                    results.append({
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "content": r.get("content", ""),
                        "engine": r.get("engine", ""),
                        "category": r.get("category", ""),
                        "publishedDate": str(r.get("publishedDate", "")),
                        "thumbnail": r.get("thumbnail", ""),
                        "img_src": r.get("img_src", ""),
                    })
                    if len(results) >= max_results:
                        break

                return {
                    "results": results,
                    "engines_used": list(result_container.engines),
                    "suggestions": list(result_container.suggestions),
                    "answers": list(result_container.answers),
                    "infoboxes": [i.to_dict() if hasattr(i, "to_dict") else str(i) for i in result_container.infoboxes],
                    "number_of_results": result_container.number_of_results,
                    "paging": result_container.paging if hasattr(result_container, "paging") else False,
                }

            except Exception as e:
                logger.error("SearXNG search failed: %s", e)
                return {"error": f"Search failed: {e}"}

    def list_engines(self) -> List[Dict[str, Any]]:
        """List all registered search engines with their metadata."""
        err = self.ensure_ready()
        if err:
            return [{"error": err}]

        with _SEARXNG_LOCK:
            try:
                engines_list = []
                # Access engine registry from searx global state
                if self._searx and hasattr(self._searx, "engines"):
                    for name, engine in self._searx.engines.items():
                        engines_list.append({
                            "name": name,
                            "categories": getattr(engine, "categories", []),
                            "shortcut": getattr(engine, "shortcut", ""),
                            "engine_type": getattr(engine, "engine_type", "online"),
                            "language_support": getattr(engine, "language_support", False),
                            "safesearch": getattr(engine, "safesearch", False),
                            "time_range_support": getattr(engine, "time_range_support", False),
                            "about": str(getattr(engine, "about", {})),
                        })
                return engines_list
            except Exception as e:
                return [{"error": str(e)}]

    def list_categories(self) -> List[Dict[str, Any]]:
        """List all search categories with engine counts."""
        err = self.ensure_ready()
        if err:
            return [{"error": err}]

        with _SEARXNG_LOCK:
            try:
                cats = []
                if self._searx and hasattr(self._searx, "categories"):
                    for cat_name, cat_engines in self._searx.categories.items():
                        cats.append({
                            "name": cat_name,
                            "engine_count": len(cat_engines),
                            "engines": [e.name if hasattr(e, "name") else str(e) for e in cat_engines],
                        })
                return cats
            except Exception as e:
                return [{"error": str(e)}]

    def status(self) -> Dict[str, Any]:
        """Return plugin status."""
        return {
            "ready": self._ready,
            "error": self._error,
            "searxng_src": _CACHED_SEARXNG_SRC,
            "settings_path": _resolve_settings_path(),
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
        safesearch=int(args.get("safesearch", 0)),
        pageno=int(args.get("pageno", 1)),
        time_range=args.get("time_range"),
        engines=args.get("engines"),
        max_results=int(args.get("max_results", 20)),
    )
    return json.dumps(result, default=str)


def _handle_searxng_engines(args: dict, **kwargs: Any) -> str:
    """List available search engines and their capabilities."""
    result = _engine.list_engines()
    # Filter by category if requested
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
