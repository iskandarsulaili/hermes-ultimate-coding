# hermes-rea — Reverse-Engineering Plugin Checklist

**Goal:** give Hermes evidence-backed reverse engineering (native binaries, Android
APK/device, JavaScript/Electron, artifacts, process behaviour, firmware, captures) by
wrapping REA — implemented, wired, verified, benchmarked. Zero mock/stub/placeholder/dormant;
nothing removed; the plugin survives `hermes update`, `hermes restart` and reboot.

**Source:** https://github.com/morluto/rea (MIT) — npm package `rea-agents` **6.4.0**,
main tree **153 CLI commands / 207 MCP tools** vs the release's **127 / ~170**.
REA ships a CLI, an MCP stdio server, engine bridges (`bridge/`) and an agent skill.

**Why wrapping beats reimplementing:** REA already supports Hermes as a first-class client
(`src/application/SupportedClients.ts:648`) — it resolves `HERMES_HOME`, `HERMES_DATA_DIR_SUFFIX`
and the sticky `active_profile`, then writes `mcp_servers.<name>` in `~/.hermes/config.yaml`
plus a skill into `~/.hermes/skills`. `rea doctor` even reports `registration:hermes` as
missing on a machine where it has not been run.

---

## Batch 1 — Understand REA against the real artifact (not the README)
- [x] Cloned the repo, read `package.json` / `server.json` / docs / `.agents/skills`.
- [x] Ran it: node 26.7.0 (REA needs 22.19/24.11/26+), `rea --version` → 6.4.0.
- [x] MCP handshake over stdio: `serverInfo {name: rea, version: 6.4.0}`, `tools/list` → **169**
      on the release, **207** on a built main tree, `prompts/list` → 6.
- [x] `rea doctor --json`: node/host healthy; hopper/ghidra/ida missing; skill + registrations missing.
- [x] Established the authoritative argument source = REA's own manifests
      (`rea --llms`, `rea --llms-full`), not documentation prose.
- [x] VERIFY: `rea setup --client hermes --dry-run --json` plans exactly 2 actions
      (config entry pinned to `rea-agents@6.4.0` + backup; skill install to `~/.hermes/skills`).

## Batch 2 — Engines (all free; paid/closed engines stay caller-supplied)
| Engine | Purpose | State here |
|---|---|---|
| adb 37.0.1 | real-device verification | pre-existing (`~/Android/Sdk/platform-tools`) |
| objdump / readelf | offline ELF | pre-existing |
| Ghidra **12.1.4** | native decompilation | installed → `/home/lot399/tools/ghidra_12.1.4_PUBLIC` |
| JADX **headless MCP jar 0.7.1** | APK method decompilation | installed + sha256 verified → `~/tools/rea-providers/` |
| Rizin **0.9.1** | offline native provider | installed (static) → `~/.local/bin/rizin` |
| apktool | APK manifest/strings/locales | apt |
| tshark 4.6.4 + capinfos | pcap + packet captures | apt |
| binwalk + unblob | firmware regions/extract | apt + pipx |
| sigrok-cli | logic recordings | apt |
| WABT | WASM artifacts | apt |
| pwntools / mitmproxy / frida-tools | binary diagnostics, native capture, live instrumentation | pipx (`pwn`, `mitmdump`, `frida`) |
| gdb | crash debugger context | apt |
| wakaru | JS source recovery | npm -g |
| .NET SDK + ilspycmd | managed decompilation | dotnet-install + tool |
| Hopper / IDA / JEB | deep native / JEB projects | **not installed** — paid or closed; report, never fake |
- [x] `tools/rea-install-engines.sh` (install + `--check`) — idempotent, one status line per engine,
      sha256-pinned JADX jar, no `--strip-components` on the Rizin archive root.
- [x] Engine env in ONE file (`<hermes state>/rea/engine_env.json`): `GHIDRA_INSTALL_DIR`,
      `JAVA_HOME`, `REA_JADX_MCP_JAR`, `PATH_EXTRA` — injected into every REA subprocess and
      into the MCP wrapper script.
