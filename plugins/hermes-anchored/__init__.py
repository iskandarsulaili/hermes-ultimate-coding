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
  - request_count >= 2  -> resident set = FULL catalog (all tools active)
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

# Default bootstrap tool set — the two most fundamental Hermes tools
# (shell + file editing), the analog of dsh's bash + str_replace_editor.
# Configurable via HERMES_ANCHORED_BOOTSTRAP_TOOLS.
DEFAULT_BOOTSTRAP_TOOLS = ["terminal", "patch"]
# Discovery tool always resident after promotion.
DISCOVERY_TOOL = "dev_tool_search"

# Lock for thread safety (handler -> internal-method lock chain)
_ANCHORED_LOCK = threading.RLock()

# In-memory state cache: session_id -> {request_count, promoted, created, last_seen}
_session_state: Dict[str, Dict[str, Any]] = {}

# Cap on how many sessions we retain in state.json. Each session is tiny
# (~4 small fields); this bounds both the in-memory dict and the on-disk
# state file so a long-lived gateway / many short-lived sessions can never
# grow the file without bound (there is no guaranteed on_session_end for
# crash-killed or subprocess-worker sessions).
_STATE_MAX_SESSIONS = _env_int("HERMES_ANCHORED_MAX_SESSIONS", 4096)
# Drop sessions older than this (seconds) even if their on_session_end never
# fired. Tunable; default 30 days. A session that is still actively calling
# the middleware refreshes its "created"/last-seen so live sessions are never
# evicted.
_STATE_SESSION_TTL_SECONDS = _env_int("HERMES_ANCHORED_SESSION_TTL", 60 * 60 * 24 * 30)


# Whether the plugin is enabled (default ON; set HERMES_ANCHORED_ENABLED=0 to opt out)
_enabled = _env_bool("HERMES_ANCHORED_ENABLED", True)


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
    """Load persisted state (survives restart/reboot).

    The state file stores ``{"enabled": bool, "sessions": {session_id: {...}}}``
    so an explicit ``/anchored enable``/``disable`` persists across
    restarts/reboots (the in-memory flag alone would reset on every start).
    """
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                raw = json.load(f)
                if isinstance(raw, dict) and "sessions" in raw:
                    return raw
                # Legacy/migrated shape: flat session map (no enabled marker).
                # Default the flag to the env/default (ON) — legacy files
                # predate persistence, so they should adopt the default.
                return {"enabled": None, "sessions": raw if isinstance(raw, dict) else {}}
    except Exception as e:
        logger.warning("anchored: state load failed: %s", e)
    return {"enabled": None, "sessions": {}}


def _save_state() -> None:
    with _ANCHORED_LOCK:
        _prune_state()  # keep the on-disk file bounded before writing
        _atomic_write(
            STATE_FILE,
            {"enabled": _enabled, "sessions": _session_state},
        )


def _reset_session(session_id: str) -> None:
    with _ANCHORED_LOCK:
        _session_state[session_id] = {
            "request_count": 0,
            "promoted": False,
            "created": time.time(),
            "last_seen": time.time(),
        }


def _get_session(session_id: str) -> Dict[str, Any]:
    """Get (or create) a session's state, refreshing its last-seen timestamp.

    The last-seen refresh ensures a session that keeps calling the middleware
    is never evicted by the TTL/cap prune, even if its on_session_end never
    fires (crash-killed / subprocess-worker / long-lived gateway sessions).
    """
    with _ANCHORED_LOCK:
        now = time.time()
        st = _session_state.get(session_id)
        if st is None:
            st = {"request_count": 0, "promoted": False, "created": now, "last_seen": now}
            _session_state[session_id] = st
            _prune_state(now)
        else:
            st["last_seen"] = now
        return st


def _prune_state(now: Optional[float] = None) -> None:
    """Enforce the TTL + cap bounds on the session state (memory + disk).

    Bounded state is a real requirement: there is NO guaranteed on_session_end
    for crash-killed or subprocess-worker sessions, so without this the
    in-memory dict and the persisted state.json grow without bound over a
    long-lived gateway. Call under _ANCHORED_LOCK.
    """
    now = time.time() if now is None else now
    try:
        if not _session_state:
            return
        # 1) Drop sessions idle past the TTL.
        if _STATE_SESSION_TTL_SECONDS > 0:
            cutoff = now - _STATE_SESSION_TTL_SECONDS
            stale = [
                sid for sid, st in _session_state.items()
                if (st.get("last_seen") or st.get("created") or 0) < cutoff
            ]
            for sid in stale:
                _session_state.pop(sid, None)
        # 2) If still over the cap, evict the least-recently-seen sessions.
        if len(_session_state) > _STATE_MAX_SESSIONS:
            ordered = sorted(
                _session_state.items(),
                key=lambda kv: (kv[1].get("last_seen") or kv[1].get("created") or 0),
            )
            excess = len(ordered) - _STATE_MAX_SESSIONS
            for sid, _st in ordered[:excess]:
                _session_state.pop(sid, None)
    except Exception as e:
        # A prune bug must never break the request path — degrade gracefully.
        logger.warning("anchored: state prune error: %s", e)


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
    """Resident set after promotion: the FULL catalog.

    Hermes' value is its 93-tool plugin ecosystem (searxng, agents, orchestra,
    lsp, vault, tdai, codegraph, ...). Narrowing the post-promotion catalog to
    bootstrap + dev_tool_search + manually-unlocked would HIDE all 15 other
    plugins' tools, contradicting the "maximize utilization of all tools"
    mandate. So after the turn-1 anchor, we keep the full catalog.

    (Returning the empty set tells the middleware to skip filtering.)
    """
    return set()


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
            # Turn-1 anchor: the minimal tool set + the discovery tool so the
            # model can unlock more on its very first request.
            keep = set(bootstrap)
            keep.add(DISCOVERY_TOOL)
        else:
            # Turn 2+: full catalog (resident set is empty => skip filtering).
            keep = _resident_keep(session_id, bootstrap)

        if not keep:
            return None  # full catalog, nothing to narrow

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


