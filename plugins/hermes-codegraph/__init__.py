"""
hermes-codegraph — Deterministic AST-based code knowledge graph.

Wraps CodeGraph (colbymchenry/codegraph, 47.4k stars, MIT) as a native
Hermes plugin. Communicates via the codegraph Node.js CLI — no MCP server
needed. Auto-installs on first use.

SYNERGY with our other plugins:
  Graphify → semantic/LLM exploration (concepts, docs, cross-repo)
  CodeGraph → deterministic queries (callers, callees, impact, source)
  Semble     → semantic code search (fuzzy find where X does Y)
  LSP        → per-file diagnostics and definitions
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Config (env overridable) ─────────────────────────────────────────
CODEGRAPH_INSTALL_DIR = os.environ.get(
    "HERMES_CODEGRAPH_DIR",
    str(Path.home() / ".hermes" / "codegraph"),
)
CODEGRAPH_NODE_MIN = os.environ.get("HERMES_CODEGRAPH_NODE_MIN", "20.0.0")
CODEGRAPH_CLI_TIMEOUT = int(os.environ.get("HERMES_CODEGRAPH_TIMEOUT", "120"))

# ── State ─────────────────────────────────────────────────────────────
_lock = threading.RLock()
_initialized: Dict[str, bool] = {}  # project path → init status
_cg_bin: Optional[str] = None  # resolved codegraph binary path


def _resolve_codegraph() -> Optional[str]:
    """Find the codegraph binary: global install, npx, or our install dir."""
    global _cg_bin
    if _cg_bin:
        return _cg_bin

    # Check PATH
    cg = shutil.which("codegraph")
    if cg:
        _cg_bin = cg
        return cg

    # Check install dir
    local_bin = Path(CODEGRAPH_INSTALL_DIR) / "node_modules" / ".bin" / "codegraph"
    if local_bin.exists():
        _cg_bin = str(local_bin)
        return str(local_bin)

    # Check npx
    try:
        subprocess.run(
            ["npx", "--yes", "@colbymchenry/codegraph", "--version"],
            capture_output=True,
            timeout=30,
        )
        _cg_bin = "npx"
        return "npx"
    except Exception:
        pass

    return None


def _ensure_installed() -> Optional[str]:
    """Auto-install codegraph if not found. Idempotent — safe to call
    on every tool dispatch. Returns the binary path or None on failure."""
    with _lock:
        cg = _resolve_codegraph()
        if cg:
            return cg

        logger.info("codegraph: installing @colbymchenry/codegraph via npx...")
        try:
            inst_dir = Path(CODEGRAPH_INSTALL_DIR)
            inst_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["npm", "install", "@colbymchenry/codegraph"],
                cwd=str(inst_dir),
                capture_output=True,
                timeout=180,
            )
            _cg_bin = str(inst_dir / "node_modules" / ".bin" / "codegraph")
            logger.info("codegraph: installed at %s", _cg_bin)
            return _cg_bin
        except Exception as e:
            logger.warning("codegraph: auto-install failed: %s", e)
            return None


def _ensure_project(path: str) -> str:
    """Resolve project path and ensure it's initialized + indexed."""
    proj_path = os.path.abspath(path or os.getcwd())

    with _lock:
        if proj_path in _initialized:
            return proj_path

    cg = _resolve_codegraph()
    if not cg:
        return ""

    # Check if already initialized
    try:
        r = _run_cg(cg, ["status", "--json", proj_path])
        if r and r.get("initialized"):
            with _lock:
                _initialized[proj_path] = True
            return proj_path
    except Exception:
        pass

    # Initialize + index
    logger.info("codegraph: initializing %s...", proj_path)
    _run_cg(cg, ["init", proj_path])
    logger.info("codegraph: indexing %s...", proj_path)
    _run_cg(cg, ["index", proj_path])
    with _lock:
        _initialized[proj_path] = True
    return proj_path


def _run_cg(binary: str, args: List[str]) -> Optional[Dict[str, Any]]:
    """Run codegraph CLI and return parsed JSON output."""
    cmd = [binary] if binary != "npx" else ["npx", "--yes", "@colbymchenry/codegraph"]

    # --json flags for machine-readable output
    if args and args[0] not in ("init", "index", "install", "uninstall"):
        json_args = list(args)
        if "--json" not in json_args:
            json_args.insert(1, "--json")
        cmd.extend(json_args)
    else:
        cmd.extend(args)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CODEGRAPH_CLI_TIMEOUT,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            return {"error": err[:500]}
        if result.stdout.strip():
            # Try JSON parse
            out = result.stdout.strip()
            if out.startswith("{"):
                return json.loads(out)
            if out.startswith("["):
                return {"results": json.loads(out)}
            # Plain text output
            return {"output": out[:5000]}
        return {"output": "ok"}
    except subprocess.TimeoutExpired:
        return {"error": f"codegraph timed out after {CODEGRAPH_CLI_TIMEOUT}s"}
    except Exception as e:
        return {"error": str(e)}