- [x] F2/F3 (below) fixed in the bootstrap script after live probes proved them.

## Batch 3 — Plugin implementation
- [x] `plugins/hermes-rea/{plugin.yaml,__init__.py}` — stdlib only, RLock-guarded caches,
      `_to_int`-style garbage-safe coercion, every handler try/except, atomic state writes,
      bounded subprocess calls with process-group kill on timeout, response spill-to-disk cap.
- [x] Launcher resolution: `HERMES_REA_CMD` → built main tree (`scripts/rea.mjs`) → global `rea`
      → `npx -y rea-agents@<pin>`; the first candidate that answers `--version` wins and is logged.
- [x] Command manifest parsed from `rea --llms`/`--llms-full` into
      `<state>/rea/command_manifest.json`, TTL-cached, regenerable — so argument shapes are
      never hardcoded and never drift.
- [x] **54 tools** registered on toolset `rea` (prefix `rea_`): readiness/discovery
      (`rea_status`, `rea_engines`, `rea_commands`, `rea_run`, `rea_doctor`, `rea_providers`,
      `rea_capabilities`), Android (`rea_adb_devices`, `rea_adb_inspect`, `rea_adb_logcat`,
      `rea_adb_screen`, `rea_adb_bugreport`, `rea_adb_app`, `rea_adb_transfer`, plus the
      single-command set: packages/processes/services/features/directory/setting),
      APK (`rea_apk`), JavaScript (`rea_js_analyze`, `rea_js_compare`, `rea_js_recover`),
      artifacts/native (`rea_artifact`, `rea_compare`, `rea_crash`, `rea_native`,
      `rea_binary_meta`, `rea_sqlite`, `rea_wasm`, `rea_evm`, `rea_objdump`, `rea_rizin`,
      `rea_dart_flutter`, `rea_dart_aot`), captures (`rea_pcap`, `rea_packets`, `rea_signals`,
      `rea_frida_devices`, `rea_frida_instrument`, `rea_gdb`), web (`rea_web`),
      process (`rea_process_capture`, `rea_process_compare`), evidence (`rea_evidence`),
      composites (`rea_mobile_audit`, `rea_verify_deploy`), MCP (`rea_mcp_setup`, `rea_mcp_status`).
- [x] Evidence persistence: every envelope (`evidence_id`) stored under
      `<state>/rea/evidence/` with retention pruning (`HERMES_REA_EVIDENCE_RETENTION_DAYS`, 30d)
      and `rea_evidence action=list|get|export|import|inspect|prune|unknowns`.
- [x] Readiness with exact remediation for every missing engine (never a bare failure).
- [x] 5 slash commands: `/rea`, `/rea-doctor`, `/rea-engines`, `/rea-setup`, `/rea-commands`.
- [x] `hermes-tps` prefix/emoji/label dicts updated (`rea_` → REA 🧩) so tool calls surface in the TUI.
- [x] JIT dependency via `_shared.deps` (npm global), probing `rea --version` (never `require()`).

## Batch 4 — Hermes MCP surface (the "full feature" path)
- [x] `rea_mcp_setup` writes `<state>/rea/rea-mcp.sh` — sources the engine env then execs the
      resolved launcher with `mcp` — because Hermes passes stdio MCP servers only a FILTERED
      environment (PATH/HOME/USER/LANG/…), so `GHIDRA_INSTALL_DIR`/`REA_JADX_MCP_JAR` would
      otherwise never reach the server.
- [x] Registration via the official CLI (`rea setup --client hermes`, then
      `hermes mcp add rea --command <wrapper>`), both with backups; `rea_mcp_status` reports the
      entry, the wrapper and whether the main-branch surface is present.
