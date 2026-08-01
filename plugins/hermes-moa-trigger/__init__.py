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
    """Run ONE max-reasoning advisory pass through the same call_llm path MoA uses."""
    try:
        from agent.auxiliary_client import call_llm

        from hermes_cli.runtime_provider import resolve_runtime_provider

        provider = str(slot.get("provider") or "")
        model = str(slot.get("model") or "")
        rt = resolve_runtime_provider(requested=provider, target_model=model)
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
        resp = call_llm(
            provider=rt.get("provider") or provider,
            model=rt.get("model") or model,
            base_url=rt.get("base_url"),
            api_key=rt.get("api_key"),
            api_mode=rt.get("api_mode"),
            messages=messages,
            reasoning_config=reasoning,
            timeout=300,
        )
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

    session_id = str(context.get("session_id") or "")

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
    logger.info(
        "hermes-moa-trigger: registered planning_trigger tool + todo middleware"
    )
    return {"registered": ["planning_trigger", "tool_execution_middleware"]}
