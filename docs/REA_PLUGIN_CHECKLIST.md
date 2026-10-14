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
| F30 | S2 | **The registration detector reported a registered server as missing.** It required the entry to be the block immediately following `mcp_servers:`; Hermes' own `mcp add` appends the entry BELOW other entries and comment blocks, so after a re-add the detector said "not registered" while `hermes mcp list` showed it enabled — and the bootstrap would have "failed" a healthy install. | **FIXED** — the scan now walks the `mcp_servers` section until the next top-level key, tolerating comment blocks (YAML ignores them), and the bootstrap asserts `registered: true` rather than merely `ok: true`. |
| F31 | S1 | **Hermes' own CLI was unusable, which broke every CLI-based validation.** `~/.hermes/hermes-agent/hermes_cli/plugins.py` still contained `<<<<<<< ours` / `>>>>>>> theirs` markers from an interrupted source update (2 blocks), so `hermes config check` died with `SyntaxError` and `_tune_mcp_entry` rolled back — the config was protected, but the MCP entry could not be re-added through the CLI. (The markers in `tests/test_audit_old_updater_imports.py` are fixture DATA inside a string and were correctly left alone.) | **FIXED** — both blocks resolved to the upstream side with per-file backups and an AST check, then verified: `hermes config check` runs, `hermes mcp list` shows `rea` enabled, and the plugin's own re-registration path succeeds. The pack documents this recovery; it is deliberately NOT auto-applied by the self-heal script, because silently choosing a conflict side could corrupt a tree. |
| F32 | S2 | **Self-inflicted: an over-broad text splice deleted an unrelated definition.** While replacing the duplicated `_mcp_registration_state`, a `s[:start] + new + s[end:]` edit removed `_SERIAL_RE`, and `rea_mobile_audit` failed with `name '_SERIAL_RE' is not defined`. Caught by the plugin smoke suite (1 failure) and restored. | **FIXED** — constant restored, smoke suite green again. Lesson recorded: anchored edits on this file must be verified by the smoke suite, and duplicate-definition drift is a signal the splice was wrong. |

## Adversarial sweep (round 2 — the "any more missing/flaw/race/loophole/blindspot?" pass)

Angles swept: declared-but-unread arguments · shared-state concurrency · collision windows ·
silent coercion · error-contract completeness · growth/retention · permissions · the SECOND
surface (MCP) · process leaks · adversarial argument types · host-level traps.

