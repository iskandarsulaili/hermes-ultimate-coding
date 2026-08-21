"""
hermes-anchored — Anchored Standard plugin for Hermes.

Ported from https://github.com/xiaobright/dsh-anchored-standard (MIT), the
"Anchored Standard" preset. The mechanism: DeepSeek V4 Pro conditions strongly
on the API-visible tool catalog — the first request's tool schema decides the
model's whole reasoning trajectory ("We need…" vs "Let me…"). Narrowing the
first request to a minimal tool set anchors the trajectory; promoting to a
resident set after the first request keeps the broader tooling available.

Hermes mapping (verified against hermes_cli/plugins.py + agent/conversation_loop.py):
  - `llm_request` middleware rewrites api_kwargs["tools"] + api_kwargs["max_tokens"]
    per request (conversation_loop.py:2945-2960; build_api_kwargs passes tools).
  - `pre_api_request` hook observes each request (session_id, api_call_count).
  - `on_session_start` / `on_session_end` hooks track session lifecycle.
  - Durable promotion state in ~/.hermes/anchored/state.json (survives restart/reboot).

Design:
  - request_count == 1  -> bootstrap tool set (anchors the trajectory)
  - request_count >= 2  -> resident set (bootstrap + dev_tool_search + unlocked)
  - Context gate: on request #1, strip injected context sections (skills digest,
    AGENTS.md) from the system prompt, keep the core persona. Degrades to
    keep-everything on any failure (never eats context).
  - dev_tool_search tool: search the full catalog + unlock tools by name.
  - Opt-in by default (like hermes-moa-trigger): /anchored-enable to activate.

Zero external deps (stdlib only). Survives hermes update (lives in
~/.hermes/plugins/ outside the venv), hermes restart (config.yaml), and system
reboot (on-disk state).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("hermes-anchored")

# ── Config (env overridable, garbage-safe) ──────────────────────────────────
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Persistent state dir (survives restart/reboot)
STATE_DIR = Path.home() / ".hermes" / "anchored"
STATE_FILE = STATE_DIR / "state.json"

# Default bootstrap tool set — the two most fundamental tools. Configurable.
DEFAULT_BOOTSTRAP_TOOLS = ["bash", "str_replace_editor"]
# Discovery tool always resident after promotion.
DISCOVERY_TOOL = "dev_tool_search"

# Lock for thread safety (handler -> internal-method lock chain)
_ANCHORED_LOCK = threading.RLock()

# In-memory state cache: session_id -> {request_count, promoted, unlocked}
_session_state: Dict[str, Dict[str, Any]] = {}
# Whether the plugin is enabled (opt-in, like moa-trigger)
_enabled = _env_bool("HERMES_ANCHORED_ENABLED", False)


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically (tmp + rename) so a crash never corrupts state."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("anchored: state write failed: %s", e)


def _load_state() -> Dict[str, Any]:
    """Load persisted state (survives restart/reboot)."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("anchored: state load failed: %s", e)
    return {}


def _save_state() -> None:
    with _ANCHORED_LOCK:
        _atomic_write(STATE_FILE, _session_state)


def _reset_session(session_id: str) -> None:
    with _ANCHORED_LOCK:
        _session_state[session_id] = {
            "request_count": 0,
            "promoted": False,
            "unlocked": [],
            "created": time.time(),
        }


def _get_session(session_id: str) -> Dict[str, Any]:
    with _ANCHORED_LOCK:
        st = _session_state.get(session_id)
        if st is None:
            st = {"request_count": 0, "promoted": False, "unlocked": [], "created": time.time()}
            _session_state[session_id] = st
        return st


def _tool_names(tools: Any) -> List[str]:
    """Extract tool names from the api_kwargs tools list (OpenAI/Anthropic shape)."""
    names = []
    if not isinstance(tools, list):
        return names
    for t in tools:
        if not isinstance(t, dict):
            continue
        # OpenAI: {"type": "function", "function": {"name": ...}}
        fn = t.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            names.append(fn["name"])
            continue
        # Anthropic: {"name": ...}
        if t.get("name"):
            names.append(t["name"])
    return names


def _filter_tools(tools: Any, keep: set) -> Any:
    """Filter the tools list to the keep-set. Returns the filtered list."""
    if not isinstance(tools, list):
        return tools
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and fn.get("name") in keep:
            out.append(t)
        elif t.get("name") in keep:
            out.append(t)
    return out


def _resident_keep(session_id: str, bootstrap: List[str]) -> set:
    """The resident set: bootstrap + discovery + unlocked."""
    st = _get_session(session_id)
    keep = set(bootstrap)
    keep.add(DISCOVERY_TOOL)
    for name in st.get("unlocked", []):
        keep.add(name)
    return keep


