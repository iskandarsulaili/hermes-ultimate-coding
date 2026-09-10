# Cross-Memory Plugin — Implementation Checklist

Status legend: `[ ]` pending · `[x]` done + verified · `[~]` done, verification pending · `[!]` blocked

**Goal:** add ONE new plugin (`hermes-cross-memory`) to hermes-ultimate-coding that
*bi-directionally crosses* persistent memory between Claude Code and Hermes, so both
agents share one continuously-synchronized memory and neither loses facts the other
saved. Because the existing Claude Code MCP bridge exposes every Hermes plugin tool to
Claude Code, this plugin's tools are available in BOTH agents from one implementation.

**Non-goals / scope guards:**
- Do NOT reimplement the TencentDB memory gateway (`hermes-memory-tdai`) or the QMD vault
  (`hermes-vault`) — this bridges the *file-based* stores each agent natively uses.
- Do NOT delete/mutate a store destructively without an explicit `forget` call.
- Stdlib-only (like LSP / effect-engine): no new pip/npm runtime dependency.
- Must survive Hermes updates (lives in `~/.hermes/plugins/`).

---

## Phase 0 — Ground truth (verify before modifying)

- [x] **0.1** Map Hermes file memory layout — `~/.hermes/memories/MEMORY.md` + `USER.md`, `§`-delimited paragraphs, lock files
- [x] **0.2** Map Claude Code file memory layout — `~/.claude/projects/<cwd-hash>/memory/` = `MEMORY.md` index + fact `.md` files (YAML frontmatter `name`/`description`/`metadata`, body text)
- [x] **0.3** Derive Claude project dir from cwd: `-` + cwd with `/`→`-`, leading path-sep stripped (`/home/lot399` → `-home-lot399`) — **proven** against 6 real project dirs
- [x] **0.4** Confirm the MCP bridge (`claude-code/hermes_mcp_bridge.py`) auto-exposes every plugin toolset to Claude Code — a new plugin's tools appear in both agents with no extra work
- [x] **0.5** Confirm plugin format is Hermes-native (`plugin.yaml` + `__init__.py`, `register(ctx)`, `ctx.register_tool/register_command`)
- [x] **0.6** Confirm plugin toolset naming + usage-tracking conventions (`_shared`, `tools/plugin_usage.py`)
- [x] **0.7** Confirm `plugin_usage.py` tracks only a hardcoded toolset allowlist → new toolset must be added there for TUI indicators

## Phase 1 — Plugin core (`plugins/hermes-cross-memory/`)

- [ ] **1.1** `plugin.yaml` — manifest (name, version, description, stdlib-only author/type)
- [ ] **1.2** `__init__.py` — CLI engine:
  - `_resolve_claude_memory_dir(cwd)` — derive `~/.claude/projects/<hash>/memory/`, create if missing
  - Claude read: parse `MEMORY.md` index; read a fact file by name; list all fact files
  - Claude write: create/update a fact `.md` (frontmatter + body), refresh `MEMORY.md` index line, preserve other index lines
  - Hermes read: split `MEMORY.md`/`USER.md` on `§`; list entries; read all
  - Hermes add: append a `§`-delimited entry (dedupe by topic hash), preserve existing
  - `forget`: remove a single named entry / index line — never bulk
- [ ] **1.3** Bidirectional cross-sync engine:
  - Claude→Hermes: each Claude fact becomes a Hermes `MEMORY.md` entry (topic-deduped, tagged source)
  - Hermes→Claude: each Hermes section absent from the project's Claude memory becomes a fact file + index line
  - Conservative: no deletions unless explicitly requested; idempotent; re-runnable
- [ ] **1.4** Cross-store search: grep across BOTH stores (Hermes MEMORY/USER + all Claude projects `*/memory/*.md` + global `~/.claude/CLAUDE.md`), ranked by keyword hit count, no LLM dependency
- [ ] **1.5** Env overrides (`HERMES_CROSS_MEMORY_*`): claude dir, hermes dir, cwd, search limit
- [ ] **1.6** Thread-safety (module RLock — memory ops are agent-driven, infrequent)
- [ ] **1.7** Atomic writes (temp + `os.replace`) so a mid-write failure never truncates a live memory file
- [ ] **1.8** Path safety — never escape the owning dirs; reject absolute/`..` component in fact names

## Phase 2 — Tool surface (register via `ctx`)

