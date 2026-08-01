"""
hermes-moa-trigger — automatic + on-demand max-reasoning planning for Hermes MoA.

WHY THIS PLUGIN EXISTS
----------------------
The MoA ``fanout`` cadence is a blunt instrument:

  - ``user_turn``     : advisor runs ONCE per turn, then never again — the
                        aggregator plans mid-loop at default reasoning only.
  - ``per_iteration`` : advisor re-runs on EVERY tool iteration — multiplies
                        advisor spend by tool-loop depth (the "feels stuck"
                        problem), and with a max-reasoning advisor it is the
                        slowest cadence available.

Both miss the actual need: the aggregator should think at MAX depth exactly
when a *planning moment* happens mid-loop — when it prepares a todo list /
plan (the 📋 plan moments), before a big refactor, after a test failure —
and not otherwise.

This plugin provides the missing piece with TWO trigger paths:

  1. AUTOMATIC (middleware): when the agent calls the ``todo`` tool with a
     ``todos`` write (the 📋 "preparing todo…" / "📋 plan" moments), a
     ``tool_execution`` middleware intercepts the call, runs a fresh
     max-reasoning advisory pass over the CURRENT conversation state, then
     lets the todo write proceed and APPENDS the advisory to the result.
     The agent reads the advice on its next thinking step and can refine the
     plan. Zero per-iteration multiplier: you pay one max pass per planning
     write, not per tool call.

  2. MANUAL (tool): ``planning_trigger`` — the agent calls it at any other
     planning moment mid-loop (before a large refactor, after a test
     failure, when the approach must change). Same max-reasoning machinery,
     invoked on demand.

Both paths reuse the exact MoA reference machinery: same advisory system
prompt, same message shaping (``_reference_messages``), same per-slot
reasoning_effort resolution, same ``call_llm`` chokepoint. The only
difference from the fan-out is WHEN they fire: on planning events instead
of on a fixed cadence.

DETECTION
---------
- Automatic: ``todo`` tool invoked with a ``todos`` array (a plan write) —
  the 📋 "preparing todo…" / "📋 plan" moments. ALWAYS fires — every plan
  write gets a fresh max-reasoning pass, with no cooldown or cost gate.
  Toggle with env ``HERMES_MOA_TRIGGER_ON_TODO`` (default "1"); set "0" to
  disable the automatic path and rely on the manual tool only.
- Manual: the agent's own judgment, expressed as a ``planning_trigger``
  tool call.
- First user/subagent message: already covered by the MoA preset itself
  (``fanout: user_turn`` runs the advisor once at turn start / subagent
  kickoff), so every new task begins with a max-reasoning pass.

DEPENDENCIES: none beyond Hermes core (stdlib + Hermes agent modules).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PRESET = "max-think-def-output"

# Per-session last-seen todo content snapshot {session_id: {id: content}} —
# used to distinguish real plan changes from status-only echoes.
_LAST_TODO_STATE: Dict[str, Dict[str, str]] = {}

# Live MoA facades tracked by /moa-flush. The plugin cannot see the agent
# from a slash-command handler (signature: fn(raw_args)), so it records
# every facade as it is built (wrap of build_moa_facade, the single
# construction point) and flushes their turn-scoped reference caches on
# demand. Same runtime-wrap pattern hermes-tps uses for call_llm.
_LIVE_FACADES: List[Any] = []
_FACADE_WRAP_DONE = False


def _ensure_facade_tracking() -> None:
    """Wrap build_moa_facade to record live MoAClient instances.

    Idempotent (marker attr on the wrapper, like hermes-tps's call_llm
    wrap). Called at register() and lazily from the flush handler so the
    tracking exists even if the plugin loads after the agent's client was
    built (session resume, model switch).
    """
    global _FACADE_WRAP_DONE
    if _FACADE_WRAP_DONE:
        return
    try:
        import agent.moa_loop as _moa
    except Exception:
        return
    _orig = getattr(_moa, "build_moa_facade", None)
    if _orig is None or getattr(_orig, "_moa_flush_wrapped", False):
        _FACADE_WRAP_DONE = True
        return

    def _wrapped(agent: Any, preset_name: Any = None) -> Any:
        facade = _orig(agent, preset_name)
        if facade is not None and facade not in _LIVE_FACADES:
            # Track the facade AND its session so /moa-flush can run the
            # immediate advisory against the live conversation.
            _LIVE_FACADES.append(
                {
                    "facade": facade,
                    "session_id": str(getattr(agent, "session_id", "") or ""),
                }
            )
        return facade

    _wrapped._moa_flush_wrapped = True  # type: ignore[attr-defined]
    _moa.build_moa_facade = _wrapped
    _FACADE_WRAP_DONE = True


def _flush_facades() -> int:
    """Invalidate turn-scoped reference caches on all live facades.

    The MoA loop's user_turn cadence caches reference advice keyed by the
    turn prefix; mid-turn tool iterations reuse it (the "stale reference").
    Resetting _ref_cache_key/_ref_cache_outputs makes the next aggregator
    create() a cache MISS, so the max-reasoning advisor re-runs against the
    FULL current state (grown tool history included).
    """
    n = 0
    for entry in list(_LIVE_FACADES):
        try:
            facade = entry.get("facade") if isinstance(entry, dict) else entry
            if facade is not None and hasattr(facade, "_ref_cache_key"):
                facade._ref_cache_key = None
                facade._ref_cache_outputs = []
                n += 1
        except Exception:
            continue
    return n


def _handle_moa_flush(raw_args: str) -> str:
    """Slash command /moa-flush — drop the stale reference, force refresh.

    Flushes the built-in MoA reference cache so the next aggregator step
    re-runs the max-reasoning advisor against the current live state, AND
    runs one advisory pass immediately so the user gets fresh advice now.
    """
    _ensure_facade_tracking()
    flushed = _flush_facades()
    # Current session: the most recent tracked facade's session (the CLI
    # runs one agent at a time; the last-built facade is this session's).
    session_id = ""
    for entry in reversed(list(_LIVE_FACADES)):
        sid = entry.get("session_id") if isinstance(entry, dict) else ""
        if sid:
            session_id = sid
            break
    # Also run an immediate fresh advisory on the live conversation state
    # (like planning_trigger does), so the flush is useful right away even
    # before the next aggregator step.
    fresh = ""
    try:
        slot = _resolve_reference_slot(DEFAULT_PRESET)
        if slot is not None:
            focus = str(raw_args or "").strip()
            messages = _load_conversation(session_id)
            ref_messages = _reference_messages(messages) if messages else []
            if focus:
                ref_messages = [
                    *ref_messages,
                    {
                        "role": "user",
                        "content": (
                            f"FOCUS FOR THIS FRESH ADVISORY PASS: {focus}. "
                            f"Give advice on this specifically, grounded in the "
                            f"current task state."
                        ),
                    },
                ]
            fresh = _run_max_advisor(slot, ref_messages)
    except Exception as exc:
        fresh = f"[moa-flush: immediate advisory failed — {exc}]"
    base = (
        f"MoA reference cache flushed ({flushed} facade(s)). "
        f"The next aggregator step will re-run the max-reasoning advisor "
        f"against the current live state."
    )
    if fresh and not fresh.startswith("[moa-flush"):
        return f"{base}\n\nFresh advisory (max reasoning, current state):\n{fresh[:4000]}"
    return base + (f"\n\n{fresh}" if fresh else "")


def _todo_auto_enabled() -> bool:
    return os.environ.get("HERMES_MOA_TRIGGER_ON_TODO", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# ── Conversation access ────────────────────────────────────────────────────
def _load_conversation(session_id: str) -> List[Dict[str, Any]]:
    """Load active messages for *session_id* from the session DB (any profile)."""
    if not session_id:
        return []
    try:
        from pathlib import Path

        from hermes_cli import profiles as profiles_mod
        from hermes_state import SessionDB

        targets = [("default", profiles_mod.get_profile_dir("default"))]
        try:
            targets += [
                (info.name, info.path) for info in profiles_mod.list_profiles()
            ]
        except Exception:
            logger.debug("list_profiles failed during session locate", exc_info=True)

        seen: set = set()
        for _name, home in targets:
            db_path = Path(home) / "state.db"
            key = str(db_path)
            if key in seen or not db_path.exists():
                continue
            seen.add(key)
            try:
                pdb = SessionDB(db_path=db_path, read_only=True)
            except Exception:
                continue
            try:
                if pdb.get_session(session_id):
                    msgs = pdb.get_messages(session_id)
                    pdb.close()
                    return msgs or []
            except Exception:
                logger.debug("get_messages probe failed for %s", session_id, exc_info=True)
            try:
                pdb.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("conversation load failed: %s", exc)
    return []


# ── MoA reference machinery reuse ──────────────────────────────────────────
def _reference_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Shape the advisory view exactly like MoA's reference fan-out."""
    try:
        from agent.moa_loop import _reference_messages as _moa_ref

        return _moa_ref(messages)
    except Exception:
        # Fallback: minimal text-only flattening (system dropped, tool results
        # inlined as text) — same invariants as the MoA shaper.
        rendered: List[Dict[str, Any]] = []
        last_tool_owner: Optional[int] = None
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if isinstance(content, list):
                text = " ".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                text = str(content or "")
            if role == "system":
                continue
            if role == "user":
                if not text.strip():
                    text = "[non-text user turn]"
                rendered.append({"role": "user", "content": text})
                last_tool_owner = None
            elif role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                lines = [text] if text.strip() else []
                for tc in tool_calls:
                    fn = (tc.get("function") or {}).get("name", "")
                    args = (tc.get("function") or {}).get("arguments", "")
                    lines.append(f"[called tool: {fn}({args})]")
                rendered.append({"role": "assistant", "content": "\n".join(lines)})
                last_tool_owner = len(rendered) - 1
            elif role == "tool" and last_tool_owner is not None:
                prev = rendered[last_tool_owner]
                prev["content"] = (
                    f"{prev['content']}\n[tool result: {text[:2000]}]"
                )
        if rendered and rendered[-1]["role"] != "user":
            rendered.append(
                {
                    "role": "user",
                    "content": (
                        "The conversation above is the current state of the task. "
                        "Give your most intelligent analysis and concrete next steps."
                    ),
                }
            )
        return rendered


