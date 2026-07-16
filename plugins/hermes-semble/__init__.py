"""
hermes-semble — Code search for Hermes via Semble.

Hybrid BM25 + semantic (Model2Vec) code search with tree-sitter AST chunking.
Uses ~98% fewer tokens than grep+read by returning only relevant code snippets.

DESIGN: Semble and grep complement each other:
  - Semble: semantic/concept search — "how is auth handled?", "find UserService"
  - grep: exact pattern/regex search — "grep -rn 'DEBUG_LOG' src/", "find all callers of function X"
  - read: full file context — already provided via Hermes read_file tool

Use them together. Semble narrows down what to look at; grep finds exact occurrences;
read gets full context.

Two complementary modes:
  1. semble_search — natural-language or symbol code search across the repo
  2. semble_find_related — discover code similar to a known location
  3. semble_stats — index statistics (files, chunks, languages)
  4. semble_reindex — force reindex after file changes

Survives Hermes updates by living entirely in ~/.hermes/plugins/.
Requires `semble` package installed (pip install semble).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure _shared package is discoverable (Hermes loads plugins in isolation)
_shared_dir = str(Path(__file__).resolve().parent.parent)
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)

from _shared.deps import DepSpec, ensure_deps

logger = logging.getLogger("hermes-semble")

# ---------------------------------------------------------------------------
# JIT dependency management
# ---------------------------------------------------------------------------
_SEMBLE_DEPS = [
    DepSpec(
        "semble",
        ["python3", "-c", "import semble"],
        install=[sys.executable, "-m", "pip", "install", "semble"],
        purpose="semantic code search (BM25 + Model2Vec)",
    ),
]

# Install dep BEFORE the module-level import attempt — otherwise the
# try/except ImportError below runs first and _SEMBLE_AVAILABLE stays
# False for the entire session.
ensure_deps("hermes-semble", _SEMBLE_DEPS, ask=True)

# =============================================================================
# Lazy singleton Semble engine — wraps Semble's async cache for synchronous use
# =============================================================================

_SEMBLE_AVAILABLE = False
_SEMBLE_IMPORT_ERROR: Optional[str] = None

try:
    from semble import SembleIndex
    from semble.cache import find_index_from_cache_folder, save_index_to_cache, clear_cache
    from semble.index.dense import load_model
    from semble.types import ContentType
    from semble.utils import resolve_chunk

    _SEMBLE_AVAILABLE = True
except ImportError as e:
    _SEMBLE_AVAILABLE = False
    _SEMBLE_IMPORT_ERROR = f"semble not installed (pip install semble): {e}"
except Exception as e:
    _SEMBLE_AVAILABLE = False
    _SEMBLE_IMPORT_ERROR = f"semble import error: {e}"


# =============================================================================
# Configuration from environment
# =============================================================================


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


_CACHE_MAX_SIZE = _env_int("HERMES_SEMBLE_CACHE_SIZE", 10)
_DEFAULT_TOP_K = _env_int("HERMES_SEMBLE_TOP_K", 5)
_DEFAULT_MAX_SNIPPET_LINES = _env_int("HERMES_SEMBLE_SNIPPET_LINES", 10)
_INDEX_TIMEOUT = _env_float("HERMES_SEMBLE_INDEX_TIMEOUT", 120.0)  # max seconds to wait for indexing


class _SembleEngine:
    """Lazy singleton that manages the Semble index cache for Hermes.

    Wraps Semble's async ``_IndexCache`` with synchronous methods suitable for
    Hermes tool handlers.  The embedding model is loaded on first use.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._model_lock = threading.Lock()  # separate lock for model loading (faster than RLock)
        self._model_path: Optional[str] = None
        self._model_error: Optional[BaseException] = None
        self._model_loaded = False
        self._indexes: OrderedDict[str, Any] = OrderedDict()  # LRU-ordered cache (move_to_end on access)

    def _ensure_model(self) -> str:
        """Load the embedding model once (thread-safe).

        Uses its own lock so it's safe to call from outside get_index's lock.
        load_model() is idempotent (caches internally), but we still guard
        against redundant calls with a dedicated lock.
        """
        if self._model_loaded:
            if self._model_error:
                raise RuntimeError(f"Embedding model failed to load: {self._model_error}")
            assert self._model_path is not None
            return self._model_path

        with self._model_lock:
            # Double-check under lock
            if self._model_loaded:
                if self._model_error:
                    raise RuntimeError(f"Embedding model failed to load: {self._model_error}")
                assert self._model_path is not None
                return self._model_path

            try:
                # load_model returns (model, model_path) tuple
                _, self._model_path = load_model()
                self._model_loaded = True
                logger.info("Embedding model loaded: %s", self._model_path)
            except Exception as e:
                self._model_error = e
                self._model_loaded = True
                raise RuntimeError(f"Failed to load embedding model: {e}")
            return self._model_path

    def _evict_lru(self) -> None:
        """Evict the oldest index if at capacity (caller must hold lock)."""
        while len(self._indexes) >= _CACHE_MAX_SIZE:
            oldest_key, _ = self._indexes.popitem(last=False)
            logger.info("Evicted cache entry: %s", oldest_key)

    def _touch(self, cache_key: str) -> None:
        """Mark a cache key as recently used (caller must hold lock)."""
        self._indexes.move_to_end(cache_key)

    def get_index(self, path: str) -> Any:
        """Get or build an index for a local directory (thread-safe).

        Returns the SembleIndex for *path*, building and caching it on first access.
        Cached indexes are evicted LRU when ``_CACHE_MAX_SIZE`` is exceeded.
        Indexing is wrapped with a timeout to prevent hangs on very large repos.
        Model loading happens outside the lock so it doesn't block other cache lookups.
        """
        cache_key = str(Path(path).resolve())

        with self._lock:
            # Fast path: cached and fresh
            cached = self._indexes.get(cache_key)
            if cached is not None:
                self._touch(cache_key)
                return cached

        # Ensure model is loaded (outside lock — model download can take 4-30s)
        model_path = self._ensure_model()

        with self._lock:
            # Double-check under lock after model load
            cached = self._indexes.get(cache_key)
            if cached is not None:
                self._touch(cache_key)
                return cached

            self._evict_lru()

            logger.info("Indexing: %s", path)
            try:
                # Wrap indexing with timeout to prevent hangs
                result: List[Any] = []

                def _build() -> None:
                    index = SembleIndex.from_path(path, model_path=model_path)
                    result.append(index)

                t = threading.Thread(target=_build, daemon=True)
                t.start()
                t.join(timeout=_INDEX_TIMEOUT)
                if not result:
                    raise TimeoutError(
                        f"Indexing timed out after {_INDEX_TIMEOUT}s for {path}. "
                        "Increase HERMES_SEMBLE_INDEX_TIMEOUT or exclude large directories."
                    )

                index = result[0]
                self._indexes[cache_key] = index

                # Save to disk cache for fast reload across sessions
                try:
                    save_index_to_cache(index, cache_key)
                except Exception:
                    pass

                logger.info(
                    "Indexed %s: %d files, %d chunks",
                    path,
                    index.stats.indexed_files,
                    index.stats.total_chunks,
                )
                return index
            except Exception as e:
                logger.error("Failed to index %s: %s", path, e)
                raise

    def search(
        self,
        path: str,
        query: str,
        top_k: int = _DEFAULT_TOP_K,
        max_snippet_lines: int | None = _DEFAULT_MAX_SNIPPET_LINES,
        filter_languages: Optional[List[str]] = None,
        filter_paths: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search a local directory and return structured results."""
        index = self.get_index(path)
        results = index.search(
            query,
            top_k=top_k,
            max_snippet_lines=max_snippet_lines,
            filter_languages=filter_languages,
            filter_paths=filter_paths,
        )
        output = []
        for r in results:
            output.append({
                "file_path": r.chunk.file_path,
                "start_line": r.chunk.start_line,
                "end_line": r.chunk.end_line,
                "language": r.chunk.language,
                "score": round(r.score, 4),
                "snippet": r.chunk.content[:500] if max_snippet_lines != 0 else "",
            })
        return output

    def find_related(
        self,
        path: str,
        file_path: str,
        line: int,
        top_k: int = _DEFAULT_TOP_K,
        max_snippet_lines: int | None = _DEFAULT_MAX_SNIPPET_LINES,
    ) -> List[Dict[str, Any]]:
        """Find code similar to a known location."""
        index = self.get_index(path)
        chunk = resolve_chunk(index.chunks, file_path, line)
        if chunk is None:
            return [{"error": f"No chunk found at {file_path}:{line}"}]
        results = index.find_related(chunk, top_k=top_k, max_snippet_lines=max_snippet_lines)
        output = []
        for r in results:
            output.append({
                "file_path": r.chunk.file_path,
                "start_line": r.chunk.start_line,
                "end_line": r.chunk.end_line,
                "language": r.chunk.language,
                "score": round(r.score, 4),
                "snippet": r.chunk.content[:500] if max_snippet_lines != 0 else "",
            })
        return output

    def stats(self, path: str) -> Dict[str, Any]:
        """Return index statistics for a local directory."""
        index = self.get_index(path)
        s = index.stats
        return {
            "indexed_files": s.indexed_files,
            "total_chunks": s.total_chunks,
            "languages": dict(s.languages),
        }

    def reindex(self, path: str) -> Dict[str, Any]:
        """Force reindex — evicts cache and rebuilds."""
        cache_key = str(Path(path).resolve())

        # Clear disk cache
        try:
            clear_cache(cache_key)
        except Exception:
            pass

        # Evict in-memory cache
        with self._lock:
            self._indexes.pop(cache_key, None)

        # Rebuild
        return self.stats(path)

    def cached_repos(self) -> List[str]:
        """Return list of all cached repo paths (thread-safe)."""
        with self._lock:
            return list(self._indexes.keys())

    def available(self) -> bool:
        """Return True if Semble is importable."""
        return _SEMBLE_AVAILABLE

    def import_error(self) -> Optional[str]:
        """Return the import error message if Semble is not available."""
        return _SEMBLE_IMPORT_ERROR


# Module-level singleton
_engine = _SembleEngine()


# =============================================================================
# Helper
# =============================================================================


# Capture cwd at import time for stable default repo resolution
_CWD = os.getcwd()


def _resolve_repo(repo: str) -> str:
    """Resolve ``repo`` parameter: if empty, use cwd; otherwise return as-is (local path).

    Captures cwd at import time so it's stable across the session.
    """
    if not repo or repo.strip() == "":
        return _CWD
    return repo.strip()


# =============================================================================
# Hermes Tool Handlers
# =============================================================================


def _handle_semble_search(args: dict, **kwargs: Any) -> str:
    """Handle semble_search tool call."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    query = args.get("query", "")
    repo = _resolve_repo(args.get("repo", ""))
    top_k = max(1, min(args.get("top_k", _DEFAULT_TOP_K), 50))
    max_snippet_lines = args.get("max_snippet_lines", _DEFAULT_MAX_SNIPPET_LINES)
    if max_snippet_lines is not None and max_snippet_lines < 0:
        max_snippet_lines = _DEFAULT_MAX_SNIPPET_LINES
    filter_languages = args.get("filter_languages", None)
    filter_paths = args.get("filter_paths", None)

    if not query:
        return json.dumps({"success": False, "error": "query is required"})

    try:
        results = _engine.search(
            repo, query,
            top_k=top_k,
            max_snippet_lines=max_snippet_lines,
            filter_languages=filter_languages,
            filter_paths=filter_paths,
        )
        return json.dumps({
            "success": True,
            "results": results,
            "count": len(results),
            "query": query,
            "repo": repo,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_semble_find_related(args: dict, **kwargs: Any) -> str:
    """Handle semble_find_related tool call."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    file_path = args.get("file_path", "")
    line = args.get("line", 0)
    if line < 0:
        line = 0
    repo = _resolve_repo(args.get("repo", ""))
    top_k = max(1, min(args.get("top_k", _DEFAULT_TOP_K), 50))
    max_snippet_lines = args.get("max_snippet_lines", _DEFAULT_MAX_SNIPPET_LINES)
    if max_snippet_lines is not None and max_snippet_lines < 0:
        max_snippet_lines = _DEFAULT_MAX_SNIPPET_LINES

    if not file_path:
        return json.dumps({"success": False, "error": "file_path is required"})

    try:
        results = _engine.find_related(repo, file_path, line, top_k=top_k, max_snippet_lines=max_snippet_lines)
        return json.dumps({
            "success": True,
            "results": results,
            "count": len(results),
            "source": {"file_path": file_path, "line": line},
            "repo": repo,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_semble_stats(args: dict, **kwargs: Any) -> str:
    """Handle semble_stats tool call."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    repo = _resolve_repo(args.get("repo", ""))

    try:
        stats = _engine.stats(repo)
        return json.dumps({
            "success": True,
            "stats": stats,
            "repo": repo,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_semble_reindex(args: dict, **kwargs: Any) -> str:
    """Handle semble_reindex tool call."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    repo = _resolve_repo(args.get("repo", ""))

    try:
        stats = _engine.reindex(repo)
        return json.dumps({
            "success": True,
            "stats": stats,
            "repo": repo,
            "message": f"Index rebuilt for {repo}",
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_semble_status(args: dict, **kwargs: Any) -> str:
    """Handle semble_status tool — check if Semble is available and report engine state."""
    try:
        available = _engine.available()
        with _engine._lock:
            model_loaded = _engine._model_loaded if available else False
            cached_indexes = len(_engine._indexes) if available else 0
        info = {
            "available": available,
            "model_loaded": model_loaded,
            "cached_indexes": cached_indexes,
            "max_cache_size": _CACHE_MAX_SIZE,
            "cached_repos": _engine.cached_repos() if available else [],
        }
        if not available:
            info["import_error"] = _engine.import_error()
        return json.dumps({"success": True, **info})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Slash command handler
# =============================================================================


def _cmd_semble(raw_args: str) -> str:
    """Handle the /semble slash command."""
    parts = raw_args.strip().split(maxsplit=1)
    subcommand = parts[0] if parts else "status"
    arg = parts[1] if len(parts) > 1 else ""

    if not _engine.available():
        return f"Error: {_engine.import_error()}"

    try:
        if subcommand == "status":
            info = _handle_semble_status({"action": "status"})
            result = json.loads(info) if isinstance(info, str) else info
            available = result.get("available", False)
            model_loaded = result.get("model_loaded", False)
            cached = result.get("cached_indexes", 0)
            return f"Semble: {'✓ available' if available else '✗ unavailable'}, model loaded: {model_loaded}, cached indexes: {cached}"

        elif subcommand == "search":
            if not arg:
                return "Usage: /semble search <query>"
            repo = _CWD
            result = json.loads(_handle_semble_search({"query": arg, "repo": repo}))
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            results = result.get("results", [])
            if not results:
                return f"No results for: {arg}"
            lines = [f"## Semble Search: {arg}"]
            for r in results[:5]:
                loc = f"{r['file_path']}:{r['start_line']}-{r['end_line']}"
                lang = r.get('language', '?')
                score = r.get('score', 0)
                snippet = r.get('snippet', '')
                lines.append(f"  [{lang}] {loc} (score={score})")
                if snippet:
                    lines.append(f"    {snippet[:200]}")
            return "\n".join(lines)

        elif subcommand == "stats":
            repo = arg if arg else _CWD
            result = json.loads(_handle_semble_stats({"repo": repo}))
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            s = result["stats"]
            return (
                f"## Semble Index Stats: {repo}\n"
                f"  Files indexed: {s['indexed_files']}\n"
                f"  Total chunks: {s['total_chunks']}\n"
                f"  Languages: {', '.join(f'{lang}: {count}' for lang, count in s.get('languages', {}).items())}"
            )

        elif subcommand == "reindex":
            repo = arg if arg else _CWD
            result = json.loads(_handle_semble_reindex({"repo": repo}))
            return f"Reindexed {repo}: {result.get('message', 'ok')}"

        else:
            return (
                "## Semble Commands\n"
                "  /semble status — check Semble status\n"
                "  /semble search <query> — search current repo\n"
                "  /semble stats [path] — index statistics\n"
                "  /semble reindex [path] — force reindex"
            )

    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# Hermes Plugin Registration
# =============================================================================


def register(ctx: Any) -> Dict[str, Any]:
    """Register the Hermes plugin — tools and slash commands."""
    logger.info("Registering hermes-semble plugin")

    if not _engine.available():
        logger.warning("Semble not available: %s", _engine.import_error())
        # Still register a status-only command so the user knows what's missing

    # Register tools
    ctx.register_tool(
        toolset="semble",
        name="semble_search",
        schema={
            "name": "semble_search",
        "description": (
            "Search code using natural language or symbol names via Semble. "
            "Returns relevant code snippets with exact file paths and line numbers. "
            "USE INSTEAD OF search_files/grep/terminal for concept-level code queries. "
            "Uses ~98% fewer tokens than grep+read by returning only matching chunks. "
            "BEST FOR: 'how is auth handled?', 'find the UserService class', 'where is rate limiting implemented', "
            "'find all places where errors are logged'. "
            "NOT good for: exact regex patterns, simple string lookup (use grep for that). "
            "Semble and grep complement each other — use Semble for semantic/concept search, "
            "grep via terminal for exact pattern matching. "
            "Write queries using function/class names or behavior descriptions. "
            "Navigate directly to the returned file paths — do not grep for the same content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language or code query (e.g. 'How is authentication handled?' or 'UserService.createUser').",
                },
                "repo": {
                    "type": "string",
                    "description": "Local directory path to search. Defaults to current working directory.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5).",
                    "default": _DEFAULT_TOP_K,
                },
                "max_snippet_lines": {
                    "type": "integer",
                    "description": "Lines of source per result (0=location only, 10=default, null=full chunk).",
                    "default": _DEFAULT_MAX_SNIPPET_LINES,
                },
                "filter_languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: only return results from these languages (e.g. ['python', 'typescript']).",
                },
                "filter_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: only return results from these file paths (repo-relative).",
                },
            },
            "required": ["query"],
        },
        },
        handler=_handle_semble_search,
    )

    ctx.register_tool(
        toolset="semble",
        name="semble_find_related",
        schema={
            "name": "semble_find_related",
        "description": (
            "Find code semantically similar to a known location. "
            "Useful for discovering all implementations of an interface, all callers of a function, "
            "or all tests for a class. "
            "USE INSTEAD OF read_file+terminal+grep to manually trace related code. "
            "Call after semble_search to explore related code without extra grep calls. "
            "BEST FOR: 'find all callers of this function', 'show me similar implementations'. "
            "For exact call-site search, use grep via terminal."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the source file (repo-relative, as returned by semble_search).",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-indexed) in the source file.",
                },
                "repo": {
                    "type": "string",
                    "description": "Local directory path to search. Defaults to current working directory.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5).",
                    "default": _DEFAULT_TOP_K,
                },
                "max_snippet_lines": {
                    "type": "integer",
                    "description": "Lines of source per result (0=location only, 10=default, null=full chunk).",
                    "default": _DEFAULT_MAX_SNIPPET_LINES,
                },
            },
            "required": ["file_path", "line"],
        },
        },
        handler=_handle_semble_find_related,
    )

    ctx.register_tool(
        toolset="semble",
        name="semble_stats",
        schema={
            "name": "semble_stats",
        "description": (
            "Return index statistics for a local directory: files indexed, total chunks, "
            "language breakdown. Use to verify that the codebase has been indexed correctly "
            "before running semble_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Local directory path. Defaults to current working directory.",
                },
            },
            "required": [],
        },
        },
        handler=_handle_semble_stats,
    )

    ctx.register_tool(
        toolset="semble",
        name="semble_reindex",
        schema={
            "name": "semble_reindex",
        "description": (
            "Force reindex of a local directory. Use after significant file changes "
            "(additions, deletions, renames) to refresh the search index. "
            "The index is normally cached; this clears the cache and rebuilds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Local directory path. Defaults to current working directory.",
                },
            },
            "required": [],
        },
        },
        handler=_handle_semble_reindex,
    )

    ctx.register_tool(
        toolset="semble",
        name="semble_status",
        schema={
            "name": "semble_status",
        "description": (
            "Check if Semble is available and report engine state — model loaded, "
            "cached indexes, max cache size."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
        },
        handler=_handle_semble_status,
    )

    # Register slash command
    ctx.register_command(
        name="semble",
        description=(
            "Semble code search commands. "
            "Subcommands: status, search <query>, stats [path], reindex [path]"
        ),
        handler=_cmd_semble,
    )

    logger.info("hermes-semble plugin registered: 5 tools, 1 command")
    return {"name": "hermes-semble", "version": "1.0.0"}