def _cg_tool_run(args: List[str], project: str = "") -> str:
    """Run codegraph tool and format output for the agent."""
    cg = _ensure_installed()
    if not cg:
        return json.dumps({"error": "CodeGraph not installed. Install with: npm install -g @colbymchenry/codegraph"})

    proj = _ensure_project(project) if project else ""
    full_args = list(args)
    if proj:
        # Commands that accept positional path: init, status, sync, index, uninit
        _POSITIONAL_PATH = {"init", "status", "sync", "index", "uninit"}
        cmd = args[0] if args else ""
        if cmd in _POSITIONAL_PATH:
            full_args.append(proj)
        else:
            # All other commands use --path <path>
            full_args.extend(["--path", proj])

    result = _run_cg(cg, full_args)
    return json.dumps(result, default=str)


# ── Tools ─────────────────────────────────────────────────────────────

def _handle_codegraph_search(args: dict, **kwargs: Any) -> str:
    """Full-text search for symbols, files, and identifiers."""
    query = args.get("query", "")
    project = args.get("project", "")
    if not query:
        return json.dumps({"error": "query is required"})
    return _cg_tool_run(["query", query], project)


def _handle_codegraph_callers(args: dict, **kwargs: Any) -> str:
    """Find every caller of a function, method, or symbol."""
    symbol = args.get("symbol", "")
    project = args.get("project", "")
    if not symbol:
        return json.dumps({"error": "symbol is required"})
    return _cg_tool_run(["callers", symbol], project)


def _handle_codegraph_callees(args: dict, **kwargs: Any) -> str:
    """Find every callee of a function or method — what it calls."""
    symbol = args.get("symbol", "")
    project = args.get("project", "")
    if not symbol:
        return json.dumps({"error": "symbol is required"})
    return _cg_tool_run(["callees", symbol], project)


def _handle_codegraph_impact(args: dict, **kwargs: Any) -> str:
    """Blast radius analysis — what code is affected by changing a symbol.
    Returns callers, call chain, and dependency graph of affected code."""
    symbol = args.get("symbol", "")
    project = args.get("project", "")
    if not symbol:
        return json.dumps({"error": "symbol is required"})
    return _cg_tool_run(["impact", symbol], project)


def _handle_codegraph_explore(args: dict, **kwargs: Any) -> str:
    """One-call code exploration: describe a task or question and get
    relevant symbols, their source, call paths, and impact radius.
    Replaces multiple Read/Grep calls with one query."""
    task = args.get("task", "")
    project = args.get("project", "")
    if not task:
        return json.dumps({"error": "task description is required"})
    return _cg_tool_run(["context", task], project)


def _handle_codegraph_node(args: dict, **kwargs: Any) -> str:
    """Get symbol definition — location, signature, and verbatim source."""
    symbol = args.get("symbol", "")
    project = args.get("project", "")
    if not symbol:
        return json.dumps({"error": "symbol is required"})
    return _cg_tool_run(["query", symbol], project)


def _handle_codegraph_status(args: dict, **kwargs: Any) -> str:
    """Check CodeGraph index status for a project — last indexed, file
    count, symbol count, staleness."""
    project = args.get("project", "")
    return _cg_tool_run(["status", "--json"], project)


def _handle_codegraph_files(args: dict, **kwargs: Any) -> str:
    """List indexed files in the project, optionally filtered by extension."""
    project = args.get("project", "")
    return _cg_tool_run(["files"], project)


# ── Slash command ─────────────────────────────────────────────────────

def _cmd_codegraph(args: str, ctx: Any = None) -> str:
    """Interactive /codegraph slash command."""
    parts = args.strip().split()
    if not parts:
        return (
            "Usage: /codegraph <subcommand> [args]\n"
            "Subcommands:\n"
            "  search <query>       Search symbols\n"
            "  callers <symbol>     Find callers\n"
            "  callees <symbol>     Find callees\n"
            "  impact <symbol>      Blast radius analysis\n"
            "  explore <task>       Explore codebase for a task\n"
            "  status [project]     Index status\n"
            "  files [project]      Indexed files\n"
            "  init [project]       Initialize + index a project"
        )

    cmd = parts[0]
    rest = parts[1:]
    project = ""

    if cmd == "init":
        project = rest[0] if rest else os.getcwd()
        return _cg_tool_run(["init", project], "")
    if cmd == "search":
        return _handle_codegraph_search({"query": " ".join(rest)}, ctx=ctx)
    if cmd == "callers":
        return _handle_codegraph_callers({"symbol": " ".join(rest)}, ctx=ctx)
    if cmd == "callees":
        return _handle_codegraph_callees({"symbol": " ".join(rest)}, ctx=ctx)
    if cmd == "impact":
        return _handle_codegraph_impact({"symbol": " ".join(rest)}, ctx=ctx)
    if cmd == "explore":
        return _handle_codegraph_explore({"task": " ".join(rest)}, ctx=ctx)
    if cmd == "status":
        project = rest[0] if rest else ""
        return _handle_codegraph_status({"project": project}, ctx=ctx)
    if cmd == "files":
        project = rest[0] if rest else ""
        return _handle_codegraph_files({"project": project}, ctx=ctx)

    return f"Unknown subcommand: {cmd}. Use /codegraph for help."


