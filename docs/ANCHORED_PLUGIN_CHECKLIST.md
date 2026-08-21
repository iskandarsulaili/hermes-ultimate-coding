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
- [x] VERIFY: middleware filters tools correctly, promotion works

## Batch 3 — Context gate
- [x] `pre_api_request` hook strips injected context on request #1
- [x] Re-opens after promotion
- [x] Degrades to keep-everything on failure (never eats context)
- [x] VERIFY: context stripped on first request, restored after promotion

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

---

## Design decisions
- **Opt-in by default** (like hermes-moa-trigger): the anchoring changes tool catalog
  per-request, which breaks request-prefix cache at promotion. Users enable it explicitly.
- **Bootstrap pair**: `bash` + `str_replace_editor` equivalents in Hermes = the two
  most fundamental tools. But Hermes tool names differ — use a configurable
  `bootstrap_tools` list (default: the 2 most-used core tools).
- **Resident set**: bootstrap pair + `dev_tool_search` + anything unlocked.
- **Promotion signal**: first tool call OR first assistant message (durable).
- **State**: JSON file, atomic write, RLock-guarded.