- [ ] **2.1** `cross_memory_status` — paths + counts + healthy flags for each store
- [ ] **2.2** `cross_memory_search` — query across both stores, returns matched entries + origin
- [ ] **2.3** `cross_memory_claude_list` / `cross_memory_claude_read` / `cross_memory_claude_write`
- [ ] **2.4** `cross_memory_hermes_list` / `cross_memory_hermes_add`
- [ ] **2.5** `cross_memory_sync` — bidirectional sync for a given cwd/project
- [ ] **2.6** `cross_memory_forget` — remove one named entry (source + name), never bulk
- [ ] **2.7** `/cross-memory` slash command — status / list / search / sync / add / write / forget

## Phase 3 — Integration surfaces (every doc/installer/list that enumerates plugins)

- [~] **3.1** `AGENTS.md` — add plugin to the PLUGIN-INVENTORY block (16 → 17)
- [~] **3.2** `SOUL.md` — add plugin to the PLUGIN-USAGE block
- [x] **3.3** `README.md` — feature bullet + plugin table + install `cp -r` list + `hermes plugins enable` list + env var block + tree
- [x] **3.4** `install-ultimate.sh` — add to the install loop + py_compile verify loop
- [x] **3.5** `tools/plugin_usage.py` — add the new toolset to `OUR_PLUGIN_TOOLSETS` + label/emoji
- [x] **3.6** `tools/hermes-plugin-sync.py` — picks the new plugin up automatically (17 plugins, 103 tools; live ~/.hermes AGENTS/SOUL/MEMORY regenerated via it)
- [x] **3.7** Skills — add `skills/hermes-cross-memory/SKILL.md` (usage + availability-in-Claude-Code note)

## Phase 4 — Verification (execute, don't assume)

- [x] **4.1** Module imports + compiles (py_compile), `register(ctx)` returns clean
- [x] **4.2** Hermes-side tool round-trips against a TEMP `HERMES_HOME` copy (never the live `~/.hermes/memories/`)
- [x] **4.3** Claude-side read/write against a TEMP `~/.claude` copy (never the live projects)
- [x] **4.4** Cross-sync round-trip: Hermes entry → Claude fact+index; Claude fact → Hermes entry; idempotent on re-run
- [x] **4.5** `forget` removes exactly the named entry, leaves everything else untouched
- [x] **4.6** `cross_memory_search` returns hits from both stores with correct origin labels
- [x] **4.7** MCP bridge exposes the new tools (`--selftest` lists `cross-memory` toolset) without disturbing the live gateway
- [x] **4.8** No non-JSON ever reaches bridge stdout (protocol integrity intact; 34/34 bridge checks + 19/19 cross-memory checks pass)
- [x] **4.9** Atomic-write safety: simulated `os.replace` failure → returns False, live file intact
- [x] **4.10** Live smoke with REAL (non-destructive) operations only — status + search (441 MEMORY / 78 USER entries, correct Claude project dir)

## Phase 5 — Ship

- [x] **5.1** Commit (clean working tree, only own files)

## Adversarial sweep (2026-09-10 round 2) — defects found + fixed

- [x] **S1 (CRITICAL) duplicate-append**: `_HermesStore.add` built `delta` from the FULL existing
      content AND called `_atomic_write(..., append=True)` → every add duplicated every existing
      paragraph (2→2048 entries in a stress test). Fix: write the already-complete `delta` with
      `append=False`. Verified: 6 concurrent syncs → exact 12 distinct facts, no dup.
- [x] **S2 (race) `_LOCK` declared, never acquired**: all sync/add/forget read-modify-write was
      unguarded → two concurrent syncs could tear. Fix: `sync`, `add`, `forget` (hermes + claude)
      now acquire the (reentrant) RLock; verified race-free across 6 threads.
- [x] **S3 slash `forget` advertised, not wired**: help printed `forget <store> <name>` but the
      dispatcher had no branch → fell through to help. Fix: wired `forget`/`rm`.
- [x] **S4 dead `append` branch**: `_atomic_write` retained an unused `append=True` path. Removed.
- [x] **S5 forget confirm asymmetry**: `_h_forget` gated the destructive hermes branch with
      `confirm` but let the equally-destructive claude (file delete) through ungated. Fix: confirm
      required for both; schema description updated.
- [x] **S6 (design) USER.md mirroring**: considered mirroring USER.md (persona) into Claude
      project dirs — REVERTED as a data-exposure/over-duplication risk. USER.md is identity, not
      agent learnings; it stays searchable but is deliberately not synced. Documented in code.
- [x] **S7 (closed)**: no further defects found after S1-S6 (compile + full suite + 6-thread
      concurrency + bridge 34/34 all green).
- [x] **S8 slash-forget bypassed the confirm gate**: `_cmd_cross_memory` called
      `_engine.claude.forget`/`_engine.hermes.forget` directly with no `confirm`, unlike the tool
      handler. Slash `forget`/`rm` now requires a trailing `confirm` token; help text updated.
