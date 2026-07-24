"""
hermes-vault — Persistent memory vault for Hermes.

Wraps QMD (semantic search over Obsidian vaults) and implements the
Obsidian Mind vault structure conventions as a Hermes plugin.

ARCHITECTURE:
  QMD is a Node.js CLI tool that builds a SQLite index + embeddings
  over an Obsidian vault. It provides semantic search (BM25 + vector),
  exact retrieval, and batch reads — all scoped to a named index.

  This plugin discovers the vault by looking for vault-manifest.json
  in the current directory (or a parent). It auto-installs QMD via npm
  on first use.

  The vault structure follows Obsidian Mind conventions:
    work/        — Active projects, archive, incidents, 1:1s, meetings
    brain/       — Agent's operational knowledge (North Star, memories, decisions)
    org/         — People, teams, organizational context
    perf/        — Performance tracking, brag doc, competencies
    thinking/    — Drafts, scratchpads, reasoning

THREAD SAFETY:
  QMD CLI calls are serialized via a module-level lock. This is acceptable
  since vault operations are infrequent (agent-driven, not user-driven).

DEPENDENCIES (JIT installed):
  qmd — Node.js CLI for semantic search (npm install -g @tobilu/qmd)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Config (env overridable) ─────────────────────────────────────────────
VAULT_QMD_INDEX = os.environ.get("HERMES_VAULT_QMD_INDEX", "")
VAULT_NODE_MIN = os.environ.get("HERMES_VAULT_NODE_MIN", "22.0.0")
VAULT_CLI_TIMEOUT = int(os.environ.get("HERMES_VAULT_TIMEOUT", "60"))

# ── JIT dependency management ──────────────────────────────────────────────
try:
    from _shared.deps import DepSpec, ensure_deps

    _VAULT_DEPS: List[DepSpec] = [
        DepSpec(
            "qmd",
            ["node", "-e", "require('@tobilu/qmd')"],
            install=["npm", "install", "-g", "@tobilu/qmd"],
            purpose="semantic search over Obsidian vault (BM25 + embeddings)",
        ),
    ]

    def _ensure_vault_deps() -> str | None:
        """Install QMD if not found. Returns error string or None on success."""
        try:
            ensure_deps("hermes-vault", _VAULT_DEPS, ask=False)
            return None
        except Exception as e:
            return str(e)

except ImportError:
    def _ensure_vault_deps() -> str | None:
        return "_shared.deps not available — cannot auto-install dependencies"


# ── Vault discovery ───────────────────────────────────────────────────────
_VAULT_LOCK = threading.RLock()
_CACHED_VAULT_DIR: Optional[str] = None
_CACHED_MANIFEST: Optional[Dict[str, Any]] = None
_QMD_READY = False
_QMD_ERROR: Optional[str] = None


def _find_vault(start_dir: Optional[str] = None) -> Optional[str]:
    """Walk up from start_dir looking for vault-manifest.json."""
    global _CACHED_VAULT_DIR
    if _CACHED_VAULT_DIR:
        return _CACHED_VAULT_DIR

    search_dir = start_dir or os.getcwd()
    for parent in [search_dir] + list(Path(search_dir).parents):
        manifest = Path(parent) / "vault-manifest.json"
        if manifest.exists():
            _CACHED_VAULT_DIR = str(parent)
            return str(parent)
    return None


def _load_manifest(vault_dir: str) -> Optional[Dict[str, Any]]:
    """Load vault-manifest.json from the vault root."""
    global _CACHED_MANIFEST
    if _CACHED_MANIFEST:
        return _CACHED_MANIFEST

    manifest_path = Path(vault_dir) / "vault-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path) as f:
            _CACHED_MANIFEST = json.load(f)
        return _CACHED_MANIFEST
    except Exception as e:
        logger.warning("vault: failed to load manifest: %s", e)
        return None


def _get_qmd_index(vault_dir: str) -> str:
    """Get the QMD index name from manifest or env var."""
    if VAULT_QMD_INDEX:
        return VAULT_QMD_INDEX
    manifest = _load_manifest(vault_dir)
    if manifest and "qmd_index" in manifest:
        return manifest["qmd_index"]
    return "hermes-vault"


def _ensure_qmd() -> Optional[str]:
    """Ensure QMD is installed and ready. Returns error or None."""
    global _QMD_READY, _QMD_ERROR
    if _QMD_READY:
        return None
    with _VAULT_LOCK:
        if _QMD_READY:
            return None

        # 1. Check Node.js version
        try:
            r = subprocess.run(
                ["node", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                _QMD_ERROR = "Node.js not found — install Node.js 22+"
                return _QMD_ERROR
            version_str = r.stdout.strip().lstrip("v")
            major = int(version_str.split(".")[0])
            if major < 22:
                _QMD_ERROR = f"Node.js 22+ required, found {r.stdout.strip()}"
                return _QMD_ERROR
        except FileNotFoundError:
            _QMD_ERROR = "Node.js not found — install Node.js 22+"
            return _QMD_ERROR

        # 2. Install deps
        deps_err = _ensure_vault_deps()
        if deps_err:
            _QMD_ERROR = deps_err
            return deps_err

        # 3. Verify QMD works
        try:
            r = subprocess.run(
                ["qmd", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                _QMD_ERROR = f"qmd not working: {r.stderr[:200]}"
                return _QMD_ERROR
            _QMD_READY = True
            return None
        except Exception as e:
            _QMD_ERROR = f"qmd check failed: {e}"
            return _QMD_ERROR


def _run_qmd(args: List[str], timeout: int = VAULT_CLI_TIMEOUT) -> Dict[str, Any]:
    """Run a QMD CLI command and return parsed JSON result."""
    err = _ensure_qmd()
    if err:
        return {"error": err}

    vault_dir = _find_vault()
    if not vault_dir:
        return {"error": "No vault found (no vault-manifest.json in current or parent directories)"}

    index = _get_qmd_index(vault_dir)
    cmd = ["qmd", "--index", index] + args

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=vault_dir)
        if r.returncode != 0:
            return {"error": f"qmd failed: {r.stderr[:500]}"}
        # Try to parse as JSON
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"result": r.stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"error": f"qmd timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"qmd error: {e}"}


# ── Vault engine ─────────────────────────────────────────────────────────
class _VaultEngine:
    """Lazy singleton wrapping QMD for vault operations.

    Thread-safe: all public methods acquire _VAULT_LOCK.
    """

    def __init__(self):
        self._ready = False
        self._error: Optional[str] = None
        self._vault_dir: Optional[str] = None
        self._qmd_index: Optional[str] = None

    def ensure_ready(self) -> Optional[str]:
        """Discover vault and ensure QMD is ready. Returns error or None."""
        if self._ready:
            return None
        with _VAULT_LOCK:
            if self._ready:
                return None

            # 1. Find vault
            vault_dir = _find_vault()
            if not vault_dir:
                self._error = "No vault found. Create a vault with vault-manifest.json or set HERMES_VAULT_DIR."
                return self._error
            self._vault_dir = vault_dir

            # 2. Get QMD index name
            self._qmd_index = _get_qmd_index(vault_dir)

            # 3. Ensure QMD installed
            err = _ensure_qmd()
            if err:
                self._error = err
                return err

            self._ready = True
            return None

    def search(self, query: str, limit: int = 10) -> Dict[str, Any]:
        """Semantic search across the vault."""
        err = self.ensure_ready()
        if err:
            return {"error": err}

        with _VAULT_LOCK:
            result = _run_qmd(["query", query, "--limit", str(limit)])
            return result

    def get(self, title: str) -> Dict[str, Any]:
        """Get a specific note by title."""
        err = self.ensure_ready()
        if err:
            return {"error": err}

        with _VAULT_LOCK:
            result = _run_qmd(["get", title])
            return result

    def multi_get(self, titles: List[str]) -> Dict[str, Any]:
        """Get multiple notes by title."""
        err = self.ensure_ready()
        if err:
            return {"error": err}

        with _VAULT_LOCK:
            result = _run_qmd(["multi-get"] + titles)
            return result

    def reindex(self) -> Dict[str, Any]:
        """Force reindex of the vault."""
        err = self.ensure_ready()
        if err:
            return {"error": err}

        with _VAULT_LOCK:
            # update + embed
            update = _run_qmd(["update"])
            if "error" in update:
                return update
            embed = _run_qmd(["embed"])
            return embed

    def status(self) -> Dict[str, Any]:
        """Return plugin and vault status."""
        result: Dict[str, Any] = {
            "ready": self._ready,
            "vault_dir": self._vault_dir,
            "qmd_index": self._qmd_index,
        }
        if self._error:
            result["error"] = self._error
        return result

    def standup(self) -> str:
        """Generate a morning standup briefing from vault context."""
        err = self.ensure_ready()
        if err:
            return f"Vault not ready: {err}"

        vault_dir = self._vault_dir
        assert vault_dir is not None

        parts: List[str] = []

        # 1. North Star
        ns_path = Path(vault_dir) / "brain" / "North Star.md"
        if ns_path.exists():
            content = ns_path.read_text()[:1000]
            parts.append(f"## North Star\n{content}")

        # 2. Active work
        active_dir = Path(vault_dir) / "work" / "active"
        if active_dir.is_dir():
            notes = sorted(active_dir.glob("*.md"))
            if notes:
                parts.append("## Active Projects")
                for n in notes:
                    parts.append(f"- {n.stem}")

        # 3. Recent memories
        memories_path = Path(vault_dir) / "brain" / "Memories.md"
        if memories_path.exists():
            content = memories_path.read_text()[:500]
            parts.append(f"## Recent Memories\n{content}")

        # 4. Open tasks (from QMD)
        tasks = self.search("open tasks action items", limit=5)
        if "error" not in tasks:
            parts.append(f"## Open Tasks\n{json.dumps(tasks, indent=2)[:500]}")

        return "\n\n".join(parts)


_engine = _VaultEngine()


# ── Tool handlers ───────────────────────────────────────────────────────────
def _handle_vault_search(args: dict, **kwargs: Any) -> str:
    """Semantic search across the Obsidian vault."""
    query = args.get("query", "")
    if not query:
        return json.dumps({"error": "query is required"})

    limit = max(1, min(int(args.get("limit", 10)), 50))
    result = _engine.search(query=query, limit=limit)
    return json.dumps(result, default=str)


def _handle_vault_get(args: dict, **kwargs: Any) -> str:
    """Get a specific note by title."""
    title = args.get("title", "")
    if not title:
        return json.dumps({"error": "title is required"})

    result = _engine.get(title=title)
    return json.dumps(result, default=str)


def _handle_vault_multi_get(args: dict, **kwargs: Any) -> str:
    """Get multiple notes by title."""
    titles = args.get("titles", [])
    if not titles:
        return json.dumps({"error": "titles is required"})

    result = _engine.multi_get(titles=titles)
    return json.dumps(result, default=str)


def _handle_vault_reindex(args: dict, **kwargs: Any) -> str:
    """Force reindex of the vault."""
    result = _engine.reindex()
    return json.dumps(result, default=str)


def _handle_vault_status(args: dict, **kwargs: Any) -> str:
    """Check vault engine status."""
    return json.dumps(_engine.status(), default=str)


def _handle_vault_standup(args: dict, **kwargs: Any) -> str:
    """Generate a morning standup briefing from vault context."""
    result = _engine.standup()
    return json.dumps({"briefing": result}, default=str)


# ── Slash command handler ──────────────────────────────────────────────────
def _cmd_vault(raw_args: str) -> str:
    """Handle /vault slash command."""
    parts = raw_args.strip().split(maxsplit=2)
    if not parts:
        return (
            "Usage: /vault search <query> [options]\n"
            "       /vault get <title>\n"
            "       /vault multi-get <title1> <title2> ...\n"
            "       /vault reindex\n"
            "       /vault status\n"
            "       /vault standup\n"
        )

    subcmd = parts[0].lower()
    if subcmd == "status":
        return json.dumps(_engine.status(), default=str, indent=2)
    elif subcmd == "reindex":
        return json.dumps(_engine.reindex(), default=str, indent=2)
    elif subcmd == "standup":
        result = _engine.standup()
        return result
    elif subcmd == "search":
        query = parts[1] if len(parts) > 1 else ""
        if not query:
            return "Usage: /vault search <query>"
        limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 10
        result = _engine.search(query=query, limit=limit)
        return json.dumps(result, default=str, indent=2)
    elif subcmd == "get":
        title = parts[1] if len(parts) > 1 else ""
        if not title:
            return "Usage: /vault get <title>"
        result = _engine.get(title=title)
        return json.dumps(result, default=str, indent=2)
    elif subcmd == "multi-get":
        titles = parts[1:] if len(parts) > 1 else []
        if not titles:
            return "Usage: /vault multi-get <title1> <title2> ..."
        result = _engine.multi_get(titles=titles)
        return json.dumps(result, default=str, indent=2)
    else:
        return f"Unknown subcommand: {subcmd}"


# ── Plugin entry point ─────────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    """Register the hermes-vault plugin."""
    logger.info("Registering hermes-vault plugin")

    # Register tools
    ctx.register_tool(
        name="vault_search",
        toolset="vault",
        schema={
            "name": "vault_search",
            "description": "Semantic search across the Obsidian vault. Finds notes by meaning, not just keywords. Uses QMD (BM25 + embeddings). Best for: finding decisions, memories, or context when you don't know the exact filename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (1-50)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
        handler=_handle_vault_search,
    )

    ctx.register_tool(
        name="vault_get",
        toolset="vault",
        schema={
            "name": "vault_get",
            "description": "Get a specific note from the vault by title. Returns full note content with frontmatter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Note title (without .md extension)",
                    },
                },
                "required": ["title"],
            },
        },
        handler=_handle_vault_get,
    )

    ctx.register_tool(
        name="vault_multi_get",
        toolset="vault",
        schema={
            "name": "vault_multi_get",
            "description": "Get multiple notes from the vault by title. More efficient than calling vault_get repeatedly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of note titles to retrieve",
                    },
                },
                "required": ["titles"],
            },
        },
        handler=_handle_vault_multi_get,
    )

    ctx.register_tool(
        name="vault_reindex",
        toolset="vault",
        schema={
            "name": "vault_reindex",
            "description": "Force reindex of the vault. Updates the QMD index and re-embeds all notes. Run after bulk edits or new notes.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_vault_reindex,
    )

    ctx.register_tool(
        name="vault_status",
        toolset="vault",
        schema={
            "name": "vault_status",
            "description": "Check vault engine status: ready state, vault directory, QMD index name, and any initialization errors.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_vault_status,
    )

    ctx.register_tool(
        name="vault_standup",
        toolset="vault",
        schema={
            "name": "vault_standup",
            "description": "Generate a morning standup briefing from vault context. Reads North Star, active projects, recent memories, and open tasks.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_vault_standup,
    )

    # Register slash command
    ctx.register_command(
        name="vault",
        description=(
            "Vault commands for persistent memory. "
            "Subcommands: search <query>, get <title>, multi-get <t1> <t2>..., "
            "reindex, status, standup"
        ),
        handler=_cmd_vault,
    )

    logger.info("hermes-vault: registered 6 tools + 1 command")
    return {"name": "hermes-vault", "version": "1.0.0"}
