# hermes-anchored — Anchored Standard Plugin Checklist

**Goal:** Port the dsh-anchored-standard "Anchored Standard" mechanism to Hermes as a
self-contained plugin that survives hermes update, hermes restart, and system reboot,
following the exact pattern of the other 15 plugins. Zero mock/stub/placeholder/dormant.

**Source:** https://github.com/xiaobright/dsh-anchored-standard (MIT) — the `preset/`
mode (Anchored Standard): first request sees a minimal tool catalog (anchors the
model's reasoning trajectory), then promotes to a resident set after the first durable
tool call / assistant message.

**Mechanism ported (verified against Hermes source):**
- `llm_request` middleware rewrites `api_kwargs["tools"]` + `api_kwargs["max_tokens"]`
  per request (conversation_loop.py:2945-2960, build_api_kwargs passes tools).
- `pre_api_request` hook strips injected context sections on request #1.
- Durable promotion state in `~/.hermes/anchored/state.json` (survives restart/reboot).
- `dev_tool_search` tool for on-demand unlock of heavier tools.

---

## Batch 1 — Plugin skeleton
- [x] `plugin.yaml` (entry: "__init__.py", name, version, description)
- [x] `__init__.py` structure: stdlib-only, RLock, try/except on every handler
- [x] `register(ctx)` registers middleware + hooks + tool
- [x] `status()` no spurious "error" key
- [x] `_env_int`/`_to_int` garbage-safe helpers
- [x] VERIFY: parses, loads, registers, no tool-name collision

## Batch 2 — Tool catalog bootstrap (Anchored Standard core)
- [x] `llm_request` middleware: request #1 → bootstrap tool pair
- [x] After promotion → resident set (bootstrap pair + discovery tools + unlocked)
- [x] `promoteOn: either` (tool/call OR assistant/message)
- [x] Missing bootstrap tool degrades to full catalog (never bricks session)
- [x] AUDIT FIX: default bootstrap tools were dsh names (bash, str_replace_editor)
      that DON'T EXIST in Hermes → would filter to EMPTY tool list. Fixed to real
      Hermes tools: `terminal` + `patch`.
- [x] VERIFY: middleware filters tools correctly, promotion works

## Batch 3 — Context gate
- [x] Audit: Hermes' system prompt is ONE atomic system message (persona + skills +
      memory + AGENTS.md concatenated — agent/system_prompt.py volatile_parts)
- [x] DECISION: context stripping is NOT APPLICABLE — stripping injected sections
      would remove the persona too (serious regression). Gate is a documented no-op.
- [x] The tool-catalog anchoring (the decisive lever in dsh's own evaluation) is
      the effective mechanism that transfers cleanly.
- [x] VERIFY: context gate returns None always, never modifies the request

## Batch 4 — dev_tool_search + durable state
- [x] `dev_tool_search` tool (search catalog + unlock by name)
- [x] Unlocked names persisted to state.json (resume-safe)
- [x] Promotion state persisted to `~/.hermes/anchored/state.json`
- [x] VERIFY: unlock persists across restart, promotion survives reboot

## Batch 5 — Integration
- [x] TPS prefix entry (`anchored_` → toolset)
- [x] AGENTS.md / SOUL.md updated (16 plugins / 93 tools)
- [x] `hermes plugins enable hermes-anchored --allow-tool-override`
- [x] VERIFY: enabled, granted, tools register

## Batch 6 — Full verification
- [x] E2E: middleware filters tools, promotion works, unlock persists
- [x] Survival: hermes update (outside venv), restart (config), reboot (on-disk)
- [x] Auto-setup: stdlib-only, zero deps, copy dir
- [x] Zero dormant: no stub/mock/todo/fixme/pass
- [x] VERIFY: all 16 plugins load, workspace clean, committed + pushed

## Batch 7 — Adversarial sweep fixes (found by "any more blindspot?" audit)
- [x] FIX 1: bootstrap tool names — dsh `bash`/`str_replace_editor` DON'T exist in
      Hermes → request #1 would filter to EMPTY tools. Corrected to `terminal`/`patch`.
- [x] FIX 2: context gate was actively harmful — Hermes' system prompt is atomic, so
      stripping would remove the persona. Demoted to documented no-op.
- [x] FIX 3: `/anchored enable` was memory-only → reset every restart/reboot. Now
      persisted in state.json (`{enabled, sessions}` shape) + restored on register.
- [x] VERIFY: persistence survives simulated restart, register restores enabled,
      all handlers try/except-wrapped, mechanism intact.

## Batch 8 — Second adversarial sweep (dead code + error-key + retry audit)
- [x] FIX 4: dead `_tool_names()` helper (defined, never called) removed.
- [x] FIX 5: tool exception paths returned `{"error": ...}` → would trip Hermes'
      `_detect_tool_failure` (false `[error]` tag). Now return `{"text": ...}`.
- [x] VERIFY: middleware runs ONCE per request (outside the retry loop at
      conversation_loop.py:2947 vs _perform_api_call:3113) — no request_count
      inflation from retries, promotion stays correct.
- [x] VERIFY: 15/15 checks pass, no dead code, no spurious error keys.

## Batch 9 — Enabled-by-default critical audit (resident-set blindspot)
- [x] CRITICAL FIX: post-promotion resident set was bootstrap+discovery+unlocked,
      which DROPPED all 15 other plugins' tools (searxng/agents/orchestra/lsp/
      vault/tdai/codegraph...) when anchoring became default-ON. That hid the
      whole plugin ecosystem and contradicted the maximize-utilization mandate.