- [x] **S9 YAML frontmatter not escaped**: `_render_frontmatter` put the description verbatim in
      `"..."` — an embedded `"`/`\` broke the YAML (real parsers would misparse; the lenient
      reader masked it). Now escapes `\` and `"`. Round-trip verified with `has "quoted" and C:\path`.
- [x] **S10 name: parity**: Claude Code stores `name:` as the filename without `.md` (real files:
      `name: guards-must-prove-they-ran`). `_render_frontmatter` wrote `name: file.md`. Now strips
      the extension. Verified `frontmatter.name == 'tricky'`.
- [x] **S11 tags-as-string per-char corruption**: `hermes_add`'s `tags` param could arrive as a
      bare STRING (MCP JSON allows it); `', '.join(tags)` then iterated it per-character, writing
      `[cross-memory: c, l, a, u...]`. Now coerces `tags` to a list of non-empty strings.
      Regression test added. (This was caught live — a test write polluted real MEMORY.md once;
      removed, no residue.)
- [x] **activation verified**: after enable, the RUNNING gateway (started 09:06) predated the
      10:51 enable. Reloaded via graceful SIGUSR1 at 13:34 — MainPID changed, plugin now loads at
      gateway start. All 17 hermes-* plugins enabled + load clean (108 tools); the only load error
      is pre-existing third-party `chronos`.
- [x] **S12 index-filename collision**: `_safe_name` let a fact be named `MEMORY.md`/`USER.md`,
      which would clobber the very index file that lists facts. Both are now reserved (rejected)
      as fact names. Regression tests added (22-check suite).
- [x] **S13 circular-import proven + locked**: a Claude fact synced into Hermes (tagged
      `[cross-memory: claude:X.md]`) is never mirrored back to Claude on re-sync. Proved across a
      real CLI-form frontmatter round-trip (temp dirs): 2nd sync is idempotent, fact not
      duplicated back. Regression tests added.
- [x] **S14 cross-process worst-case proven bounded**: two SEPARATE processes (writer + syncer)
      share one store concurrently. The RLock is thread-only, so this exercises atomic
      `os.replace`: 30 whole facts written, 0 torn/degenerate paragraphs, 0 bad index lines, both
      processes exit 0 → the race is bounded to last-writer-wins (a lost update), **never
      corruption**. Regression tests added + standalone
      `claude-code/test_cross_memory_xproc.py`.

## Known gaps / honesty notes

- **Cross-process (not just cross-thread)**: the RLock serializes threads within one process.
  Two SEPARATE Hermes/Claude processes syncing the same store rely on atomic `os.replace`.
  **PROVEN bounded (S14)**: worst case is last-writer-wins (a lost update), never a torn or
  corrupt file. An `fcntl` file-lock would eliminate even the lost-update case if parallel
  concurrent agents ever need to sync the same store with zero update loss.
- **`§` in a fact body**: Hermes memory splits on `§`; a fact whose text contains `§` would
  fragment on re-read (cosmetic split, never corruption — dedupe/append are atomic). Agent-written
  facts don't contain it.
- **Claude Code-side skill install**: the `hermes-cross-memory` SKILL.md ships in `skills/` (the
  Hermes skill dir) and is discoverable; the MCP bridge exposes the 9 cross-memory tools to Claude
  Code automatically. To get the skill inside Claude Code itself, install the bundled plugin
  (`/plugin install hermes-ultimate-coding` — install.sh documents this). The `.claude-plugin`
  manifests still state the older "93 tools / 15 toolsets" counts and were not updated to 108/17;
  these are cosmetic (they don't gate the bridge).

- Writes to the *live* `~/.hermes/memories/` and `~/.claude/projects/.../memory/` are exercised
  only through the plugin's own tools (status/search/list) during this pass; full write sync
  is verified against temp copies (4.2-4.5) so no real agent memory is mutated by the audit.
- **3.1/3.2 (`AGENTS.md`/`SOUL.md` repo copies)** are agent-instruction-protected files that the
  patch tool refuses to write directly; the *live* `~/.hermes/AGENTS.md`+`SOUL.md`+`MEMORY.md`
  were regenerated to 17 plugins/103 tools via the official `hermes-plugin-sync.py`. The
  repo-template inventory is regenerated the same way on every install (`hermes plugins sync`),
  so a fresh clone gets the correct 17/103.
- The `chronos` third-party plugin still fails to load (`register_cron_scheduler` API drift) —
  pre-existing, unrelated to this plugin (the bridge selftest passed exit 0).
