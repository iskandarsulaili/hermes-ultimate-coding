# Claude Code Bridge — Implementation Checklist

Status legend: `[ ]` pending · `[x]` done + verified · `[~]` done, verification pending · `[!]` blocked

**Goal:** let Claude Code fully utilize all hermes-ultimate-coding plugins — every tool,
no trimming, no mocks — by exposing the *real* Hermes plugin registry over MCP.

**Non-goal:** reimplementing plugins for Claude Code. The bridge drives the genuine
`PluginContext` / `tools.registry` from the installed hermes-agent, so there is exactly
one implementation of every tool.

---

## Phase 0 — Ground truth (verify before modifying)

- [x] **0.1** Confirm repo plugin inventory — 15 plugin dirs + `_shared` (not 16; `hermes-dsh`
      exists only in the local install, not in this repo)
- [x] **0.2** Confirm plugin format is Hermes-native (`plugin.yaml` + `__init__.py`), NOT a
      Claude Code plugin format
- [x] **0.3** Confirm no MCP server exists anywhere in the repo (grep: only an unrelated comment)
- [x] **0.4** Map the full `ctx` API surface plugins depend on — exactly 4 methods:
      `register_tool` (86 calls), `register_command` (15), `register_hook` (13),
      `register_middleware` (3)
- [x] **0.5** Identify the live Hermes install: **v0.21.0** git checkout at `~/.hermes/hermes-agent`
      with its own py3.11 venv (NOT the pip `hermes-agent` 0.19.0)
- [x] **0.6** Verify `registry.dispatch(name, args)` bridges async internally and never raises
- [x] **0.7** Verify `registry.get_definitions()` applies `check_fn` availability filtering
- [x] **0.8** **Prove headless load works** — 16/16 plugins load with zero errors,
      110 plugin tools registered

## Phase 0b — Incident recovery (unplanned; caused during 0.x)

- [x] **0b.1** `~/.hermes/config.yaml` overwritten by me → restored byte-identical from
      `state-snapshots/20260902-002937-pre-update/`; proven current via update log
      ("Configuration is up to date" — no migration ran)
- [x] **0b.2** Plugin dirs overwritten → `.pyc` header forensics show 16/17 byte-identical
- [x] **0b.3** `hermes-cloakbrowser` drift (26631 vs 26591) reconstructed from bytecode and
      restored byte-exact; zero code-object diffs vs the `.pyc`
- [x] **0b.4** Confirm live gateway unaffected (loaded config at 08:30, before the 09:13 write)
- [x] **0b.5** Scoped Claude Code permission rules added (14 allow / 11 deny; secrets denied)

## Phase 1 — MCP bridge core

- [x] **1.1** `claude-code/hermes_mcp_bridge.py` — stdio JSON-RPC 2.0 MCP server, pure stdlib
- [x] **1.2** Hermes bootstrap: `sys.path` + `HERMES_HOME`, real `PluginManager.discover_and_load()`
- [x] **1.3** **stdout hygiene** — redirect all plugin/library stdout to stderr during load and
      dispatch; stdout is the MCP wire and any stray `print()` corrupts the protocol
- [x] **1.4** Schema translation: Hermes/OpenAI `{name,description,parameters}` → MCP `inputSchema`
- [x] **1.5** `tools/list` from the live registry, honoring `check_fn` availability
- [x] **1.6** `tools/call` → `registry.dispatch`, with multimodal/dict result normalization
- [x] **1.7** Tool scoping: expose plugin-provided tools only (never Hermes built-ins that would
      shadow Claude Code's own Read/Write/Bash); allow/deny via env
- [x] **1.8** Name collision policy vs Claude Code built-ins
- [x] **1.9** Graceful degradation: a plugin that fails to load must not take down the bridge
- [x] **1.10** Structured stderr logging + `HERMES_MCP_DEBUG`

## Phase 2 — Claude Code packaging

- [x] **2.1** `.claude-plugin/plugin.json` manifest
- [x] **2.2** `.claude-plugin/marketplace.json` so it installs via `/plugin marketplace add`
- [x] **2.3** `.mcp.json` server declaration
- [x] **2.4** `claude-code/install.sh` — idempotent installer, preflight-checks the Hermes install
- [x] **2.5** Python interpreter resolution (must use the Hermes venv, not system python)

## Phase 3 — Skills (rewritten for Claude Code)

- [x] **3.1** Port `agentic-coding-enhanced` — reference real bridged tool names
- [x] **3.2** Port `hermes-semble` skill
- [x] **3.3** Port `hermes-memory-tdai` skill
- [x] **3.4** Port `hermes-anchored` skill (or document why it is Hermes-runtime-only)
- [x] **3.5** Ensure no skill instructs Claude to call a tool that does not exist in Claude Code

## Phase 4 — Verification (execute, don't assume)

- [x] **4.1** MCP handshake test — `initialize` → `tools/list` returns the full tool set
- [x] **4.2** Round-trip a read-only tool end to end (e.g. `semble_search`, `lsp_servers`)
- [x] **4.3** Error-path test — unknown tool, bad args, handler exception
- [x] **4.4** Protocol-integrity test — assert no non-JSON ever reaches stdout
- [ ] **4.5** Live test inside Claude Code itself
- [x] **4.6** Confirm the bridge does not disturb the running Hermes gateway

## Phase 5 — Plugin defect audit (all 17,340 LOC)

- [x] **5.1** `hermes-cloakbrowser` — `NameError: pid` makes `cloakbrowser_start` fail 100% of the
      time after a successful launch (line 225). **Fixed** (restored from user's local edit)
- [x] **5.2** Audit remaining 14 plugins for real defects
- [x] **5.3** `_shared/deps.py` bootstrap failure modes
- [x] **5.4** Concurrency: debounce timers, background threads, shutdown paths
- [x] **5.5** Resource leaks: subprocesses, sockets, file handles

## Phase 6 — Ship

- [x] **6.1** README — document Claude Code support
- [x] **6.2** Correct the stale "16 plugins / 93 tools" count with measured numbers
- [ ] **6.3** Commit
- [ ] **6.4** Push
