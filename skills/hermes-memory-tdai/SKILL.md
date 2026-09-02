---
name: hermes-memory-tdai
version: "1.0.0"
description: "Four-layer agent memory for Hermes via TencentDB Agent Memory — L0 conversations, L1 atoms, L2 scenarios, L3 persona."
---

# hermes-memory-tdai — Four-Layer Agent Memory

## Availability in Claude Code

All nine `tdai_*` tools are bridged and work normally. Note that Claude Code has
its own file-based memory under `~/.claude/.../memory/`; the two are independent
stores. Use `tdai_*` when you want the four-layer TencentDB memory shared with
Hermes, and Claude Code's own memory for Claude-Code-local facts.


TencentDB Agent Memory gives Hermes a **persistent, layered memory system**
that goes beyond the built-in MEMORY.md/USER.md files:

| Layer | What it stores | Tools |
|-------|---------------|-------|
| **L0** | Raw conversation history | `tdai_capture`, `tdai_conversations` |
| **L1** | Structured atoms (facts, episodic, instructions) | `tdai_search` |
| **L2** | Scenario blocks (Markdown scene files) | `tdai_scenarios`, `tdai_read_scenario` |
| **L3** | Persona / user profile synthesis | `tdai_core`, `tdai_write_core` |

## When to use

- **Remember past context** → `tdai_recall(query)` — searches L1 + L0 in one call
- **Record a decision** → `tdai_capture(messages=[{role, content}], session_id=...)`
- **Find a specific fact** → `tdai_search(query, type="persona|episodic|instruction")`
- **Find verbatim past conversation** → `tdai_conversations(query)`
- **Review scenario context** → `tdai_scenarios` + `tdai_read_scenario(path)`
- **Check the persona** → `tdai_core` / `tdai_write_core(content)`

## How it works

1. **First use**: the plugin auto-clones
   `https://github.com/TencentCloud/TencentDB-Agent-Memory.git` to
   `~/.hermes/tdai/tencentdb-agent-memory/`, runs `npm install` in
   `MemoryCore/`, and starts the gateway sidecar
   (`node --import tsx src/gateway/server.ts`) on `127.0.0.1:8420`.
2. **Every call**: the Python plugin is a thin HTTP client to the gateway's
   v3 API (`/v3/conversation/*`, `/v3/atomic/*`, `/v3/scenario/*`, `/v3/core/*`).
3. **L1-L3 extraction** (LLM distillation of conversations into atoms/scenarios/
   persona) runs gateway-side; it needs LLM credentials via env:
   `TDAI_LLM_API_KEY`, `TDAI_LLM_BASE_URL`, `TDAI_LLM_MODEL`.
   Without them, L0 capture/search still work.

## Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TDAI_GATEWAY_HOST` | `127.0.0.1` | Gateway host |
| `TDAI_GATEWAY_PORT` | `8420` | Gateway port |
| `TDAI_GATEWAY_API_KEY` | — | Optional gateway auth |
| `TDAI_REPO_DIR` | `~/.hermes/tdai/tencentdb-agent-memory` | Repo clone location |
| `TDAI_DATA_DIR` | `~/.hermes/tdai/data` | L0-L3 storage |
| `TDAI_LLM_API_KEY` | — | LLM key for L1-L3 extraction |
| `TDAI_LLM_BASE_URL` | — | LLM base URL |
| `TDAI_LLM_MODEL` | — | LLM model |
| `TDAI_TIMEOUT` | `15` | HTTP timeout (s) |

## Slash command

`/memory-tdai status` — gateway status
`/memory-tdai recall <query>` — recall all layers
`/memory-tdai capture <json>` — capture messages
`/memory-tdai search <query>` — L1 search
`/memory-tdai conversations <query>` — L0 search
`/memory-tdai scenarios` — list scenarios
`/memory-tdai read-scenario <path>` — read scenario
`/memory-tdai core` — read persona
`/memory-tdai write-core <content>` — write persona