- [x] Full surface is discoverable but budget-safe: Hermes' deferred catalog is token-budgeted
      (`tools/tool_search_catalog.py`, 4000-token listing, per-server degradation largest-first),
      so a 200-tool server collapses to a summary line and is still reachable via tool search.

## Batch 5 — Verification (live, on this host, not assumed)
| Check | Evidence |
|---|---|
| Plugin import + register | 1.6s import, 54 tools + 5 commands, 0 failures across 19 smoke assertions |
| Android device discovery | `rea_adb_devices` → serial `XC8LJR4TQKN76PZ9`, model CPH2321, usb/physical |
| Display inspection | 720x1600 @ 320dpi |
| Screen capture (was failing, see F1) | `screen.png` 123,586 bytes + sha256, digest-backed evidence |
| Composite device audit | `rea_mobile_audit` → 4 evidence ids, 0 failed steps |
| Artifact inspection | zip → inspection with observations/hypotheses/contradictions, evidence id stored |
| SQLite (main-branch command) | `rea_sqlite` on a real 1.7MB dev.db → schema inventory, exit 0 |
| Manifest-driven executor | `rea_run` rejects unknown commands; executes main-branch-only commands |
| Garbage input hardening | non-numeric `count`, relative path, selectorless decompile → structured errors, no tracebacks |
| Evidence store | envelopes persisted + listed, retention prune reachable |
| Engines | **21/21 ready** via `tools/rea-install-engines.sh --check` (Ghidra, JADX-MCP jar, Rizin, apktool, tshark+capinfos, binwalk, unblob, sigrok, WABT, pwntools, mitmproxy, frida, gdb, wakaru, .NET+ilspycmd, adb, objdump, java, node) |
| Native analysis (Ghidra) | `overview` **22s** → 34 segments / 134 procedures / 132 strings, evidence stored; `function` dossier **21s** with pseudocode + callers/callees |
| MCP registration | `hermes mcp list` → `rea` enabled; the entry carries command/args/env/lazy/connect_timeout/timeout; `hermes config check` exit 0; `hermes mcp test rea` discovered the full surface during setup |
| Plugin visibility in Hermes | `hermes plugins list` → hermes-rea enabled (18th); `hermes tools list` → plugin toolset `rea` enabled for cli |
| Inventory regeneration | `hermes-plugin-sync.py` → **18 plugins / 157 tools**, hermes-rea present in `~/.hermes/SOUL.md`, `AGENTS.md`, `memories/MEMORY.md` |
| Config safety | `_tune_mcp_entry()` first run `changed:true, verified:true`; second run `changed:false` (idempotent); corrupting API path documented and avoided |