- [x] FIX: turn-1 anchor = [terminal, patch, dev_tool_search] (trajectory benefit
      + discoverable unlock); turn-2+ = FULL catalog (no filtering). (50bd2b9)
- [x] VERIFY: turn1 [terminal, patch, dev_tool_search], turn2 None (full catalog),
      default ON, 15/15 checks pass.

## Batch 10 — Bounded-state audit (unbounded memory/disk leak)
- [x] FIX: _session_state was only pruned on on_session_end, which does NOT fire
      for crash-killed / subprocess-worker / long-lived gateway sessions → the
      in-memory dict + persisted state.json grew without bound.
- [x] Added _prune_state: TTL eviction (30d default) + LRU cap (4096 sessions),
      run on session-create + save. last_seen refreshed on access so live
      sessions are never evicted.
- [x] VERIFY: TTL evicts stale + preserves live; cap bounds size; defaults
      configurable (HERMES_ANCHORED_MAX_SESSIONS / SESSION_TTL).

## Batch 11 — Unlock-mechanism dead-code audit
- [x] FIX: `unlocked` list written by dev_tool_search was NEVER read after the
      resident-set fix (turn-2+ returns full catalog regardless) → dead state,
      and the tool's description LIED ("everything else is unlocked on demand")
      causing the model to waste turns unlocking already-available tools.
- [x] dev_tool_search now a pure catalog SEARCH tool (no `toolNames`/unlock);
      removed dead `unlocked` field from state creation + status output.
- [x] VERIFY: no `unlocked`/`toolNames` refs, search path clean, mechanism intact.

---

## Design decisions
- **Enabled by default** (user directive 2026-08-21): anchoring is ON out of the
  box; `HERMES_ANCHORED_ENABLED=0` or `/anchored disable` opts out. The per-request
  tool-catalog change breaks request-prefix cache at promotion, but the user
  preferred the anchoring benefit globally (MOA is the disabled-by-default one).
- **Bootstrap pair**: `terminal` + `patch` (the two most fundamental Hermes tools,
  the analogs of dsh's bash + str_replace_editor). Configurable via
  `HERMES_ANCHORED_BOOTSTRAP_TOOLS`.
- **Resident set**: FULL catalog after turn 1 (bootstrap+discovery on turn 1 only).
  NOT narrowed — hiding the other 15 plugins' tools would break the
  maximize-utilization mandate (CRITICAL fix in 50bd2b9).
- **Promotion signal**: first tool call OR first assistant message (durable).
- **State**: JSON file, atomic write, RLock-guarded.
- **MOA**: disabled by default (config `plugins.moa_trigger.enabled: false`).