def _resolve_reference_slot(preset_name: str) -> Optional[Dict[str, Any]]:
    """Return the first enabled max-reasoning reference slot from a MoA preset."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.moa_config import normalize_moa_config, resolve_moa_preset

        cfg = load_config() or {}
        moa = normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
        preset = resolve_moa_preset(moa, preset_name or None)
        refs = preset.get("reference_models") or []
        for slot in refs:
            if str(slot.get("reasoning_effort") or "").strip().lower() in (
                "max",
                "ultra",
                "xhigh",
            ):
                return dict(slot)
        return dict(refs[0]) if refs else None
    except Exception as exc:
        logger.debug("reference slot resolution failed: %s", exc)
        return None


def _run_max_advisor(slot: Dict[str, Any], ref_messages: List[Dict[str, Any]]) -> str:
    """Run ONE max-reasoning advisory pass through the same call_llm path MoA uses.

    Failure messages name the failing stage (resolve/build/call/extract) so
    "advisory unavailable" is diagnosable instead of opaque. Transient
    failures (timeout/429/5xx) are retried once with a 5s backoff before
    giving up.
    """
    stage = "resolve"
    try:
        from agent.auxiliary_client import call_llm

        from hermes_cli.runtime_provider import resolve_runtime_provider

        provider = str(slot.get("provider") or "")
        model = str(slot.get("model") or "")
        rt = resolve_runtime_provider(requested=provider, target_model=model)
        stage = "build"
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a reference advisor in a Mixture of Agents (MoA) "
                    "process. You are NOT the acting agent and you do NOT execute "
                    "anything. Analyze the current task state below and give your "
                    "most intelligent advice: the best approach, concrete next "
                    "steps, likely pitfalls, and anything the acting agent may "
                    "have missed. Respond with advice directly — no preamble."
                ),
            },
            *ref_messages,
        ]
        reasoning = {
            "enabled": True,
            "effort": str(slot.get("reasoning_effort") or "max"),
        }
        # Trim to the reference model's context window, exactly like the MoA
        # fan-out does — a long session would otherwise 400 on overflow.
        try:
            from agent.moa_loop import _trim_messages_for_reference

            messages = _trim_messages_for_reference(
                messages,
                slot,
                rt,
                reserve_output_tokens=None,
            )
        except Exception:
            pass

        def _call() -> Any:
            return call_llm(
                task="moa_reference",
                provider=rt.get("provider") or provider,
                model=rt.get("model") or model,
                base_url=rt.get("base_url"),
                api_key=rt.get("api_key"),
                api_mode=rt.get("api_mode"),
                messages=messages,
                reasoning_config=reasoning,
                timeout=120,
            )

        stage = "call"
        try:
            resp = _call()
        except Exception as exc:
            # One retry for transient failures (timeout / 429 / 5xx /
            # connection) — under gateway contention a single hiccup must
            # not permanently drop the advisory.
            msg = f"{type(exc).__name__} {exc}".lower()
            if any(
                h in msg
                for h in (
                    "timeout",
                    "timed out",
                    "429",
                    "rate limit",
                    "rate_limit",
                    "503",
                    "502",
                    "500",
                    "connection",
                    "econnreset",
                    "service unavailable",
                    "overloaded",
                    "busy",
                    "temporarily",
                    "try again",
                )
            ):
                import time as _t

                _t.sleep(5)
                resp = _call()
            else:
                raise

        stage = "extract"
        # Use the SAME response->text extractor the MoA loop uses, so the
        # advice is clean text, not a ChatCompletion repr.
        try:
            from agent.moa_loop import _extract_text

            text = _extract_text(resp)
        except Exception:
            text = ""
        if not text:
            text = getattr(resp, "content", None)
            if text is None and isinstance(resp, dict):
                text = resp.get("content")
            if text is None:
                text = str(resp)
        return str(text)
    except Exception as exc:
        logger.warning("planning_trigger advisor call failed: %s", exc)
        return f"[planning_trigger: advisor call failed — {exc}]"


# ── Automatic trigger: tool_execution middleware on `todo` ────────────────
def _todo_planning_middleware(
    tool_name: str,
    args: Dict[str, Any],
    next_call: Callable[[Any], Any],
    **context: Any,
) -> Any:
    """Intercept `todo` plan writes: let todo proceed, then run max advisor.

    The todo write executes FIRST (fast, in-memory — never blocked or lost
    on advisor interruption). Then the max-reasoning advisor runs against the
    current state, and its advice is appended to the tool result so the agent
    reads it on its next thinking step and can refine the plan (merge=true)
    if the advice suggests changes.
    """
    if tool_name != "todo":
        return next_call(args)

    todos = args.get("todos")
    is_plan_write = isinstance(todos, (list, str)) and bool(todos)
    if not is_plan_write:
        # Reading the current list is not a planning moment.
        return next_call(args)
    if not _todo_auto_enabled():
        return next_call(args)

    # Distinguish a PLAN WRITE from status bookkeeping. The todo tool is
    # also called with merge=true to flip statuses mid-execution (mark
    # complete, set in_progress) — those are NOT planning moments. A plan
    # write is a non-merge replace, or a merge that carries REAL changes:
    # new ids, removed ids, or changed content (not just status flips).
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except Exception:
            return next_call(args)
    merge = bool(args.get("merge", False))
    session_id = str(context.get("session_id") or "")
    if merge:
        incoming = {
            str(t.get("id")): str(t.get("content") or "").strip()
            for t in todos
            if isinstance(t, dict) and t.get("id") is not None
        }
        last = _LAST_TODO_STATE.get(session_id, {})
        if last:
            # Every incoming item already exists with identical content →
            # status-only flip / echo, not a planning moment. Skip.
            changed = any(
                iid not in last or last[iid] != icontent
                for iid, icontent in incoming.items()
            )
            if not changed:
                return next_call(args)
        elif not any(
            isinstance(t, dict) and str(t.get("content") or "").strip()
            for t in todos
        ):
            # First write in session with no content at all — pure flip.
            return next_call(args)

    # Signal the TUI status-bar indicator (hermes-tps) that a planning
    # trigger fired. Best-effort: the counter module is optional.
    try:
        from hermes_plugins.hermes_tps import (
            record_plugin_call as _tps_record,  # type: ignore
        )
    except Exception:
        try:
            from hermes_tps import record_plugin_call as _tps_record  # type: ignore
        except Exception:
            _tps_record = None
    if _tps_record is not None:
        try:
            _tps_record("moa-trigger")
        except Exception:
            pass

    # 1. Execute the plan write FIRST — fast, safe, never lost on interrupt.
    result = next_call(args)

    # Record the post-write content snapshot so the NEXT merge write can
    # tell a real plan change from a status-only echo.
    try:
        _payload = json.loads(result) if isinstance(result, str) else result
        if isinstance(_payload, dict):
            _items = _payload.get("items") or _payload.get("todos")
            if isinstance(_items, list):
                _LAST_TODO_STATE[session_id] = {
                    str(i.get("id")): str(i.get("content") or "").strip()
                    for i in _items
                    if isinstance(i, dict) and i.get("id") is not None
                }
    except Exception:
        pass

    # 2. Now run the advisory pass against the current state.
    slot = _resolve_reference_slot(DEFAULT_PRESET)
    advice = ""
    if slot is not None:
        messages = _load_conversation(session_id)
        ref_messages = _reference_messages(messages) if messages else []
        # Ground the advisor in the plan being written — the todo write just
        # happened, so its content is the thing to critique.
        plan_preview = json.dumps(todos, ensure_ascii=False)[:4000]
        ref_messages = [
            *ref_messages,
            {
                "role": "user",
                "content": (
                    f"THE PLAN JUST WRITTEN (todo tool input): {plan_preview}. "
                    f"Critique this plan specifically: is it complete, correctly "
                    f"sequenced, and free of gaps? Give concrete improvements."
                ),
            },
        ]
        if ref_messages:
            advice = _run_max_advisor(slot, ref_messages)
        else:
            advice = "[planning_trigger: no conversation state available]"
    else:
        advice = (
            f"[planning_trigger: no reference slot for preset "
            f"'{DEFAULT_PRESET}' — configure moa.presets.{DEFAULT_PRESET}]"
        )

    # Append the advisory to the JSON result without breaking the shape the
    # agent expects from the todo tool.
    try:
        payload = json.loads(result)
        if isinstance(payload, dict):
            payload["planning_advice"] = advice
            return json.dumps(payload, ensure_ascii=False)
    except Exception:
        pass
    return f"{result}\n\n[planning_advice] {advice}"


# ── Manual trigger: planning_trigger tool ─────────────────────────────────
def _handle_planning_trigger(args: Dict[str, Any], **kwargs: Any) -> str:
    """Force a fresh max-reasoning advisory pass over the current task state."""
    focus = str(args.get("focus") or "").strip()
    preset_name = str(args.get("preset") or "").strip() or DEFAULT_PRESET
    session_id = str(kwargs.get("session_id") or "").strip()

    messages = _load_conversation(session_id)
    if messages:
        ref_messages = _reference_messages(messages)
    else:
        ref_messages = [
            {
                "role": "user",
                "content": (
                    f"Current task state (live conversation unavailable — reason "
                    f"from this focus): {focus or 'no focus provided'}"
                ),
            }
        ]

    slot = _resolve_reference_slot(preset_name)
    if slot is None:
        return json.dumps(
            {
                "error": (
                    f"No usable reference slot found for MoA preset "
                    f"'{preset_name}'. Configure moa.presets.{preset_name} in "
                    f"~/.hermes/config.yaml."
                )
            }
        )

    if focus:
        ref_messages = [
            *ref_messages,
            {
                "role": "user",
                "content": (
                    f"FOCUS FOR THIS ADVISORY PASS: {focus}. Give your advice "
                    f"specifically on this planning question, grounded in the "
                    f"task state above."
                ),
            },
        ]

    advice = _run_max_advisor(slot, ref_messages)
    return json.dumps(
        {
            "advisor": f"{slot.get('provider')}:{slot.get('model')}",
            "reasoning_effort": slot.get("reasoning_effort") or "max",
            "focus": focus,
            "advice": advice,
        },
        ensure_ascii=False,
    )


# ── Plugin entry point ─────────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    """Register the planning_trigger tool + todo middleware (plugin entry)."""
    ctx.register_tool(
        name="planning_trigger",
        toolset="moa-trigger",
        schema={
            "name": "planning_trigger",
            "description": (
                "Force a fresh MAX-REASONING advisory pass over the current "
                "task state. Call this at PLANNING MOMENTS mid-task, "
                "specifically:\n"
                "1. After a test/command failure that contradicts the current "
                "approach — before re-planning the fix.\n"
                "2. Before large or irreversible changes: refactor, migration, "
                "rewrite, deletion, destructive operations.\n"
                "3. Before security-sensitive actions: deploys, credential/key "
                "handling, production mutations.\n"
                "4. When the plan must change mid-task based on new tool "
                "output (strategy pivot).\n"
                "5. Before delegating a subagent task — the delegation brief "
                "quality determines the whole subtree's output.\n"
                "6. Final review pass before declaring the task complete — "
                "verify the deliverable, not just the plan.\n"
                "Runs the MoA max-reasoning advisor on the live conversation "
                "and returns its advice — read it and apply it before "
                "continuing. (Note: todo-plan writes already trigger this "
                "automatically via middleware; use this tool for the moments "
                "above.)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": (
                            "The planning question to think hard about, e.g. "
                            "'should I refactor payment.py or patch the bug?'. "
                            "Optional — leave empty for a general state review."
                        ),
                    },
                    "preset": {
                        "type": "string",
                        "description": (
                            "MoA preset whose reference slot supplies the "
                            "max-reasoning advisor. Default: max-think-def-output."
                        ),
                    },
                },
                "required": [],
            },
        },
        handler=_handle_planning_trigger,
    )

    ctx.register_middleware(
        "tool_execution",
        _todo_planning_middleware,
    )

    # /moa-flush — drop the stale built-in reference and force a fresh
    # max-reasoning pass against the current live state. Handler signature
    # is fn(raw_args: str) -> str | None (plugin slash-command contract).
    try:
        ctx.register_command(
            name="moa-flush",
            handler=_handle_moa_flush,
            description=(
                "Flush the stale MoA reference cache and re-run the "
                "max-reasoning advisor against the current live state. "
                "Use when the built-in reference advice feels stale (it "
                "runs once per user turn and misses mid-loop tool results). "
                "Optional argument: a focus for the fresh advisory pass."
            ),
            args_hint="[focus]",
        )
        _ensure_facade_tracking()
        logger.info("hermes-moa-trigger: registered /moa-flush command")
    except Exception as exc:
        logger.warning("hermes-moa-trigger: /moa-flush registration failed: %s", exc)

    logger.info(
        "hermes-moa-trigger: registered planning_trigger tool + todo middleware"
    )
    return {"registered": ["planning_trigger", "tool_execution_middleware", "moa-flush"]}