# ── Register ──────────────────────────────────────────────────────────

def register(ctx) -> Dict[str, Any]:
    """Register the hermes-codegraph plugin."""

    ctx.register_tool(
        name="codegraph_search",
        toolset="codegraph",
        schema={
            "name": "codegraph_search",
            "description": "Full-text search for symbols, files, and identifiers in the codebase using CodeGraph's deterministic AST index. Faster and more precise than grep. Returns matching symbols with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (symbol name, identifier, or keyword)"},
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
                "required": ["query"],
            },
        },
        handler=_handle_codegraph_search,
    )

    ctx.register_tool(
        name="codegraph_callers",
        toolset="codegraph",
        schema={
            "name": "codegraph_callers",
            "description": "Find everything that calls a function, method, or symbol. Shows caller location, call site, and call chain depth. Deterministic — no LLM involved. Use instead of grep + read for understanding how a function is used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Function/method/symbol name"},
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
                "required": ["symbol"],
            },
        },
        handler=_handle_codegraph_callers,
    )

    ctx.register_tool(
        name="codegraph_callees",
        toolset="codegraph",
        schema={
            "name": "codegraph_callees",
            "description": "Find what a function or method calls (its callees). Shows the complete call tree — every function, method, and external call made by this symbol. Deterministic AST analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Function/method/symbol name"},
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
                "required": ["symbol"],
            },
        },
        handler=_handle_codegraph_callees,
    )

    ctx.register_tool(
        name="codegraph_impact",
        toolset="codegraph",
        schema={
            "name": "codegraph_impact",
            "description": "Blast radius analysis — find all code affected by changing a symbol. Shows the full ripple effect: callers, transitive dependencies, test files, and framework routes. Use BEFORE making changes to understand what your edit would break.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Function/method/symbol name to analyze"},
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
                "required": ["symbol"],
            },
        },
        handler=_handle_codegraph_impact,
    )

    ctx.register_tool(
        name="codegraph_explore",
        toolset="codegraph",
        schema={
            "name": "codegraph_explore",
            "description": "One-call codebase exploration for a task or question. Returns relevant symbols with verbatim source code, call paths, and impact radius — replacing multiple Read/Grep calls. The primary CodeGraph tool. Use for understanding how a feature works end-to-end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description or question about the codebase"},
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
                "required": ["task"],
            },
        },
        handler=_handle_codegraph_explore,
    )

    ctx.register_tool(
        name="codegraph_status",
        toolset="codegraph",
        schema={
            "name": "codegraph_status",
            "description": "Check CodeGraph index status for a project. Returns file count, symbol count, last indexed time, and staleness information. The index must be fresh for query accuracy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
            },
        },
        handler=_handle_codegraph_status,
    )

    ctx.register_tool(
        name="codegraph_files",
        toolset="codegraph",
        schema={
            "name": "codegraph_files",
            "description": "List indexed files in the project. Shows file paths, sizes, and modification times. Useful for understanding project structure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
            },
        },
        handler=_handle_codegraph_files,
    )

    ctx.register_tool(
        name="codegraph_node",
        toolset="codegraph",
        schema={
            "name": "codegraph_node",
            "description": "Get detailed information about a specific code symbol — its location, signature, type, and verbatim source code. Use when you need the full definition of a function, class, or method.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol name to look up"},
                    "project": {"type": "string", "description": "Project directory path (default: cwd)"},
                },
                "required": ["symbol"],
            },
        },
        handler=_handle_codegraph_node,
    )

    ctx.register_command(
        name="/codegraph",
        description="CodeGraph: code intelligence and knowledge graph commands",
        handler=_cmd_codegraph,
    )

    # Log capabilities on load
    logger.info(
        "codegraph: 8 tools registered — deterministic AST-based "
        "code queries (search, callers, callees, impact, explore). "
        "Auto-installs via npx on first use."
    )

    return {"name": "hermes-codegraph", "version": "1.0.0"}
