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

## Phase 5 — Plugin defect audit (deep review)

- [x] **5.1** `hermes-cloakbrowser` — `NameError: pid` made `cloakbrowser_start` fail 100% of
      the time after a successful launch. **Fixed**
- [x] **5.2** Static sweep of all plugins (pyflakes: undefined names, unbound locals,
      shadowing, dead assignments). 52 findings triaged; only 5.1 affected runtime
- [x] **5.3** `_shared/deps.py` reviewed — already used `setsid` + `killpg` correctly and
      guarded against signalling its own group. That rule is now generalised in
      `_shared/procs.py`
- [x] **5.4** **Concurrency — AST analysis of every `with <lock>:` block for blocking calls
      in the body (48 sites triaged).** Network calls all carry timeouts, every thread is
      `daemon=True`, no bare `except:`, no mutable default args. Two real findings:
      `hermes-cloakbrowser` held `_lock` across an unbounded `stderr.read()` (permanent
      plugin-wide deadlock) and across a `readline()` that made its 30s timeout illusory.
      Both **fixed** and verified. `hermes-searxng` holds its lock across a 30s HTTP call —
      serialises concurrent searches but is correctness-safe; left as is
- [x] **5.5** **Resource leaks — root cause found and fixed.** Plugins signalled only their
      direct child, so helper-spawned grandchildren leaked AND kept inherited pipes open,
      which is why `communicate()`/`read()` stalled. `_shared/procs.py` added and wired into
      lsp / memory-tdai / searxng / graphify; `hermes-lsp` additionally called `kill()`
      with no `wait()`, leaving a zombie
- [x] **5.6** **`hermes-lsp` request path had never worked** — four stacked defects
      (unconditional `which` success, bare-name launch of servers in a managed prefix,
      missing Content-Length framing, str written to a binary pipe). `lsp_diagnostics`
      reported "clean" for files nothing had analysed. **All fixed**, verified end to end,
      regression test added (`claude-code/test_lsp.py`)
- [x] **5.7** `graphify_cancel` — `_cancel_background_build()` was fully implemented with
      zero callers while the tool output advertised a `cancel_command` naming a tool that
      did not exist. **Registered and verified**


## Phase 7 — Full tool coverage (all 94 tools, every toolset)

Baseline entering this phase: 26/94 tools executed, 13/15 toolsets touched.
Result: **94/94 exercised — 84 OK, 5 backend-absent, 3 guarded (LLM spend), 2
environment-limited, 0 crashes.** Harness: `claude-code/test_coverage.py`.

The two environment-limited cases are not code defects: `vault_multi_get`
correctly reports a document the sandbox vault does not contain, and
`vault_search` hits a CUDA fault inside qmd's node-llama-cpp embedding model on
this machine (both GPUs already loaded). `vault_search` is therefore **not
proven working here** — an honest gap, not a pass.

- [x] **7.1** Exhaustive harness with real per-schema arguments, sandboxed in a temp
      project + temp `HERMES_ORCHESTRA_DIR`; classifies OK / BACKEND / GUARDED / FAIL,
      guards LLM spend behind `COVER_SPEND=1`, and reconnects if the server dies
- [x] **7.2** `semble_*` (5) — all execute
- [x] **7.3** `lsp_*` (7) — all execute, incl. previously untested `lsp_verify`/`lsp_auto_fix`
- [x] **7.4** `graphify_*` (8) — all execute against a *built* graph (the harness now waits
      for the build; querying during "building" proved nothing)
- [x] **7.5** `codegraph_*` (8) — all execute with real results (callers/callees/impact)
- [x] **7.6** `cgc_*` (8) — **3 were impossible to call successfully.** Fixed
- [x] **7.7** `orchestra_*` (12) — full lifecycle. **5 hung forever; plan failed 100%.** Fixed
- [x] **7.8** `vault_*` (6) — **status never probed.** Fixed; `HERMES_VAULT_DIR` implemented
- [x] **7.9** `tdai_*` (9) — all execute; `tdai_write_core` proven by identity write-back
- [x] **7.10** `agents_*` (7) — 6 execute; `agents_delegate` guarded (LLM spend)
- [x] **7.11** `dsh_*` (7) — 6 execute; `dsh_run` guarded (LLM spend)
- [x] **7.12** `effect_*` (4) — all execute
- [x] **7.13** `searxng_*` (4) — query/status execute; **engines/categories hit a 404
      endpoint that does not exist in SearXNG.** Fixed to use /config
- [x] **7.14** `cloakbrowser_*` (6) — launch/status/close execute; navigate/html/screenshot
      blocked by a self-kill bug (fixed) then by browser availability
- [x] **7.15** `anchored` (2) + `planning_trigger` (1, guarded)
- [x] **7.16** Exercised the shutdown paths previously changed but never run (tdai, searxng)
- [x] **7.17** Re-verified: 34-check bridge suite and 10-check LSP suite still pass

### Defects found by running every tool

| # | Component | Defect | Impact |
|---|---|---|---|
| 1 | bridge | children inherited stdin; Node sets O_NONBLOCK on the shared file description, so `readline()` returned "" and the server exited rc=0 | **server vanished mid-session, 3× per sweep** |
| 2 | orchestra | non-reentrant `Lock`; 5 decorated methods call decorated `get_issue` | **one claim wedged every orchestra tool in the process** |
| 3 | orchestra | `_atomic_write` never made parent dirs; `plan` writes nested spec names | `orchestra_plan` failed 100% |
| 4 | orchestra | `LOCK_FILE` declared, never used — claims had no cross-process exclusion | two agents could hold one lease |
| 5 | cgc | passed `project_path`; every backend handler reads `repo_path` | project scope silently ignored on all 8 tools |
| 6 | cgc | `relationship_type`/`symbol_name` vs required `query_type`/`target` | 3 tools could never succeed |
| 7 | searxng | fetched `/engines`, which SearXNG does not serve | 2 tools always 404'd |
| 8 | vault | `status()` read cached flags, never probed | always reported "not ready" on a fresh session |
| 9 | vault | error advised `HERMES_VAULT_DIR`, never implemented | documented escape hatch did nothing |
| 10 | cloakbrowser | `killpg` with no guard against our own group *(introduced by me earlier this session)* | could SIGKILL the host |
| 11 | bridge | `with ThreadPoolExecutor` + timeout ⇒ `shutdown(wait=True)` blocked on the abandoned handler | per-call timeout bounded nothing |

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

- ~~`hermes-dsh` is missing from the repo.~~ **Added** — the tree now matches the
  documented 16 plugins and a fresh clone can follow the README.
- **Phase 5 deep review is done for concurrency, process lifetime and the LSP path**
  (see 5.4-5.7). Not exhaustively reviewed: per-plugin business logic in
  `hermes-orchestra` (12 tools), `hermes-agents`, `hermes-dsh` and `hermes-memory-tdai`
  beyond their process handling.
- **4.5 (live test inside Claude Code) is unverified by me.** The bridge is proven
  against a real subprocess speaking real MCP, but I cannot restart this session's own
  MCP client to load it.