## Findings register
| # | Sev | Finding | Status |
|---|-----|---------|--------|
| F1 | S1 | `capture-adb-screen`, `collect-adb-bugreport`, `pull-adb-file`, `pull-adb-package` **require** `--output-directory` — omitting it returns `VALIDATION_ERROR` and the device-audit composite silently lost its screenshot step. | **FIXED** — `_out_dir()` always supplies a directory, defaulting to `<state>/rea/artifacts/<kind>`; the composite audit now reports `failed_steps=[]`. |
| F2 | S2 | Bootstrap probed pwntools as the binary `pwntools` (it is `pwn`) → a working install was reported FAIL and re-installed on every run. | **FIXED** — probe `pwn`; plugin engine spec matches. (Same "probe the way you use it" class as the pack's QMD `require()` trap.) |
| F3 | S2 | Rizin's release archive root IS `bin/`, so `tar --strip-components=1` scattered bare binaries and the provider never landed on PATH. | **FIXED** — extract without stripping; symlink `rizin`/`rz-bin`/… into `~/.local/bin`; verified `rizin 0.9.1 @ linux-x86-64`. |
| F4 | S2 | `inspect-sqlite-database --rowLimit` **must** accompany an explicit `--table` (`row_limit requires an explicit table selection`). | **FIXED** — `--rowLimit` only emitted with `--table`; documented in the tool schema. |
| F5 | S2 | The **published 6.4.0 does NOT ship** SQLite/Rust/Go/objdump/WASM/Frida/pcap/packet/signal/Rizin/GDB families that main's README advertises — a plugin hardcoded against `main` would advertise tools the installed release cannot run. | **FIXED** — every command is validated against the *installed* manifest and returns an explicit "not in this build; build main or update" remediation; a built main tree is detected and preferred. |
| F6 | S3 | `analyze-javascript-application` on a large tree (with `node_modules`) ran >7 min and had to be killed; `--token-limit` cannot be combined with structured output (`UNSUPPORTED_OUTPUT_COMBINATION`). | **MITIGATED** — tool exposes `source_paths`, `max_heap_mb`, `analysis_timeout_ms`; responses spill to disk past `HERMES_REA_MAX_BYTES` instead of being truncated silently. |
| F7 | S3 | CLI names ≠ MCP names, and CLI args are positional (e.g. `resolve-adb-packages <serial> <query>`). | **FIXED** — all argv shapes come from REA's manifest; the plugin never guesses. |
| F8 | S1 | **`hermes_cli.config.save_config()` corrupted config.yaml.** Used to add `lazy`/`connect_timeout`/`timeout` to the `mcp_servers.rea` entry that `hermes mcp add` had just written, it appended the three keys **outside** the section, inside the file's trailing comment block — while the API read-back reported the values as present ("VERIFIED"). Only grepping the file by hand exposed it. | **FIXED + ENCODED** — `_tune_mcp_entry()` does bounded text surgery, validates with `hermes mcp list` **and** `hermes config check`, and rolls back from a timestamped backup on any failure. Lesson recorded in the plugin-patterns skill. |
| F9 | S2 | `ilspycmd` could not run: the current tool package installs `DotnetToolSettings.xml`-less builds (`dotnet tool install -g ilspycmd` fails), and the working version targets `net6.0` while only SDK 8.0 is installed → *"You must install .NET to run this application"*. | **FIXED** — pin `ilspycmd 8.2.0.7535`, wrapper execs it via `dotnet` with `DOTNET_ROOT` + `DOTNET_ROLL_FORWARD=LatestMajor`; both now in the engine env and the MCP entry env. |
| F10 | S2 | **`hermes-plugin-sync.py` silently dropped a plugin.** The scanner only matched literal `ctx.register_tool(name="x")` blocks, so a plugin registering through helpers contributed 0 tools, and `--targets soul,agents,memory` (one argparse token) made every membership check fail → "in sync" with nothing written. | **FIXED** — scanner also matches `_add("tool")` / `_simple_tool("tool")`; targets are comma-split and unknown targets exit non-zero. Inventory now reports 18 plugins / 157 tools. |
| F11 | S2 | **REA's Ghidra first query is bounded by target size, not by the install.** REA allows 330s by default for import + auto-analysis + bridge readiness; the `analyze` overview of a 150KB binary failed at **334.9s** on that default and still exited 1 with no Evidence at a 900s allowance (905.1s), while Ghidra's OWN headless run on `/bin/true` takes **15s** (6s analysis). The bridge enumerates and decompiles every procedure (134 procedures on `/bin/true`, 10 decompiler workers observed), so the cost scales with the binary. | **VERIFIED + MITIGATED** — REA-native round-trip proven on the smallest target: `overview` **22s** (evidence `ev_b31dac…`: 34 segments, 134 procedures, 132 strings, image_base 0x100000, confidence `derived`) and a `function` dossier **21s** (pseudocode + assembly + callers/callees + unresolved calls). The allowance is configurable (`HERMES_REA_GHIDRA_TIMEOUT_MS`, default 900000) and injected via the engine env + MCP entry; REA's documented rule applies (a larger server allowance does not extend the client deadline, hence Hermes' per-server `timeout: 900`), and repeat work should use REA's `--snapshot`. A large stripped binary is expected to be a multi-minute first query — documented rather than pretended away. |
| F12 | S2 | **Engine-env precedence was backwards.** The persisted `engine_env.json` overwrote same-named ambient variables, so a per-run `REA_GHIDRA_STARTUP_TIMEOUT_MS=…` was silently ignored (proven: a run exported 3600000 and the engine still reported 900000). | **FIXED** — an explicit ambient variable now WINS over the file (which is the fallback store), `HERMES_REA_GHIDRA_TIMEOUT_MS` always overrides the persisted Ghidra allowance, and PATH additions stay additive. |
| F13 | S1 | **A numeric env value invalidated the whole REA registration.** `_tune_mcp_entry` wrote `REA_GHIDRA_STARTUP_TIMEOUT_MS: 900000` unquoted; YAML types that as an integer and REA's own validator rejects the entry — *"env.REA_GHIDRA_STARTUP_TIMEOUT_MS: Server environment must be an object containing only string values"* — so `rea doctor` reported `registration:hermes` as `config_drift` and the registration counted as broken while `hermes mcp list` looked perfectly fine. | **FIXED** — every env value is written as a quoted YAML string (escaped), idempotent on re-run. Verified with REA's own doctor: `registration:hermes ok=true healthy`, health scope `healthy: true`. |
| F14 | S1 | **Every engine except Ghidra/JADX was installed but invisible to REA.** REA installs nothing and reads each tool's absolute path from its own env var (`REA_TSHARK_COMMAND`, `REA_BINWALK_COMMAND`, `REA_WABT_BIN_DIRECTORY`, `REA_WAKARU_COMMAND`, `REA_PWNTOOLS_PYTHON`, `REA_BROWSER_EXECUTABLE`, …); only 4 were wired, so the readiness report said "ready" while the tools answered *"requires … REA_BINWALK_COMMAND"* / *"REA_WABT_BIN_DIRECTORY must be absolute"* / *"Provide an absolute REA_WAKARU_COMMAND"*. | **FIXED** — all tool locations are resolved from PATH (existence-gated) and injected: adb, apktool, binwalk, ilspycmd, mitmdump, objdump, rizin, sigrok, tshark(+prlimit), unblob, wakaru, pwntools python, WABT dir, Chromium. `rea_engines` now reports **ready vs wired** separately plus an `unwired` list, so this class can never hide again. |
| F15 | S2 | **REA rejects symlinked inputs** ("Expected a regular file without a symlink"), so `/bin/ls`, `/usr/bin/python3` and friends failed for no useful reason on a normal Linux host. | **FIXED** — `_check_path` resolves a symlink to the real file before REA sees it. |
| F16 | S2 | **Three tools could not satisfy REA's own required arguments.** `debug-with-gdb` needs an inferior TTY (the plugin only sent one if the caller did), `inspect-packet-capture` needs `--fields`, `inspect-evm-interface` needs the `encoding` positional, and `inspect-with-objdump` takes an enum operation (`file_headers`, `section_headers`, …) that was undocumented. | **FIXED** — sane defaults (`/dev/null` inferior TTY, `frame.number` fields, `raw` encoding), the documented `--inferior-tty` spelling, and the operation enum listed in the tool description. |
| F17 | S2 | **The generic runner's 300s default killed engine commands mid-analysis.** `rea_run` on an analysis command (e.g. `address-to-file-offset`) died at 303s and surfaced REA's *"Analysis was cancelled"* — which reads like an REA bug rather than the plugin's own deadline. | **FIXED** — the default timeout is now chosen per command class (1800s for analysis-engine commands, 300s otherwise, capped by `HERMES_REA_LONG_TIMEOUT`), and a timeout adds `timed_out`/`timeout_s`/`timeout_note` explaining exactly what stopped it. |
| F18 | S2 | **`recover-javascript-sources` could never succeed.** REA requires the output directory to be ABSENT, while the plugin created it (`mkdir(parents=True, exist_ok=True)`) before calling — every call failed with "Analysis input is invalid". The opposite convention applies to capture commands, which need it to exist. | **FIXED** — `_absent_dir()` supplies a genuinely absent path (never deleting caller data: an existing path becomes a fresh timestamped sibling, reported back in `output_directory`). |
| F19 | S2 | **Tool upgrades never reached REA.** The engine env file is a cache of derived values, and stale paths still satisfied "exists", so installing the pinned WABT 1.0.42 (REA rejects 1.0.36 with a version-banner error) or binwalk 3.1.x had no effect — the old path kept being injected. | **FIXED** — derived tool locations are refreshed whenever the current derivation differs, and a path-like key whose target has disappeared is dropped (self-healing after an uninstall). Operator intent still wins via ambient env (F12). |
| F20 | S2 | **Symlinked TOOL paths are rejected too.** `~/.local/bin/wakaru` (npm shim) and pipx entry points are symlinks; REA answered *"Wakaru --version failed; reported version: ; exit: null"*, the same defect class as F15 applied to engine wiring rather than inputs. | **FIXED** — every location-valued engine key is resolved to its real file before injection. |
| F21 | S2 | **`action=classes` on an APK always failed.** REA requires a non-empty class query and rejects an empty string as a missing argument. | **FIXED** — the omitted query now sends REA's documented match-all form `"."`; a real APK fixture then returned matches, members, decompiled method bodies and incoming references. |
| F22 | S2 | **The APK/JS "output directory" conventions are OPPOSITE per command.** Capture commands need the directory to exist (F1), `recover-javascript-sources` needs it to be ABSENT (F18) — and the plugin applied one convention to both. | **FIXED** — `_out_dir()` (exists, unique per run) and `_absent_dir()` (never pre-created, never deletes caller data) are used per command. |
| F23 | S2 | **`--record` on `inspect-web-network-capture` means "select record ordinals", not "start recording".** Guessing the flag's meaning produced *"Missing value for flag: --record"*, and boolean-as-sentinel passing (`--record true`) was wrong for a repeatable array option. | **FIXED** — the tool exposes `records` (zero-based ordinals, list or comma string, repeatable `--record N`) plus `sensitive_values`, validated against REA's four producer formats (har|mitmproxy|saz|qlog). Proven on a real mitmproxy capture: 3 records with byte-range locations, ordinal selection honoured. |
| F24 | S2 | **The CDP endpoint was demanded for every web action, including capture inspection.** `action=network` needs no browser, so the requirement made HAR/mitmproxy inspection unreachable; the `target_id` gate sat above the network branch for the same reason. | **FIXED** — the endpoint is required only for CDP-backed actions and the network branch runs before the target gate. |
| F25 | S1 | **Ubuntu's tshark AppArmor profile forbids the Hermes scratch dir.** REA stages the capture into `TMPDIR` and hands it to tshark; Hermes sets `TMPDIR` to `~/.hermes/cache/scratch`, which the profile denies — every pcap/packet inspection died with *"tshark: You don't have permission to read the file"* while the same file read perfectly from `/tmp` (`aa-status` lists the `tshark` profile). | **FIXED** — REA's subprocess tree gets `TMPDIR=TEMP=TMP=/tmp` (override `HERMES_REA_TMPDIR`); pcap inspection then reported 5,037 packets with endpoints and capture window, and packet field selection works. |
| F26 | S1 | **Each engine FAMILY needs its own resource limiter.** REA runs caller-supplied tools through `prlimit` read from `REA_<FAMILY>_PRLIMIT_COMMAND`; only tshark/sigrok were wired, so JavaScript (wakaru), EVM and firmware had `limiter: undefined` and their spawn reported *"exit: null"* with an empty version — F14's class, one level deeper. | **FIXED** — all five family limiters are wired (TSHARK, SIGROK, JAVASCRIPT, EVM, FIRMWARE). |
| F27 | S1 | **REA fingerprints the tool FILE, so the npm shim is not Wakaru.** `wakaru` from npm is a `#!/usr/bin/env node` script; REA identifies the release from the file itself and rejected it before running (*"Wakaru --version failed; reported version: ; exit: null"*), even with node on PATH and a PATH-independent wrapper. | **FIXED** — the native `wakaru-linux-x64` 1.14.0 build (the version REA verifies with) is installed under `tools/` and preferred; JS source recovery then succeeded (decompiled `bundle.js` artifact on disk). The npm shim remains a wrapped last resort. |
| F28 | S2 | **Node-based engines need node on PATH.** REA's own tool env does not carry the node bin directory, so npm shims cannot start; the engine PATH now leads with it. | **FIXED** — `PATH_EXTRA` starts with node's bin dir and PATH_EXTRA is refreshed like the other derived values (a new entry previously never reached the stored file). |
| F29 | S2 | **REA's array options take ONE value per flag.** Passing `--fields frame.number,ip.proto` failed with *"Some fields aren't valid: frame.number,ip.proto"*. | **FIXED** — `_as_list()` expands comma lists into repeated flags (`--fields a --fields b`); verified on a real pcap with a two-field selection. |

