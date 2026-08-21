---
name: hermes-anchored
description: Anchored Standard — narrow the first model request to a minimal tool catalog to anchor the reasoning trajectory, then promote to a resident set. On-demand tool unlock via dev_tool_search. Opt-in.
---

# hermes-anchored — Anchored Standard

Ported from [dsh-anchored-standard](https://github.com/xiaobright/dsh-anchored-standard)
(MIT). The mechanism: DeepSeek V4 Pro conditions strongly on the API-visible tool
catalog — the first request's tool schema decides the model's whole reasoning
trajectory. Narrowing the first request to a minimal tool set anchors the trajectory;
promoting to a resident set after the first request keeps broader tooling available.

## How it works

- **Request #1** → the model sees only **`terminal` + `patch` + `dev_tool_search`**
  (the minimal anchor + the discovery tool, so turn-1 unlock works). This anchors
  the reasoning trajectory.
- **Request #2+** → the model sees the **FULL catalog** (all 93 tools). The
  post-promotion resident set is NOT narrowed: hiding the 15 other plugins' tools
  (searxng/agents/orchestra/lsp/vault/tdai/codegraph...) would contradict the
  maximize-utilization mandate. Only turn 1 is anchored.
- **Context gate** → NOT APPLICABLE in Hermes: the system prompt is ONE atomic
  system message (persona + skills + memory + AGENTS.md concatenated), so
  stripping injected context would remove the persona too. The context-gate
  middleware is a documented no-op; the tool-catalog anchoring (the decisive
  lever in dsh's own evaluation) is the effective mechanism.
- **`dev_tool_search`** → search the full catalog + unlock tools by name. In the
  bootstrap set on turn 1 (and resident thereafter), so discovery works from the
  first request.
- **Persistent enable** → `/anchored enable` persists to state.json, so it
  survives restart/reboot (the in-memory flag alone would reset every start).

## Enable / Disable

```bash
/anchored status      # show current state
/anchored disable     # turn off (persists across restart/reboot)
/anchored enable      # turn back on (persists)
```

**Enabled by default.** The anchoring changes the tool catalog per-request, which
breaks request-prefix cache at promotion — the user deliberately chose to enable
it by default (MOA is disabled by default instead). Set
`HERMES_ANCHORED_ENABLED=0` to opt out via env, or `/anchored disable`.

## Tools

| Tool | What it does |
|------|-------------|
| `dev_tool_search` | Search the full tool catalog + unlock tools by name |
| `anchored_status` | Show plugin state (enabled, request count, promotion, unlocked) |

## Configuration (env vars)

- `HERMES_ANCHORED_ENABLED` — default `1` (ON). Set `0` to opt out.
- `HERMES_ANCHORED_BOOTSTRAP_TOOLS` — comma-separated bootstrap tool names
  (default `terminal,patch`).

## Survival

- **hermes update** → lives in `~/.hermes/plugins/` outside the venv.
- **hermes restart** → config.yaml `plugins.enabled` + `allow_tool_override`.
- **system reboot** → durable state in `~/.hermes/anchored/state.json`.
- **auto-setup** → stdlib-only, zero deps, just copy the dir.