# ── Middleware: tool catalog bootstrap ──────────────────────────────────────
def _llm_request_middleware(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Rewrite api_kwargs["tools"] per request: bootstrap on #1, resident after."""
    try:
        if not _enabled:
            return None
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return None

        with _ANCHORED_LOCK:
            st = _get_session(session_id)
            st["request_count"] = st.get("request_count", 0) + 1
            request_count = st["request_count"]
            # Promotion: request #2+ is promoted (request #1 always bootstrap).
            if request_count >= 2:
                st["promoted"] = True

        tools = request.get("tools")
        if not isinstance(tools, list) or not tools:
            return None

        bootstrap = _bootstrap_tools()
        if request_count == 1:
            keep = set(bootstrap)
        else:
            keep = _resident_keep(session_id, bootstrap)

        filtered = _filter_tools(tools, keep)
        if len(filtered) == len(tools):
            return None  # nothing changed
        new_request = dict(request)
        new_request["tools"] = filtered
        return {"request": new_request}
    except Exception as e:
        # A middleware bug must never break the request path — degrade to no-op.
        logger.warning("anchored: llm_request middleware error: %s", e)
        return None


# ── Context gate: strip injected context on request #1 ─────────────────────
def _context_gate_middleware(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """On request #1, strip injected context sections from the system prompt.

    Conservative: only strips the skills digest + AGENTS.md digest sections,
    keeps the core persona. Degrades to keep-everything on any failure.
    """
    try:
        if not _enabled:
            return None
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return None
        st = _get_session(session_id)
        if st.get("request_count", 0) != 1:
            return None  # only strip on the very first request

        # OpenAI-wire: system prompt is messages[0] (role=system).
        messages = request.get("messages")
        if isinstance(messages, list):
            stripped = False
            new_messages = []
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "system":
                    content = m.get("content", "")
                    if isinstance(content, str) and _is_injected_context(content):
                        stripped = True
                        continue  # drop the injected section
                new_messages.append(m)
            if stripped:
                new_request = dict(request)
                new_request["messages"] = new_messages
                return {"request": new_request}

        # Anthropic-wire: system is a top-level field.
        system = request.get("system")
        if isinstance(system, str) and _is_injected_context(system):
            new_request = dict(request)
            new_request["system"] = _strip_injected(system)
            return {"request": new_request}

        return None
    except Exception as e:
        # A gate bug must never eat context — degrade to keep-everything.
        logger.warning("anchored: context gate error: %s", e)
        return None


def _is_injected_context(content: str) -> bool:
    """Heuristic: is this a system section that is injected context (not persona)?

    Matches the skills digest / AGENTS.md reminder markers. Keeps the core
    persona (which doesn't contain these markers).
    """
    low = content.lower()
    markers = [
        "available skills",
        "available_skills",
        "you have the following tools",
        "plugin usage instructions",
        "workflow priority",
        "mandatory rules",
        "quick reference",
        "troubleshooting",
    ]
    return any(m in low for m in markers)


def _strip_injected(system: str) -> str:
    """Strip injected-context markers from an Anthropic system string."""
    # Keep it simple: if the whole system is injected, blank it; otherwise
    # leave it (we can't reliably split sections in a plain string).
    if _is_injected_context(system):
        return ""
    return system


# ── dev_tool_search tool ────────────────────────────────────────────────────
def _handle_dev_tool_search(args: dict, **kwargs: Any) -> str:
    """Search the full tool catalog + unlock tools by name."""
    try:
        query = str(args.get("query", "") or "").strip()
        unlock = args.get("toolNames") or []
        if isinstance(unlock, str):
            unlock = [unlock]
        unlock = [u for u in unlock if isinstance(u, str) and u]

        session_id = str(kwargs.get("session_id") or "")
        lines = []
        if unlock:
            with _ANCHORED_LOCK:
                st = _get_session(session_id)
                existing = set(st.get("unlocked", []))
                for name in unlock:
                    existing.add(name)
                st["unlocked"] = sorted(existing)
            _save_state()
            lines.append(f"Unlocked for the next request: {', '.join(unlock)}")

        if not query and not unlock:
            lines.append("Provide `query` to search the catalog, or `toolNames` to unlock tools.")
            return json.dumps({"text": "\n".join(lines)})

        if query:
            # Search the full catalog via the tool registry.
            try:
                from tools.registry import registry
                entries = registry.get_all_entries() if hasattr(registry, "get_all_entries") else []
                wanted = [w for w in query.lower().split() if w]
                matches = []
                for entry in entries:
                    name = getattr(entry, "name", "")
                    desc = getattr(entry, "description", "") or ""
                    hay = f"{name} {desc}".lower()
                    if all(w in hay for w in wanted):
                        matches.append(f"- {name}: {desc[:90]}")
                if matches:
                    lines.append(f"Matching tools ({len(matches)}):")
                    lines.extend(matches[:25])
                    lines.append('Unlock with dev_tool_search({"toolNames": ["<exact name>"]}).')
                else:
                    lines.append(f'No tools match "{query}".')
            except Exception as e:
                lines.append(f"catalog search unavailable: {e}")

        return json.dumps({"text": "\n".join(lines)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── status tool ─────────────────────────────────────────────────────────────
def _handle_anchored_status(args: dict, **kwargs: Any) -> str:
    """Show the anchored plugin state."""
    try:
        session_id = str(kwargs.get("session_id") or "")
        st = _get_session(session_id) if session_id else {}
        return json.dumps({
            "enabled": _enabled,
            "session_id": session_id or None,
            "request_count": st.get("request_count", 0) if st else 0,
            "promoted": st.get("promoted", False) if st else False,
            "unlocked": st.get("unlocked", []) if st else [],
            "bootstrap_tools": _bootstrap_tools(),
            "state_file": str(STATE_FILE),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── slash commands ─────────────────────────────────────────────────────────
def _cmd_anchored(raw_args: str) -> str:
    """/anchored [enable|disable|status] — control the anchored plugin."""
    try:
        parts = raw_args.strip().split()
        sub = parts[0].lower() if parts else "status"
        global _enabled
        if sub == "enable":
            _enabled = True
            return "anchored: enabled — first request will use the bootstrap tool set."
        if sub == "disable":
            _enabled = False
            return "anchored: disabled — full tool catalog on every request."
        if sub == "status":
            return json.dumps(_handle_anchored_status({}), default=str, indent=2)
        return "anchored: usage — /anchored [enable|disable|status]"
    except Exception as e:
        return f"anchored: error: {e}"


# ── bootstrap tools (configurable) ─────────────────────────────────────────
def _bootstrap_tools() -> List[str]:
    raw = os.environ.get("HERMES_ANCHORED_BOOTSTRAP_TOOLS", ",".join(DEFAULT_BOOTSTRAP_TOOLS))
    return [t.strip() for t in raw.split(",") if t.strip()]


# ── hooks ──────────────────────────────────────────────────────────────────
def _on_session_start(session_id: str, **kwargs: Any) -> None:
    try:
        _reset_session(str(session_id))
    except Exception:
        pass


def _on_session_end(session_id: str, **kwargs: Any) -> None:
    try:
        with _ANCHORED_LOCK:
            _session_state.pop(str(session_id), None)
        _save_state()
    except Exception:
        pass


# ── registration ───────────────────────────────────────────────────────────
def register(ctx: Any) -> None:
    """Register the anchored plugin with Hermes."""
    # Load persisted state on startup (survives restart/reboot).
    global _session_state
    try:
        _session_state = _load_state()
    except Exception:
        _session_state = {}

    # llm_request middleware: tool catalog bootstrap + context gate.
    ctx.register_middleware("llm_request", _llm_request_middleware)
    ctx.register_middleware("llm_request", _context_gate_middleware)

    # Session lifecycle hooks.
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)

    # dev_tool_search tool.
    ctx.register_tool(
        name="dev_tool_search",
        toolset="anchored",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search keywords (e.g. 'web', 'subagent')"},
                "toolNames": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "exact tool names to unlock",
                },
            },
            "additionalProperties": False,
        },
        handler=_handle_dev_tool_search,
        description=(
            "Discover and unlock tools that are NOT currently available. "
            "This session starts with a minimal resident set; everything else is "
            "unlocked on demand through this tool. Pass `query` to search the "
            "catalog, then `toolNames` with exact names to unlock them."
        ),
        emoji="⚓",
    )

    # anchored_status tool.
    ctx.register_tool(
        name="anchored_status",
        toolset="anchored",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handle_anchored_status,
        description="Show the anchored plugin state (enabled, request count, promotion, unlocked tools).",
        emoji="⚓",
    )

    # /anchored slash command.
    ctx.register_command(
        name="anchored",
        handler=_cmd_anchored,
        description="Control the anchored plugin: /anchored [enable|disable|status]",
        args_hint="[enable|disable|status]",
    )

    logger.info("hermes-anchored: registered — llm_request middleware + dev_tool_search + anchored_status + /anchored")