## Max-utilization batches (B1–B6)
Baseline measured 2026-10-14: **153 CLI commands** (built main tree), **207 MCP tools / 6 prompts**
(live handshake 2.6s), **54 plugin tools** (32 composite + 22 one-to-one), **21/21 engines**,
**22 one-to-one mappings verified against the installed manifest (0 stale)**. 66 commands have no
dedicated tool and are reached through `rea_commands` + `rea_run` — reachability, not coverage loss,
but it must be *proven*, not assumed.

| Batch | Goal | Method / evidence | Status |
|---|---|---|---|
| **B1** Reachability + wiring matrix | No dormant, mock or unwired handler anywhere; the long tail really executes | Call **all 54 plugin tools** with minimal/safe args and require a structured answer (ok, or an error that names the missing input) — zero tracebacks, zero silent no-ops. Exercise `rea_run` on the four command shapes (positional-only, option-heavy, main-only, unknown→refused). Call **MCP tools end-to-end** through the registered wrapper (initialize → tools/call), not just `tools/list`. Benchmark per class. | **DONE** — final matrix over 59 cases covering all 54 tools: **49 OK, 0 FAIL, 0 tracebacks, 0 silent no-ops**; the 8 non-OK cases are 3 documented preconditions (Flutter-AOT APK, sigrok capture, rooted device for frida *instrument*), 3 harness-arg cases whose tools were proven separately (CDP web family 5/5, screenshot compare identical-vs-different, endpoint-scoped targets), 1 deliberately-slow Ghidra command that the harness capped at 240s (the plugin allows 1800s), and 1 `rea_compare` case that needs its per-kind artifact pair (documented). `rea_run` verified in all four shapes (unknown command → structured refusal, never a crash). Run 1 (34 OK/21 error) is what produced F14–F21. |
| **B2** Hermes integration | The MCP surface is really usable from Hermes, not merely registered | Entry audit (enabled/lazy/connect/timeout/env, no include/exclude so nothing is filtered); deferred-catalog budget with 207 tools (tool_search reachability at `max_tokens=4000`); first-call cost after a lazy connect; profile-aware config/skill paths; installed REA skill visible to Hermes. | **DONE** — entry carries command/args/env/enabled/lazy/connect_timeout 120/timeout 900 with **no** `tools.include|exclude`, so nothing is filtered; Hermes' own discovery reports **209 `mcp__rea__*` tools registered** (verified via `tools.mcp_tool_discovery.discover_mcp_tools()` in Hermes' runtime venv — a deliberate check, since the CLI paths are unusable while the interrupted source update is pending); live handshake 2.6s for initialize+tools/list (207 tools, 6 prompts); REA's own skill is installed and visible (`reverse-engineer-anything`). |
| **B3** Engine depth | Each engine proves a REAL bridge, not just a binary on PATH | One real invocation + evidence id per engine: ghidra (overview 22s / function 21s — done), jadx-MCP (Android decompile on a real APK), rizin, objdump, gdb, frida (device listing), pwntools, mitmproxy, tshark, binwalk/unblob, sigrok, wabt, wakaru, ilspycmd, apktool. Anything needing hardware or an artifact this box lacks is recorded with its exact precondition. | **IN PROGRESS (most families proven live)** — Ghidra native overview 20s + function dossier 20.5s; Android static 6/6 on a real APK fixture (package/classes/class/method decompile/refs/resources); CDP web family 5/5 (targets, page, session, screenshot with real artifact sha256+viewport, network capture inspection of a real mitmproxy recording, visual regression `identical` vs `different`); binaries: objdump, rizin 8.5s, binary-meta (auto/go/rust/strings), pwntools-backed ELF layout, gdb, crash from a REAL ELF core; formats: sqlite (schema+rows), WASM (WABT 1.0.42), EVM interface, firmware regions (binwalk 3.1.0 — the version REA verifies with), dart/flutter detection; Android live: 11 device tools on a physical phone. Remaining preconditions: frida *instrument* (device is not rooted), sigrok (no logic-analyzer capture), Hopper/IDA/JEB (paid/closed), pwndbg (optional core mapping). |
| **B4** Durability + fresh-machine install | Survives reboot, `hermes update`, and installs itself on a new box | `tools/survive.sh` re-applies the plugin copy + MCP entry after a simulated loss; `install-ultimate.sh` covers plugin enable + engine bootstrap; byte-identity repo↔install; inventory regeneration; TPS counters; stdlib-only (no Hermes core import). | PENDING |
| **B5** Docs + tracking | One authoritative, verified record | This file, `docs/VERSION_MATRIX.md`, `docs/COMPLETENESS_AND_MAX_UTILIZATION_CHECKLIST.md`, the engine table and the installed skill all agree with the live surface. | PENDING |
| **B6** Adversarial sweep | Re-audit every claim with live probes | Hunt the defect classes this work already produced: silent no-op / ignored argument (F12 class), stale command mapping, unbounded growth (evidence store, manifest cache), concurrency and timeout/kill semantics, output caps, garbage input, retention/prune. | PENDING |