| # | Severity | Finding | Fix |
|---|---|---|---|
| F33 | **S1** | **The output directory was unique per SEC, not per call.** `_out_dir()` built the leaf from `%Y%m%dT%H%M%S` + `exist_ok=True`, so two calls in the same second shared a directory — and REA refuses to write into a non-empty target, producing the very "Analysis returned an unreadable result" failure the timestamp was added to prevent. Parallel agents or an adb screen next to a bugreport hit it. | **FIXED** — the leaf is `stamp-<pid>-<seq>` created with an atomic `os.mkdir` (fails if present, retried), sequence counter guarded by a lock. Verified: 8 sequential + 12 parallel calls → 20/20 distinct, all empty. |
| F34 | S2 | **Self-inflicted during F33's fix**: the atomic leaf used `os.mkdir` without creating the parent, so every fresh kind returned "cannot create output_directory" (8/8 failures). | **FIXED** — the base is `mkdir(parents=True, exist_ok=True)` first, then the atomic leaf. |
| F36 | **S1** | **The state tree was world-readable** (`775` dirs / `664` files) while it holds captured artifacts, PCAP payloads and binary strings — on a box where `/home` is `755`, any local user could read them. | **FIXED** — new files land `0600` (chmod'd before the rename) and directories `0700`, with a heal pass over the existing tree. Verified: dirs 700, files 600. |
| F37 | **S1** | **Retention covered only evidence envelopes.** Artifact directories (adb captures, screenshots, decompiled trees), spilled oversized outputs and per-call `spec-*.json` files grew without any bound — the classic unbounded-growth blindspot. | **FIXED** — the prune now covers evidence + artifacts + spill + specs, adds a total-size ceiling for artifacts (default 2048 MB, oldest-first eviction, newest 20 always kept) and reports `by_kind` counters. Verified: an aged file is removed while a fresh one survives; a 4 MB tree is evicted to 2 MB keeping the newest. |
| F38 | S2 | **Numeric arguments were silently coerced.** `limit="many"` returned the default 40 and `limit=-5` was clamped to 1 with no indication, so a caller could not tell its request had been ignored. | **FIXED** — `_num_arg()` returns `(value, note)` and the payload carries a `notes` list naming every coercion. Verified: `"many"` → note, `-5` → "below the minimum 1", `9999` → "above the maximum 200". |
| F39 | **S1** | **Failures could carry an empty `error`.** `_exec` returned only `{ok, command, argv, exit_code, stderr, result}`; when REA put its reason in `result.message` the top-level `error` stayed absent — a caller branching on `error` (the documented contract) saw a failure with no reason. | **FIXED** — a non-zero exit synthesises `error` from `result.message` → `result.code` → stderr → exit code. Verified: `adb_screen serial=12345` now explains "Unsupported target for capture_adb_screen at 12345". |
| F40 | **S1** | **The MCP wrapper was a setup-time snapshot.** `_write_mcp_wrapper()` was only reachable from `rea mcp setup`, so installing or upgrading an engine AFTER setup left the wrapper exporting a stale env — the plugin's tools saw the engine while everything reaching REA through MCP did not ("installed but invisible", via the second surface). | **FIXED** — `_wrapper_body(env)` is shared and `_sync_mcp_wrapper()` rewrites an existing wrapper whenever the engine env differs (never creates one — that stays an explicit setup step). Verified: a deliberately corrupted wrapper is rebuilt on the next tool call (26 exports restored, stale line gone). |
| F41 | S2 | **Spec files could collide and were never pruned.** `_write_spec` used a millisecond stamp as the whole name (two calls in one millisecond overwrite each other) and nothing ever deleted them. | **FIXED** — the name carries the sequence counter and specs are covered by the retention prune. |
| F42 | **S1** | **Self-inflicted, and the most dangerous of the round**: the new permission heal chmod'd every file in the state tree to `0600` — including the MCP wrapper, which Hermes launches by path. Executing it returned **126** and the entire MCP surface would have been dead, invisibly until an MCP call was attempted. | **FIXED** — `_harden()` preserves execute bits (they become owner-only `0700`) and the wrapper is re-written `0700`. Verified: mode 700, executable, and a full MCP handshake returns **207 tools, exit 0**. |
| F43 | **S1** | **Registered, handshake-perfect — and invisible.** `mcp_servers.rea` was healthy (`hermes mcp list` enabled, 207 tools / 6 prompts over the wrapper) yet `tool_search` listed **no `rea` source at all**: the live surface showed 157 tools, all from the `hermes` server. A lazily-registered server gets a tool manifest cached at first use; when that manifest is empty the server contributes zero discoverable tools, so REA's 207-tool surface could not be found by search or by the deferred catalog — only by already knowing an exact name. This directly contradicted a claim made in the previous pass ("the surface arrives uncapped"), which is corrected here. | **FIXED** — the entry is now **`lazy: false`** (eager registration, so the real manifest is probed at session start; the connect costs 2.6s, measured) and the plugin's tuner re-asserts that value on every run (with the reason in a comment, and only reporting `changed` when it really differs) so an accidental revert heals. `hermes config check` exits 0 and the entry reads `lazy: false` with `registered: true`. **Verification for the next session:** `tool_search` should now list a `rea` source carrying ~207 tools; a failed start degrades gracefully (Hermes logs the connect error and cools the server down without affecting other servers). |
| F44 | S2 | **Self-inflicted while fixing F43**: the tuner's lazy re-assertion flagged `changed: true` on every run even when the value already matched, so each tune claimed work it had not done (and rewrote the config needlessly). | **FIXED** — the value is compared first; two consecutive tunes now report `changed: false, verified: true`. |
| F45 | **S1 (blocker)** | **Infinite recursion in the engine-env path — the plugin stopped loading entirely.** `_load_engine_env` → `_sync_mcp_wrapper` → `_wrapper_body` → `_rea_argv` → `_engine_subprocess_env` → `_load_engine_env` → … (introduced by the F40 fix). A fresh process never finished importing: Hermes killed the load with *"Failed to load plugin 'hermes-rea': load timed out after 10s"*, the all-plugins loop attributed **304s** to `register()`, and the whole 54-tool surface silently disappeared. Reproduced with a stack dump (`faulthandler`), which showed the repeating frame set. | **FIXED three ways, so it cannot recur**: (a) the wrapper refresh reuses the `exec` line already in the file instead of resolving the launcher (refreshing the env is not re-resolving the CLI); (b) a re-entrancy guard around the refresh; (c) `_engine_subprocess_env()` uses the read-only `_load_engine_env(persist=False)` form, so the subprocess-env path can never write or refresh. Verified: import **1.58s**, register **0.00s**, 54 tools, and Hermes' own loader no longer reports a timeout. |
| F46 | S2 | **Registration probed every engine.** The load-time log line evaluated `_engine_report()` (a readiness probe over 21 engine specs), i.e. plugin registration could spawn subprocesses before the plugin was even usable. | **FIXED** — registration now logs `len(ENGINE_SPECS)` and points at `rea_engines`/`rea_status`, where readiness is a runtime question and the result is cached. |
| F47 | **S1 (verification process)** | **I measured the wrong file.** My fixes verified fast (1.6s) in the repo copy while Hermes still timed out, because Hermes loads the **installed** copy — which was three edits stale. Chasing the difference is what exposed F45 a second time. | **FIXED as a rule** — byte-identical sync (`cp` + sha256 compare) is now done *before* any load-time measurement, and the install copy is part of the verification checklist. |
| F48 | **S1 (blocker)** | **The preserved exec line was a complete statement.** F40's refresh appended the suffix to a line that already ended in `mcp "$@"`, producing `rea.mjs mcp "$@" mcp "$@"` — REA exits 1 with no output, so the entire MCP surface was dead while the wrapper still *looked* healthy. | **FIXED** — a preserved line is emitted verbatim, plus `_normalise_exec_line()` collapses any duplicated suffix so an already-broken wrapper heals instead of persisting. Verified: handshake **exit 0, 207 tools**. |
| F49 | observation (pack-level) | **`plugins/_shared/deps.py ensure_deps()` costs ~1.5s at import for every plugin** (18 plugins ≈ 27s of load path), against a 10s per-plugin timeout. It is the tightest constraint on plugin load and the reason contention can push a heavy plugin over the limit. | **RECORDED, not changed** — it is pack-wide infrastructure outside this task; changing how all 18 plugins resolve deps is a separate, riskier piece of work than this round's scope. Noted here so it is not rediscovered. |
| F50 | **S1 (corrects my own claim)** | **The raw MCP surface is registry-only — it has no toolset, so no session can reach it.** `resolve_toolset('mcp-rea')` returns 209 tools after discovery, but `get_all_toolsets()` (80 entries) contains no `mcp-*` key and the name is absent from `platform_toolsets`, so nothing ever enables it. `hermes tools enable mcp-rea` → *"Unknown toolset"*. The `mcp__hermes__*` mirror DOES reach sessions only because a toolset named `hermes` happens to exist (static), and `get_toolset('rea')` resolves the **plugin** toolset branch (54 tools) — the same-named server is shadowed by the plugin branch, exactly as Hermes' own source comment warns for the static case. The previous round's "366 tools from two sources" was the **MCP registry**, not the session surface: my closure claim was too optimistic and is corrected here. | **RESOLVED AS DESIGN, not forced.** Measured session surface: **54 bare `rea_*` + 54 `mcp__hermes__rea_*` — both reachable and searchable** (`tool_search` finds them: "inspect rust binary" → `rea_binary_meta`, "adb bugreport" → `rea_adb_bugreport`). The 207 raw names are for MCP-protocol clients of the wrapper; functional coverage is complete without them because **`rea_run` executes any of REA's 153 CLI commands** (validated against the manifest) and the curated 54 cover the task-shaped workflows. Renaming the plugin toolset was tested and made it *worse* (both keys disappeared), which is the evidence for this conclusion rather than a guess. |
| F51 | S1 (self-inflicted, performance) | **The retention sweep ran on EVERY evidence write.** Extending the prune (F37) put an `rglob` + stat pass over the artifact tree in the hot path: measured **13 ms** with 81 files, scaling with the tree (hundreds of ms after a month of captures) — a per-call cost for work that only needs hourly resolution. | **FIXED** — `_maybe_prune()` runs the sweep at most once per `HERMES_REA_PRUNE_INTERVAL` (default 3600 s) while an explicit `rea_evidence action=prune` still runs immediately. Verified: 5.1 ms first call, **0.003 ms** gated, explicit prune unchanged. |
| F50² | **closed live** | **The raw MCP surface (F50) is now genuinely reachable — verified in a live session, not inferred.** After the server reconnected, `tool_search` reports `total_available: 475` (was 266) and `available_sources` now contains **`rea` with 209 tools**; a query that previously returned **zero** matches ("sigrok logic analyzer capture") returns `mcp__rea__decode_signal_recording` with its full schema. Two raw tools were then **called end-to-end through the bridge**: `mcp__rea__list_adb_devices` → Evidence envelope with the real device (`XC8LJR4TQKN76PZ9`, CPH2321, `confidence: observed`); `mcp__rea__inspect_sqlite_database` → artifact sha256 + 1,769,472 bytes, SQLite 3.53.4, complete 18-column schema, sampled rows with `bytes_hex`/encoding, plus REA's own documented limits. | **RESOLVED** — the earlier "registry-only" state was the pre-reconnect reality (measured), and the round-4 conclusion stands as the reason it was invisible then; this entry records that it is live now, with calls as proof. Operational note: the surface appeared after an MCP reload, so a session that started before the `lazy: false` change will not see it until it restarts. |
| F52 | S2 (wiring) | **The pack's usage indicator silently ignored the two new name shapes.** `hermes-tps` maps tool names by prefix and only knew `rea_`, so every raw call (`mcp__rea__<tool>`, 209 tools) and every pack-mirror call (`mcp__hermes__rea_*`, 54) fell into "other" — REA usage would look far smaller than it is. | **FIXED** — both prefixes map to the `rea` bucket (🧩 REA). Verified: `rea_status`, `mcp__rea__decode_signal_recording`, `mcp__hermes__rea_apk` all classify to `rea`, while `codegraph_search` → `codegraph` and `browser_exec` → `None` are unchanged. Install copy byte-identical. |
| F53 | S2 (coherence, documented not "fixed") | **Raw MCP calls are not retained in the evidence store.** The store holds plugin-mediated results only: `mcp__rea__list_adb_devices` matched an existing file because REA's evidence ids are deterministic (same operation + inputs → same id, so the plugin had already written it), while `mcp__rea__inspect_sqlite_database` left **no** file. A caller using the raw surface gets its envelopes in-conversation but nothing persists locally, so `rea_evidence action=list` silently under-reports what was actually observed. | **DOCUMENTED as the surface contract** — retention is a property of the plugin path (`rea_*` tools, or `rea_run` for any of the 153 commands); the raw server is REA's own process and the plugin cannot intercept it. Anyone needing a durable record should call through the plugin. Side benefit confirmed: deterministic evidence ids make persistence idempotent (a re-run of the same operation overwrites nothing). |
| F54 | **S1** | **No tool accepted a per-call deadline — 0 of 54.** A Ghidra-backed call therefore ran to its class default (up to `REA_LONG_TIMEOUT` = 1800 s), and passing `timeout_s` was silently ignored (exactly the silent-ignore class fixed for `limit` in F38, but systemic). Measured: `rea_native` with `timeout_s=20` kept analysing past 420 s of wall clock. | **FIXED centrally** — `_bounded()` wraps every handler, `_add()` injects the argument into every schema, and `_exec()` caps the effective deadline (caller value, itself capped at `REA_LONG_TIMEOUT`). Verified: **54/54 tools declare `timeout_s`**, a bad value returns a note, and `rea_native timeout_s=20` now returns `timed_out=true`, exit 124, in **22.8 s**. |
| F55 | S2 | **A TERM-ignoring child had no escalation.** The timeout path signalled the process group with SIGTERM and then waited 20 s — but if a JVM ignored TERM the group was simply left running (the deadline would not actually stop the work). | **FIXED** — SIGTERM → 20 s grace → **SIGKILL escalation** → 10 s → honest note ("the process group ignored SIGTERM; killed it"). |
| — | **closed** | **The orphan question, deferred twice, is now measured.** A Ghidra-backed run killed at a 20 s deadline was followed for 45 s: **0 surviving JVMs** at +4 s, +20 s and +45 s (java count 0 before/after), and no REA node leftover beyond the session's own long-lived MCP server. The group-SIGTERM design works; the escalation above is the safety net for the case that did not occur here. | **VERIFIED, no leak.** |
| F56 | S2 | **`rea_doctor` reported an opaque "failed with exit code 1".** REA audits *every* provider, so the deliberately-absent paid/closed ones make an audit-wide run unhealthy — a readiness tool that fails without saying why. | **FIXED** — the handler now names the failing checks and the right scope ("use client=\"hermes\" for the registration-only verdict; see rea_engines for the free engines"), and adds `failed_checks` to the payload. |
| F57 | **S1** | **The Hermes MCP entry registered a wrapper command, which REA classifies as foreign.** `rea doctor` therefore reported `registration:hermes = config_drift` — and it was **not** the `lazy` flag: a controlled flip to `lazy: true` left the drift unchanged (proven, not assumed). REA's `registrationAligned()` only accepts `npx|bunx <rea-package> mcp`, `[node, <abs>/rea.mjs, "mcp"]` or the REA CLI. | **FIXED** — the entry now uses the canonical form (`command: <node>`, `args: ["…/rea.mjs", "mcp"]`) with the env block it already had. Verified: the direct command serves **207 tools using only the entry's env** (so the wrapper was redundant on this path), REA's doctor is **healthy**, `rea_mcp_status` reports `registered: true` with `shape: canonical`, and `rea-bootstrap.sh --check` exits 0. The wrapper file is kept for non-Hermes MCP clients and is documented as such; this also removes the wrapper-staleness class (F40/F43/F48) from the Hermes path entirely. |

**Status of the three rea surfaces, measured (this supersedes the F43 closure text and the F50
round-4 text above).** A live session now reports **366 MCP tools / 475 total available**, and the
same capability is reachable three ways:

| surface | count | how it reaches a session | verified by |
|---|---|---|---|
| `rea_*` (this plugin's curated tools) | 54 | plugin toolset `rea`, enabled by default | real `tool_search` + smoke suite |
| `mcp__hermes__rea_*` (the pack's MCP mirror) | 54 | toolset `hermes` | real `tool_search` |
| `mcp__rea__*` (REA's own raw tools) | 209 | **now live** (server reconnected; `lazy: false`) | `tool_search` source `rea` (209) + **two end-to-end calls** |

Duplication is deliberate and documented: the curated tools add the pack's task-shaped workflows,
the mirror gives MCP clients the pack's surface, and the raw tools expose REA's full 209-tool
protocol surface with its own schemas (`required` fields included, as shown by
`mcp__rea__decode_signal_recording` → `required: ["path","decoders"]`). One capacity note from REA's
own limits: it admits **at most two concurrent inspections per provider** and queues the rest, so
running many heavy analyses at once (raw + plugin) is bounded by REA rather than by the plugin.
Cost note: with 333 deferrable tools the embedded catalog listing degrades from full descriptions
to a names-only listing to stay inside its token budget — search and call still work (proven), and
this is the standing trade of having the full raw surface enabled.

Swept and found CLEAN (stated so the next pass does not re-do them): 54/54 registered tools declare
no unread arguments · 16/16 adversarial argument shapes return structured errors with no traceback
(int/None/list/relative/dotted paths, `/etc/shadow`, 300-char junk, `rm -rf /`, `; whoami`, a directory
where a file is expected, unknown enum, missing required field) · 24 concurrent engine-env accesses and
24 concurrent writes leave valid JSON (temp + `os.replace`) · unknown commands are refused with a hint ·
no orphaned java/ghidra/node processes after the analysis runs · the wrapper already exported the full
engine env including all five per-family `prlimit` limiters.

### Batch reconciliation (background results, all processed)

* **Ghidra scale limit now has hard numbers** (a standalone probe, independent of the plugin):
  `/bin/true` → **rc=0 in 15 s** (analysis 6 s); `/bin/ls` (≈150 KB, stripped) → **rc=124 at the 900 s
  ceiling**, still decompiling. So the earlier "large-binary scale limit" is confirmed, not a plugin
  deadline artefact: the Ghidra bridge decompiles every procedure, and a big stripped binary is a
  multi-minute-to-unbounded first query. Workaround unchanged and documented: REA `--snapshot`, or
  raise `HERMES_REA_GHIDRA_TIMEOUT_MS` and accept the wall-clock.
* **binwalk 3.1.0 is installed and wired** (`/home/lot399/tools/binwalk-3.1.0/bin/binwalk --version` →
  `binwalk 3.1.0`). Three earlier cargo attempts failed on missing `fontconfig` (`yeslogic-fontconfig-sys`
  build error) and were superseded once `libfontconfig1-dev` + `pkg-config` were installed; the 122 MB of
  stale `cargo-install*` scratch trees left by those attempts were removed.
* **Fixture builder gaps reconciled**: `loop.pcap` exists (1.2 MB, captured on loopback) and the crash
  fixture is a real ELF core (`rea_fixtures/crash.core`, 467 KB) — both were missing from the older
  `build_fixtures.sh` summary because later runs produced them by other means. `apk/base.apk` was a
  1.5 KB stub from a `aapt2` run that could not find `android.jar`; the APK tests use
  `apk2/base.apk` (3.4 KB, built against a real `android.jar`).
* **Three matrix cases were harness bugs, now fixed and re-proven live**: `rea_web` needed a CDP
  `endpoint` (now `ok=True`, browser Chrome/153 protocol 1.3), `rea_run_options` passed table
  `Provider` where the DB has `ProviderConnection` (now `ok=True`), and `rea_run_positional` used
  `/bin/ls` (unreachable inside any deadline) — the positional shape is proven with
  `inspect-artifact <path>` (`ok=True`).
* **Still fixture-limited, stated honestly**: `rea_screenshot_compare` needs TWO screenshot artifacts.
  The tool works (a real capture returns `ok=True` with a full `artifact`), but this headless Chrome
  build exposes only its launch tab to REA's target listing ("Multiple targets are not supported in
  headless mode" — one URL per launch; extra tabs via the CDP API are not listed), so I could not
  produce a second artifact in this round. The compare itself was proven in an earlier round
  (identical vs different). Fixture script corrected to one launch URL + CDP tab creation.
* **Two of my own probe bugs, both the same class**: reading a payload at the wrong nesting level
  (REA returns `result.normalized_result.*`, not `result.*`) produced "0 targets" and "no artifact"
  from working tools, and a `Ctx` harness used before its class attribute existed produced a
  traceback instead of data. Rule reinforced: verify the API exists and the payload shape before
  treating a probe result as evidence about the plugin.

## Max-utilization batches (B1–B6)
Baseline measured 2026-10-14: **153 CLI commands** (built main tree), **207 MCP tools / 6 prompts**
(live handshake 2.6s), **54 plugin tools** (32 composite + 22 one-to-one), **21/21 engines**,
**22 one-to-one mappings verified against the installed manifest (0 stale)**. 66 commands have no
dedicated tool and are reached through `rea_commands` + `rea_run` — reachability, not coverage loss,
but it must be *proven*, not assumed.

| Batch | Goal | Method / evidence | Status |
|---|---|---|---|
| **B1** Reachability + wiring matrix | No dormant, mock or unwired handler anywhere; the long tail really executes | Call **all 54 plugin tools** with minimal/safe args and require a structured answer (ok, or an error that names the missing input) — zero tracebacks, zero silent no-ops. Exercise `rea_run` on the four command shapes (positional-only, option-heavy, main-only, unknown→refused). Call **MCP tools end-to-end** through the registered wrapper (initialize → tools/call), not just `tools/list`. Benchmark per class. | **DONE** — final matrix over 59 cases covering all 54 tools: **49 OK, 0 FAIL, 0 tracebacks, 0 silent no-ops**; the 8 non-OK cases are 3 documented preconditions (Flutter-AOT APK, sigrok capture, rooted device for frida *instrument*), 3 harness-arg cases whose tools were proven separately (CDP web family 5/5, screenshot compare identical-vs-different, endpoint-scoped targets), 1 deliberately-slow Ghidra command that the harness capped at 240s (the plugin allows 1800s), and 1 `rea_compare` case that needs its per-kind artifact pair (documented). `rea_run` verified in all four shapes (unknown command → structured refusal, never a crash). Run 1 (34 OK/21 error) is what produced F14–F21. |
| **B2** Hermes integration | The MCP surface is really usable from Hermes, not merely registered | Entry audit (enabled/lazy/connect/timeout/env, no include/exclude so nothing is filtered); deferred-catalog budget with 207 tools (tool_search reachability at `max_tokens=4000`); first-call cost after a lazy connect; profile-aware config/skill paths; installed REA skill visible to Hermes. | **DONE** — entry carries command/args/env/enabled/lazy/connect_timeout 120/timeout 900 with **no** `tools.include|exclude`, so nothing is filtered. Reproducible proof (any session can re-run this; the CLI is not needed): speaking MCP over the registered wrapper returns **207 tools and 6 prompts from `serverInfo: rea 6.4.0`** in a single `initialize` → `tools/list` → `prompts/list` exchange, and `mcp__hermes__rea_*` (the pack's own MCP server) serves the curated 54 tools, which `tool_search` reaches. **Corrected in round 2 (F43):** the raw surface is registered and handshake-perfect but was *not discoverable* — `tool_search` listed no `rea` source while the entry was `lazy`, because a lazily-registered server contributes whatever manifest it cached at first use, and an empty one means zero discoverable tools. The entry is eager (`lazy: false`) now so the manifest is probed at startup; the next session should list a `rea` source of ~207 tools. Registration alone was never proof of reachability — that distinction is the finding. `hermes mcp list` shows `rea` enabled; REA's own skill is installed and visible (`reverse-engineer-anything`). *Note for the next session: importing `tools.mcp_tool_discovery` in-process can fail with `No module named 'ruamel'` even in a venv where ruamel imports fine on its own, so prefer the handshake above (or `hermes mcp list`) as the registration proof.* |
| **B3** Engine depth | Each engine proves a REAL bridge, not just a binary on PATH | One real invocation + evidence id per engine: ghidra (overview 22s / function 21s — done), jadx-MCP (Android decompile on a real APK), rizin, objdump, gdb, frida (device listing), pwntools, mitmproxy, tshark, binwalk/unblob, sigrok, wabt, wakaru, ilspycmd, apktool. Anything needing hardware or an artifact this box lacks is recorded with its exact precondition. | **IN PROGRESS (most families proven live)** — Ghidra native overview 20s + function dossier 20.5s; Android static 6/6 on a real APK fixture (package/classes/class/method decompile/refs/resources); CDP web family 5/5 (targets, page, session, screenshot with real artifact sha256+viewport, network capture inspection of a real mitmproxy recording, visual regression `identical` vs `different`); binaries: objdump, rizin 8.5s, binary-meta (auto/go/rust/strings), pwntools-backed ELF layout, gdb, crash from a REAL ELF core; formats: sqlite (schema+rows), WASM (WABT 1.0.42), EVM interface, firmware regions (binwalk 3.1.0 — the version REA verifies with), dart/flutter detection; Android live: 11 device tools on a physical phone. Remaining preconditions: frida *instrument* (device is not rooted), sigrok (no logic-analyzer capture), Hopper/IDA/JEB (paid/closed), pwndbg (optional core mapping). |
| **B4** Durability + fresh-machine install | Survives reboot, `hermes update`, and installs itself on a new box | `tools/survive.sh` re-applies the plugin copy + MCP entry after a simulated loss; `install-ultimate.sh` covers plugin enable + engine bootstrap; byte-identity repo↔install; inventory regeneration; TPS counters; stdlib-only (no Hermes core import). | **DONE (proven by simulated loss)** — deleted `~/.hermes/plugins/hermes-rea` AND removed the `mcp_servers.rea` entry from `config.yaml` (17 lines, backup kept), then ran `tools/survive.sh`: plugin files restored from the repo (byte-identical), engines 21/21, and the MCP entry re-registered (wrapper command + env) with `hermes mcp list` showing `rea` enabled again. `rea-bootstrap.sh --check` exits 0 and now fails loudly if the entry is absent; `install-ultimate.sh` calls it with `HERMES_REA_ENGINES=auto` so a fresh machine gets the pinned tool versions (the installer installs WABT 1.0.42, binwalk 3.1.0 and native Wakaru 1.14.0 itself). Plugin remains stdlib-only. |
| **B5** Docs + tracking | One authoritative, verified record | This file, `docs/VERSION_MATRIX.md`, `docs/COMPLETENESS_AND_MAX_UTILIZATION_CHECKLIST.md`, the engine table and the installed skill all agree with the live surface. | **DONE** — findings register F1–F32 with the fix for each; `docs/VERSION_MATRIX.md` records the three version-pinned tools (WABT 1.0.42, binwalk 3.1.0, native Wakaru 1.14.0) **with the reason each pin exists**; the completeness checklist carries the 18-plugin sections; `README.md`/install docs point at `tools/rea-bootstrap.sh` as the single entry point; the upstream `reverse-engineer-anything` skill is installed. Every number in this file came from a command whose output is quoted (or from the matrix artifact in `state/rea/artifacts/matrix`). |
| **B6** Adversarial sweep | Re-audit every claim with live probes | Hunt the defect classes this work already produced: silent no-op / ignored argument (F12 class), stale command mapping, unbounded growth (evidence store, manifest cache), concurrency and timeout/kill semantics, output caps, garbage input, retention/prune. | **DONE** — 6 concurrent calls: 6/6 OK in 1.8s (no shared-state race); garbage input on the generic runner: empty command → *"command is required"*, unbalanced quote → structured shlex error, missing required enum → the valid values listed (no tracebacks anywhere); unbounded-growth checks: evidence store 140 files with a working 30-day prune (`removed: 0, retention_days: 30` for fresh data), manifest cache 153 commands aged 7,784s against an 86,400s TTL with the age surfaced so staleness is visible; output cap 200,000 bytes with spill-to-file; retention/export/import round-trip exercised. The sweep also caught a self-inflicted regression (F32) that the smoke suite flagged and that is now fixed. |

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
