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

## Definition of done
1. Repo ↔ install byte-identical for `plugins/hermes-rea`.
2. Every engine either ready or reporting an honest, actionable remediation (no silent empties).
3. Android, artifact, sqlite, process and manifest-executor paths exercised live with stored evidence.
4. Tool calls surface in the TUI (TPS prefix wired).
5. `hermes-plugin-sync.py --targets soul,agents,memory` regenerated the plugin inventory.
6. Checklist reflects reality at push time.