Verification discipline for every batch: evidence from the live system (ids, counters, timings,
exit codes) recorded here **before** the batch is marked done; anything unverifiable is listed as an
open item instead of claimed.

## Open items
1. **Large-binary native analysis is a multi-minute first query.** Verified working (22s overview /
   21s function dossier on `/bin/true`), but the REA bridge decompiles every procedure, so a large
   stripped binary exceeded a 900s allowance on this host. Repeat work should use REA's `--snapshot`
   to persist the Ghidra project, and the allowance is tunable via `HERMES_REA_GHIDRA_TIMEOUT_MS`.
2. **Deliberate preconditions (not defects).** `instrument-with-frida` needs a rooted device or a
   gadget-instrumented app (the test phone reports `uid=2000(shell)`); `decode-signal-recording`
   needs a logic-analyzer capture (no capture hardware here); Hopper / IDA / JEB are paid or closed
   and stay caller-supplied; pwndbg (crash `debugger_context`) is optional and not installed.
3. **Android method decompilation** needs the JADX *headless MCP* jar (installed, sha256 pinned);
   the plain `jadx` CLI is a different provider and is not a substitute.

## Definition of done
1. Repo ↔ install byte-identical for `plugins/hermes-rea`.
2. Every engine either ready or reporting an honest, actionable remediation (no silent empties).
3. Android, artifact, sqlite, process and manifest-executor paths exercised live with stored evidence.
4. Tool calls surface in the TUI (TPS prefix wired).
5. `hermes-plugin-sync.py --targets soul,agents,memory` regenerated the plugin inventory.
6. Checklist reflects reality at push time.