# ── Context gate: NOT APPLICABLE in Hermes ─────────────────────────────────
# dsh's context-gate strips injected context (skills digest, AGENTS.md) on the
# first request. In Hermes the system prompt is ONE atomic system message
# (persona + skills + memory + AGENTS.md all concatenated into a single
# role=system message — see agent/system_prompt.py volatile_parts). Stripping
# it would remove the persona too, which is a serious regression. The tool
# catalog narrowing (the PRIMARY, decisive lever in dsh's own evaluation) is
# the mechanism that transfers cleanly. The context-gate lever is deliberately
# NOT ported because it cannot be applied surgically in Hermes.
#
# Kept as a documented no-op so the design intent is explicit and a future
# Hermes that exposes separable system-prompt sections can enable it.
def _context_gate_middleware(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """No-op: Hermes' system prompt is atomic, so context stripping is unsafe.

    Returns None always (never modifies the request). The tool-catalog
    anchoring in _llm_request_middleware is the effective mechanism.
    """
    try:
        return None
    except Exception:
        return None


# ── dev_tool_search tool ────────────────────────────────────────────────────
def _handle_dev_tool_search(args: dict, **kwargs: Any) -> str:
    """Search the full tool catalog to discover what tools are available.

    On turn 1 the model's visible catalog is narrow (terminal, patch,
    dev_tool_search). This tool lets the model SEARCH the full catalog to
    learn what else exists. On turn 2+ the full catalog is already visible, so
    this is a convenience search. It does NOT gate the catalog — no "unlock"
    is needed because the resident (post-turn-1) set is the full catalog.
    """
    try:
        query = str(args.get("query", "") or "").strip()

        lines = []
        if not query:
            lines.append("Provide `query` to search the available tool catalog.")
            return json.dumps({"text": "\n".join(lines)})

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
            else:
                lines.append(f'No tools match "{query}".')
        except Exception as e:
            lines.append(f"catalog search unavailable: {e}")

        return json.dumps({"text": "\n".join(lines)})
    except Exception as e:
        # Return text envelope (not {"error": ...}) so Hermes' _detect_tool_failure
        # doesn't flag a false [error] tag on the tool result.
        return json.dumps({"text": f"dev_tool_search error: {e}"})


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
            "bootstrap_tools": _bootstrap_tools(),
            "state_file": str(STATE_FILE),
        })
    except Exception as e:
        # Text envelope (not {"error": ...}) to avoid a false [error] tag.
        return json.dumps({"text": f"anchored_status error: {e}"})


# ── slash commands ─────────────────────────────────────────────────────────
def _cmd_anchored(raw_args: str) -> str:
    """/anchored [enable|disable|status] — control the anchored plugin."""
    try:
        parts = raw_args.strip().split()
        sub = parts[0].lower() if parts else "status"
        global _enabled
        if sub == "enable":
            _enabled = True
            _save_state()  # persist so it survives restart/reboot
            return "anchored: enabled — first request will use the bootstrap tool set."
        if sub == "disable":
            _enabled = False
            _save_state()  # persist so it survives restart/reboot
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
    global _session_state, _enabled
    try:
        persisted = _load_state()
        # `sessions` holds the per-session map; `enabled` is the persisted flag.
        sessions = persisted.get("sessions", {})
        if isinstance(sessions, dict):
            _session_state = sessions
        else:
            _session_state = {}
        # Persisted enabled flag wins over the in-memory default.
        flag = persisted.get("enabled")
        if isinstance(flag, bool):
            _enabled = flag
        else:
            _enabled = _env_bool("HERMES_ANCHORED_ENABLED", True)
    except Exception:
        _session_state = {}
        _enabled = _env_bool("HERMES_ANCHORED_ENABLED", True)

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
            },
            "additionalProperties": False,
        },
        handler=_handle_dev_tool_search,
        description=(
            "Search the available tool catalog to discover what tools exist. "
            "On the first request only a minimal set (terminal, patch, this "
            "tool) is visible; use this to learn what else is available. "
            "Pass `query` keywords to find matching tools."
        ),
        emoji="⚓",
    )

    # anchored_status tool.
    ctx.register_tool(
        name="anchored_status",
        toolset="anchored",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_handle_anchored_status,
        description="Show the anchored plugin state (enabled, request count, promotion).",
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
