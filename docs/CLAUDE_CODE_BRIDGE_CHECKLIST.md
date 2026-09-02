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
- [x] **5.2** Static defect sweep across all 17,340 LOC (pyflakes: undefined names,
      unbound locals, shadowing, dead assignments). 52 findings triaged; the only
      runtime-affecting one was 5.1. Remaining are cosmetic — redundant `global` in
      `hermes-tps:119` (read-only), a nested-scope `import time` in `hermes-graphify:678`
      (safe), 38 unused imports, 8 dead locals
- [~] **5.3** `_shared/deps.py` bootstrap failure modes — static pass only; degraded-mode
      paths not exercised under real install failures
- [~] **5.4** Concurrency — **NOT systematically audited.** One real issue found and fixed
      at the bridge layer: plugin-spawned children were orphaned on exit, and since their
      time budget is enforced by the parent (`communicate(timeout=...)`), orphans ran
      unbounded. Plugin-internal debounce timers and thread shutdown paths remain unreviewed
- [~] **5.5** Resource leaks — partial. `hermes-graphify` subprocess timeout handling
      verified correct. Sockets and file handles across the other 15 plugins unreviewed

## Phase 6 — Ship

- [x] **6.1** README — document Claude Code support
- [!] **6.2** Count discrepancy **unresolved**: README documents 16 plugins and its install
      step copies `plugins/hermes-dsh`, but that plugin was never committed and is not
      gitignored — a clone cannot follow the README. Blocked pending owner approval to
      publish it (scanned clean: no secrets, no internal hosts, localhost only)
- [ ] **6.3** Commit
- [ ] **6.4** Push


---

## Known gaps (honest status)

- **`hermes-dsh` is missing from the repo.** Documented in the README, present in the
  local install, never committed. Adding it needs owner sign-off because this repo is
  public. Until then the README's install instructions are broken for fresh clones.
- **Phase 5 is a static audit, not a deep review.** Logic errors, race conditions and
  API misuse across the 15 plugins would not be caught by what was run.
- **4.5 (live test inside Claude Code) is unverified by me.** The bridge is proven
  against a real subprocess speaking real MCP, but I cannot restart this session's own
  MCP client to load it.
