<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/iskandarsulaili/hermes-ultimate-coding/main/assets/logo-dark.svg">
    <img src="https://raw.githubusercontent.com/iskandarsulaili/hermes-ultimate-coding/main/assets/logo-light.svg" alt="hermes-ultimate-coding" width="480" style="max-width: 100%;">
  </picture>
</p>

<p align="center">
  <b>Ultimate vibe coding plugins for Hermes AI agent.</b>
</p>

<p align="center">
  Effect-ts functional architecture • LSP code intelligence • Semble semantic code search • Graphify knowledge graph • t/s status bar • Plugin usage indicators • MoA planning trigger • Four-layer agent memory • DeepSeek Harness integration • Anchored Standard tool trajectory • Claude Code support via MCP • 16 plugins, 93 tools • Stdlib-only core
</p>

<p align="center">
  <a href="#-features">Features</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-the-vibe-coding-stack">The Stack</a> •
  <a href="#-comparison">Comparison</a>
</p>

<p align="center">
  <a href="https://github.com/iskandarsulaili/hermes-ultimate-coding"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-hermes--ultimate--coding-2ea44f?style=flat-square&logo=github"></a>
  <a href="https://github.com/iskandarsulaili/hermes-ultimate-coding/blob/main/LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"></a>
  <a href="#"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square&logo=python"></a>
  <a href="#"><img alt="LSP+EE: stdlib only" src="https://img.shields.io/badge/LSP%2BEE-stdlib%20only-success?style=flat-square"></a>
  <a href="#"><img alt="Semble+Graphify: optional pip" src="https://img.shields.io/badge/Semble%2BGraphify-optional%20pip-lightgrey?style=flat-square"></a>
  <a href="https://github.com/sponsors/iskandarsulaili"><img alt="Sponsor" src="https://img.shields.io/badge/sponsor-30363D?style=flat-square&logo=GitHub-Sponsors&logoColor=EA4AAA"></a>
</p>

---

**hermes-ultimate-coding** is the ultimate vibe coding stack for [Hermes AI agent](https://hermes-agent.nousresearch.com). Sixteen plugins, 93 tools. Everything you need to turn Hermes into a self-correcting, codebase-aware AI coding agent — and, via the [MCP bridge](#-claude-code-support--the-mcp-bridge), the same 93 tools inside Claude Code:

**1. Effect-ts functional architecture** — Typed errors, DI container with cycle detection, structured concurrency via Scope + Fiber. Every operation is composable, typed, and error-tracked. No silent failures.

**2. LSP code intelligence** — Real-time diagnostics after every edit, completions, hover, go-to-definition, auto-fix. The agent self-corrects before shipping broken code. 49 languages. Cross-repo fallback. Auto-installs missing npm language servers on first use.

**3. Semble semantic code search** — Hybrid BM25 + semantic embeddings. Find code by what it *does*, not just by what characters it contains. ~98% fewer tokens than grep+read.

**4. Graphify knowledge graph** — Dependency graphs, call chains, subsystem detection, shortest paths between concepts. Understand how everything connects. Auto-builds on first use; auto-adds `graphify-out/` to the repo's `.gitignore`.

**5. t/s status bar** — Real-time tokens-per-second in the Hermes TUI status bar. See generation speed alongside model, context %, and elapsed time. Zero deps (stdlib only).

**6. Plugin usage indicators** — TUI status bar shows live 🔧⚡🕸️🔍 indicators for plugin tool usage, adaptively from emoji-only to full names+counts depending on terminal width. Zero LLM cost.

**7. Four-layer agent memory** — hermes-memory-tdai wraps the TencentDB Agent Memory gateway (L0 conversation store → L1 atomic memories → L2 scenario blocks → L3 core persona). L0 capture/search works with zero LLM; L1-L3 semantic extraction uses your gateway LLM. The gateway auto-clones, auto-installs (npm), and auto-starts on first use.

The LSP and Effect Engine plugins are **pure Python, zero external dependencies** (stdlib only). Semble and Graphify require optional pip packages (`pip install semble`, `pip install graphifyy`). All plugins install in seconds, **auto-setup their own dependencies on first use** (pip/npm installs, git clones, gateway startup — non-interactive), and survive Hermes updates because they live in `~/.hermes/plugins/`, not in Hermes's core. All timeouts, limits, and cache sizes are configurable via environment variables — no hardcoded settings.

## ✨ Features

### Effect-ts Architecture — in Python

| What OpenCode has | What hermes-ultimate-coding provides |
|-------------------|------------------------------|
| Effect-ts `Effect<A, E, R>` | `Effect[T, E]` — compose, map, flatMap, catch, retry, withTimeout |
| Effect-ts `Schema.TaggedError` | `TypedError` — tagged errors with `_tag` discriminator, JSON round-trip |
| Effect-ts `Layer` (DI) | `ServiceContainer` — register services with deps, resolve graphs, detect cycles at register time |
| Effect-ts `Scope` + `Fiber` | `Scope` + `Fiber` — async `fork`, `join`, `interrupt`, auto-cancel on scope exit |
| Effect-ts `Logger` | Python `logging` — all configurable via env |
| TypeScript runtime | Python 3.11+ — no transpilation, no bundling |

**Zero external dependencies** — stdlib only. No pip install needed.

4 Hermes tools:

| Tool | What it does |
|------|-------------|
| `effect_run` | Execute a chain of operations as a typed effect. Each step validated, errors tracked by type, stops on first typed failure. |
| `effect_scope` | Fork concurrent fibers, join results, cancel, or list running fibers. Auto-cancels on scope exit. |
| `effect_service` | Register services with explicit dependencies, resolve them, or inspect the graph. Cycle detection at register time. |
| `effect_inspect` | Inspect the service graph, tool registry, and known error types. |

### LSP Code Intelligence — 49 Languages

| Tool | What it does |
|------|-------------|
| `lsp_verify` | Opens file, gets diagnostics, returns pass/fail. Agent self-corrects before shipping. |
| `lsp_completions` | Method names, imports, documentation |
| `lsp_hover` | Type signatures, documentation for any symbol |
| `lsp_definition` | File + line number, with cross-repo fallback |
| `lsp_auto_fix` | Quick-fix suggestions (like the IDE lightbulb) |
| `lsp_servers` | List available servers and running clients |
| `lsp_diagnostics` | Get diagnostics for a specific file |

**Cross-repo resolution** — when `goto_definition` can't find a symbol in the current repo, it automatically queries all other running LSP servers of the same language. Self-adapting: discovers related repos organically as you open files. No config needed.

**Zero external dependencies** — stdlib only. No pip install needed.

7 Hermes tools + `/lsp` slash command.

### Semble Semantic Code Search

Search your whole codebase using natural language or symbol names. Complements grep+read:

| Search type | Tool | Example |
|-------------|------|---------|
| Concept/semantic | `semble_search` | "how is authentication handled?" |
| Symbol lookup | `semble_search` | "where is UserService.createUser?" |
| Find related code | `semble_find_related` | "all implementations of IRepository" |
| Exact pattern | `grep` (terminal) | "grep -rn 'TODO' src/" |
| Full context | `read_file` | After Semble finds the right file |

**Requires:** `pip install semble` (optional — plugin loads without it, tools return helpful error)

5 Hermes tools + `/semble` slash command.

### Graphify Knowledge Graph

Structural code understanding via dependency graphs. Complements LSP (per-file depth) and Semble (semantic search) with structural relationships.

| Query type | Tool | Example |
|-------------|------|---------|
| Concept relationships | `graphify_query` | "how does auth connect to the database?" |
| Shortest path | `graphify_path` | "UserService → DatabasePool" |
| Explain a symbol | `graphify_explain` | "what does RateLimiter connect to?" |
| Most connected nodes | `graphify_god_nodes` | "what are the core abstractions?" |
| Graph statistics | `graphify_stats` | node/edge/community counts |
| Find nodes | `graphify_find` | "find LSPClient in the graph" |
| Subsystem contents | `graphify_community` | "what's in community 0?" |

**Requires:** `pip install graphifyy` (optional — plugin loads without it, tools return helpful error)
**Auto-.gitignore:** On successful build, automatically adds `graphify-out/` to the target repo's `.gitignore`.

7 Hermes tools + `/graphify` slash command.

### t/s Status Bar — Real-time Generation Speed

See tokens-per-second in the Hermes TUI status bar, right alongside model name, context %, and elapsed time:

```
⚕ Qwen3.6-27B-UD-Q4_K_XL │ 83K/262K │ [█░░░░░░░░░] 8% │ 12.3 t/s │ 6m │ ⏲ 3m 27s │ ✓ 21s
```

| Feature | What it does |
|---------|-------------|
| `post_api_request` hook | Captures completion tokens and API duration from every LLM call |
| Status bar injection | Monkey-patches HermesCLI to display t/s in wide format (≥76 cols) |
| Thread-safe storage | Latest t/s value stored under a lock, read on every status bar refresh |

**Zero external dependencies** — stdlib only. No pip install needed.

1 Hermes hook (no tools or commands).

### 🔧 Plugin Usage Indicators — Adaptive Status Bar

See which plugin toolsets are being used live in the Hermes TUI status bar, displayed adaptively based on terminal width:

```
# Narrow (<52 cols): active plugin emoji only
⚕ deepseek ... 🔧⚡

# Medium (52-75 cols): emoji + count for active plugins
⚕ deepseek · 55% · 🔧3 ⚡1 · 7m

# Full (76+ cols): emoji + name + count (active bright, zero dim)
⚕ deepseek │ ctx │ [░░] 55% │ 🔧 LSP:3 │ ⚡ Effect:1 │ 🕸️ Graphify:0 │ 🔍 Semble:0 │ 7m
```

| Plugin | Emoji | Meaning |
|--------|-------|---------|
| 🔧 LSP | `🔧` | Code diagnostics & intelligence |
| ⚡ Effect | `⚡` | Typed effect chains |
| 🕸️ Graphify | `🕸️` | Code knowledge graph |
| 🔍 Semble | `🔍` | Semantic code search |

**Zero LLM cost** — uses lightweight in-memory counters read on every status bar refresh.

### What OpenCode Doesn't Have

| Feature | hermes-ultimate-coding | OpenCode |
|---------|------------------------|----------|
| **Idle client eviction** | ✓ — clients auto-evicted after TTL | ✗ — clients live forever |
| **Server availability cache** | ✓ — caches binary checks for 60s | ✗ — checks every time |
| **Project root cache** | ✓ — caches root discovery | ✗ — re-discovers every file |
| **Thread safety** | ✓ — every shared state has a lock | ✗ — single-threaded only |
| **Timeouts on every I/O** | ✓ — reads, writes, stops all have configurable timeouts | Partial |
| **Environment variable configuration** | ✓ — 70+ env vars for all timeouts/limits | ✗ — hardcoded |
| **Cross-repo LSP fallback** | ✓ — queries other repos on miss | ✗ — single workspace only |
| **Survives agent updates** | ✓ — lives in user plugin dir | ✗ — bundled in monorepo |
| **Agent-agnostic** | ✓ — works with Hermes, OpenCode, Cline, any plugin system | ✗ — OpenCode only |
| **Auto-.gitignore on graph build** | ✓ — appends `graphify-out/` to repo's `.gitignore` | ✗ — no graph at all |
| **JIT auto-build** | ✓ — graphify builds on first use if missing | ✗ — no graph at all |
| **Four-layer agent memory** | ✓ — L0-L3 via TencentDB gateway | ✗ |
| **Auto-setup on fresh machines** | ✓ — all 16 plugins self-bootstrap deps | ✗ |

## ⚡ Quick Start

### Prerequisites

- **Hermes Agent** — plugins auto-discover from `~/.hermes/plugins/`
- **Python 3.11+** — LSP and EE plugins need only stdlib; Semble and Graphify need optional pip packages

### Install

```bash
git clone https://github.com/iskandarsulaili/hermes-ultimate-coding.git /tmp/hermes-ultimate-coding

# Install all 16 plugins
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-lsp ~/.hermes/plugins/hermes-lsp
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-effect-engine ~/.hermes/plugins/hermes-effect-engine
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-semble ~/.hermes/plugins/hermes-semble
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-graphify ~/.hermes/plugins/hermes-graphify
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-codegraph ~/.hermes/plugins/hermes-codegraph
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-codegraph-context ~/.hermes/plugins/hermes-codegraph-context
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-orchestra ~/.hermes/plugins/hermes-orchestra
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-searxng ~/.hermes/plugins/hermes-searxng
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-cloakbrowser ~/.hermes/plugins/hermes-cloakbrowser
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-vault ~/.hermes/plugins/hermes-vault
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-agents ~/.hermes/plugins/hermes-agents
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-tps ~/.hermes/plugins/hermes-tps
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-moa-trigger ~/.hermes/plugins/hermes-moa-trigger
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-memory-tdai ~/.hermes/plugins/hermes-memory-tdai
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-dsh ~/.hermes/plugins/hermes-dsh
cp -r /tmp/hermes-ultimate-coding/plugins/hermes-anchored ~/.hermes/plugins/hermes-anchored
cp -r /tmp/hermes-ultimate-coding/plugins/_shared ~/.hermes/plugins/_shared

# Clean up
rm -rf /tmp/hermes-ultimate-coding
```

> **Important:** Each plugin must be a direct subdirectory of `~/.hermes/plugins/`. Cloning the whole repo into `~/.hermes/plugins/hermes-ultimate-coding/` will NOT work.

> **Auto-setup:** Every plugin self-bootstraps on first use — pip/npm dependencies auto-install (non-interactive, `ask=False`), upstream repos auto-clone (agents, memory-tdai), and services auto-start (SearXNG, TencentDB gateway, CloakBrowser). No manual dependency steps needed.

### Enable Plugins

```bash
hermes plugins enable hermes-lsp
hermes plugins enable hermes-effect-engine
hermes plugins enable hermes-semble
hermes plugins enable hermes-graphify
hermes plugins enable hermes-codegraph
hermes plugins enable hermes-codegraph-context
hermes plugins enable hermes-orchestra
hermes plugins enable hermes-searxng
hermes plugins enable hermes-cloakbrowser
hermes plugins enable hermes-vault
hermes plugins enable hermes-agents
hermes plugins enable hermes-tps
hermes plugins enable hermes-moa-trigger
hermes plugins enable hermes-memory-tdai
hermes plugins enable hermes-dsh
hermes plugins enable hermes-anchored --allow-tool-override
```

### Restart & Verify

```bash
# In Hermes:
/lsp servers
/effect
/semble status
/graphify status
/tdai status
/anchored status
```

### 🔄 Auto-Sync SOUL.md + AGENTS.md (plugin inventory pipeline)

Keep the agent's plugin-awareness in sync with the installed pack automatically:

```bash
# Regenerate the plugin-inventory sections of SOUL.md + AGENTS.md from the
# ACTUAL installed plugins (scans plugin.yaml + register_tool for the truth).
python3 tools/hermes-plugin-sync.py            # write live files
python3 tools/hermes-plugin-sync.py --dry-run  # preview without writing
python3 tools/hermes-plugin-sync.py --targets memory  # (opt-in; MEMORY.md is near its token budget)

# Automated version — regenerates + commits any drift to the repo.
# Install on a cron:  17 6 * * * tools/hermes-plugin-sync-cron.sh
```

Every plugin's `plugin.yaml` description and registered tool names/counts are
derived deterministically — the docs can never drift from the installed pack
(e.g. it caught SOUL.md listing 14 plugins while 16 were installed). Sections
are delimited by markers so only the plugin inventory is rewritten; the rest
of each file (persona, workflow priority, mandatory rules) is untouched.

## 🤖 Claude Code Support — the MCP bridge

Every plugin in this pack is available inside **[Claude Code](https://claude.com/claude-code)**,
not just Hermes. `claude-code/hermes_mcp_bridge.py` serves the *live* Hermes
plugin registry over the Model Context Protocol.

It is a bridge, not a port. It boots the real `hermes_cli.plugins.PluginManager`
against your actual `$HERMES_HOME`, lets every plugin register through the real
`PluginContext`, and serves the resulting `tools.registry` on stdio. So:

- **One implementation of every tool.** Fix a plugin, and the fix is live in
  Claude Code with no porting step.
- **No mocks, no stubs, no reimplementation.** Schemas, availability gates
  (`check_fn`), async handlers, result contracts and error formats are the
  plugins' own.
- **New plugins appear automatically.** Nothing in the bridge enumerates tools
  by name.

### Install

```bash
git clone https://github.com/iskandarsulaili/hermes-ultimate-coding.git
cd hermes-ultimate-coding
./claude-code/install.sh          # preflights, self-tests, registers the server
```

Or as a plugin, which also installs the bundled skills:

```
/plugin marketplace add iskandarsulaili/hermes-ultimate-coding
/plugin install hermes-ultimate-coding
```

Or by hand:

```bash
claude mcp add hermes --scope user -- /path/to/hermes-ultimate-coding/claude-code/launch.sh
```

Hermes must be installed — the bridge serves *its* registry. Verify with `/mcp`
inside Claude Code; `hermes` should be connected.

### What you get

Measured on a full install (`launch.sh --selftest`): **93 tools across 15
toolsets**, out of 110 registered — the other 17 are correctly hidden by their
own `check_fn` because their dependencies are absent.

| Toolset | n | Toolset | n |
|---|---|---|---|
| `orchestra_*` | 12 | `graphify_*` | 7 |
| `tdai_*` | 9 | `lsp_*` | 7 |
| `codegraph_*` | 8 | `cloakbrowser_*` | 6 |
| `cgc_*` | 8 | `vault_*` | 6 |
| `agents_*` | 7 | `semble_*` | 5 |
| `dsh_*` | 7 | `effect_*` | 4 |
| `searxng_*` | 4 | `anchored_*` | 2 |
| `planning_trigger` | 1 | | |

### Configuration

| Variable | Effect |
|---|---|
| `HERMES_HOME` | Hermes home (default `~/.hermes`) |
| `HERMES_AGENT_DIR` | hermes-agent checkout (default `$HERMES_HOME/hermes-agent`) |
| `HERMES_MCP_PYTHON` | Force a specific interpreter |
| `HERMES_MCP_TOOLSETS` | Allowlist, e.g. `semble,lsp` |
| `HERMES_MCP_EXCLUDE_TOOLS` | Denylist of tool names |
| `HERMES_MCP_INCLUDE_BUILTINS` | `1` to also expose Hermes built-ins (off by default — Claude Code already has Read/Write/Bash) |
| `HERMES_MCP_CALL_TIMEOUT` | Per-call budget in seconds (default 300) |
| `HERMES_MCP_DEBUG` | `1` for verbose stderr tracing |

### Design notes

**stdout is the protocol wire.** Hermes plugins log verbosely and some libraries
print straight to file descriptor 1; one stray byte desynchronizes JSON-RPC and
Claude Code drops the server. At start-up the bridge duplicates the real stdout
to a private descriptor and points fd 1 at stderr, so everything the process
*and its children* write to "stdout" lands harmlessly in the MCP log. Only the
framer writes to the real one. This is asserted by the test suite.

**Child-process reaping.** Plugins spawn helpers (`graphify extract`, language
servers, browsers) whose time budget is enforced by the parent that started
them. If the bridge exited while one was running, the child was reparented to
init and that budget stopped being enforced — it ran unbounded. The bridge now
owns its process group and reaps exactly its own descendants on exit.

**No session hooks.** The bridge serves the tool registry rather than running
Hermes' session lifecycle, so `session_start` and file-change hooks do not fire.
Semble indexes on first use and Graphify builds on first query, so this is
mostly invisible — but call `semble_reindex` after your own edits, and pass an
explicit `repo`/`project_dir` when the cwd is not the project root.

### Tests

```bash
"$HERMES_HOME/hermes-agent/venv/bin/python" claude-code/test_bridge.py
```

34 checks against a live bridge subprocess: handshake and version negotiation,
schema validity for every tool, real tool round-trips, error paths, malformed
frames, toolset scoping, the missing-Hermes failure mode, and stdout integrity.

## 🧠 MoA Preset — max-think-def-output

A ready-to-merge [Mixture of Agents](https://hermes-agent.nousresearch.com/docs) preset for Hermes: **one advisor thinking at max reasoning depth, an aggregator writing at provider-default reasoning** — "think deep, execute light."

```
moa-presets/max-think-def-output.yaml
```

**What it does**

- A single reference advisor runs at `reasoning_effort: max` — the deepest thinking tier.
- The aggregator (the acting model that writes the user-visible answer) runs at the backend's default reasoning — it is NOT pinned, so it follows your current `/reasoning` level when one is set.
- With `fanout: user_turn`, the max-reasoning advisor runs once per user turn on the raw request; the aggregator then does the whole tool loop. Mid-loop max reasoning is provided by the **hermes-moa-trigger plugin** (fires on 📋 todo plan writes + the six planning moments) instead of paying per-iteration cost.

**Cadence — max reasoning at start AND mid-loop**

| fanout | Advisor runs | Max thinking sees live state? | Cost |
|--------|-------------|-------------------------------|------|
| `user_turn` | once per user turn | no — original request only | cheapest |
| `every_n:2` | iteration 1, then every 2nd | yes, within ~1 step | medium |
| `per_iteration` | every tool iteration | yes, immediately | most expensive |

This preset uses `user_turn` (cheapest base cadence). Mid-loop max passes fire via hermes-moa-trigger's automatic todo-plan detection + manual `planning_trigger` tool — max reasoning exactly at planning moments, never per iteration. Subagents inherit the preset automatically.

**Install**

> ⚠️ `install-ultimate.sh` only copies `plugins/` — this preset is **not** auto-installed. The `custom:combo/deepseek-v4-flash` provider/model are machine-specific — use the canonical `custom:<entry-name>` form (bare `custom` breaks resolution when the active default provider changes).

**`default` preset — local-first fallback** (`moa-presets/default.yaml`)

The Hermes factory `default` preset ships OpenRouter/OpenAI-Codex slots. Without an OpenRouter key, every reference advisor 401s → "advisory unavailable / references are down" and MoA silently loses the max pass. `moa-presets/default.yaml` aliases `default` to the same local-gateway slots so a bare `/model moa` or `moa:default` always works.

**`/moa-flush` — kill the stale reference**

The built-in MoA reference runs once per user turn (`fanout: user_turn`) and reuses that advice mid-loop, so it can feel stale after the tool loop has moved on. `/moa-flush` (registered by hermes-moa-trigger) resets the facade's turn-scoped reference cache — the next aggregator step re-runs the max-reasoning advisor against the FULL current state. The bare flush is instant (never blocks the terminal, never gated — it targets the built-in reference, not the plugin's triggers); pass a focus to also get an immediate advisory against the live conversation: `/moa-flush focus on caching design`. The focus-triggered advisory runs the LLM and **is** gated by plugin enablement (a disabled plugin flushes the cache but skips the advisory — use `/moa-enable` to get focused passes).

Merge the `moa.presets.max-think-def-output` block from `moa-presets/max-think-def-output.yaml` into your `~/.hermes/config.yaml`, adapting provider/model to your endpoint. Then activate in-session:

```bash
/model max-think-def-output        # or: /model moa:max-think-def-output
```

**Cadence**: the preset ships with `fanout: user_turn` (cheap — advisor once per turn, aggregator grinds alone after). Mid-loop planning depth is handled by the companion plugin, not by fanout.

## 🎯 MoA Planning Trigger — hermes-moa-trigger

The companion plugin that gives you **max reasoning exactly when planning happens mid-loop**, without the `per_iteration` cost multiplier.

```
plugins/hermes-moa-trigger
```

**Two trigger paths:**

1. **Automatic** — a `tool_execution` middleware intercepts every `todo` plan write (the 📋 "preparing todo…" / "📋 plan" moments). It runs a fresh max-reasoning advisory pass over the CURRENT conversation state, lets the todo write proceed, and appends the advice to the tool result so the agent reads it on its next thinking step. **Always fires** — every plan write gets the pass, no cooldown or cost gate. Toggle with `HERMES_MOA_TRIGGER_ON_TODO` (default `1`).

2. **Manual** — the `planning_trigger` tool. The tool description names six explicit trigger moments: (1) after a test/command failure that contradicts the current approach, (2) before large or irreversible changes (refactor, migration, rewrite, deletion), (3) before security-sensitive actions (deploys, credential handling, prod mutations), (4) when the plan must change mid-task based on new tool output, (5) before delegating a subagent task, (6) final review pass before declaring the task complete.

The first user/subagent message is covered by the preset itself (`fanout: user_turn` runs the max advisor once at turn start / subagent kickoff), so every new task begins with a max-reasoning pass.

Both paths reuse the MoA reference machinery — same advisory prompt, same message shaping, same per-slot `reasoning_effort` resolution, same `call_llm` chokepoint. They just fire on planning events instead of a fixed cadence.

**Why this beats `per_iteration`:** with `per_iteration`, a 20-iteration coding turn pays 20 max-reasoning advisor runs (the "feels stuck" problem). With this plugin + `user_turn`, max reasoning fires exactly at the moments that matter — first user/subagent message, every 📋 todo/plan write, and any manual `planning_trigger` call — with zero per-iteration multiplier.

**Install:** the plugin installs via `install-ultimate.sh` (it is in the plugin list). It needs the `max-think-def-output` preset (or any preset with a max/ultra reference slot) configured for the advisor model.

**Honest limits**: the max advisor's advice is injected as context — the aggregator's own planning steps run at default reasoning (it follows `/reasoning`). Whether `reasoning_effort: max` actually changes depth depends on your endpoint honoring the parameter; on gateways that ignore it, the advisor still runs, just at the backend's baked-in depth.

**Enabled by default? NO — the plugin's triggers are DISABLED until you turn them on.** The plugin registers its commands whenever it loads (it must, to receive `/moa-enable`), but its max-reasoning triggers (the todo-auto-fire middleware + the `planning_trigger` tool) NO-OP until enabled. Enable it in-session:

```
/moa-enable --session     # this session only — resets on restart
/moa-enable --global      # persist in config.yaml — survives restart
/moa-enable               # both (default)
/moa-disable --session    # this session only
/moa-disable --global     # persist in config.yaml
/moa-status               # show global/session/effective state
```

The `planning_trigger` tool returns a clear "DISABLED" error when off; the todo middleware simply doesn't fire. This lets you run Hermes with the plugin loaded (so `/moa-enable` is always available) but zero advisory cost until you opt in.

## 🧠 Four-Layer Agent Memory — hermes-memory-tdai

Persistent agent memory via the [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) gateway — a 4-layer memory hierarchy:

| Layer | What it stores | Needs LLM? |
|-------|---------------|------------|
| **L0** | Raw conversation store (capture + search) | No |
| **L1** | Atomic structured memories (facts, episodic) | Yes (extraction) |
| **L2** | Scenario blocks (Markdown scene files) | Yes |
| **L3** | Core persona / user profile | Yes |

```bash
# The plugin auto-clones the gateway repo, npm-installs MemoryCore,
# and starts the gateway on 127.0.0.1:8420 on first use — no manual steps.

# L0 (works with zero LLM):
/tdai capture '[{"role": "user", "content": "..."}]'   # store conversation
/tdai recall "what did we decide about X"               # recall from all layers

# L1-L3 (needs TDAI_LLM_BASE_URL / TDAI_LLM_API_KEY / TDAI_LLM_MODEL env):
/tdai search "auth design"                              # L1 atomic memories
/tdai scenarios                                         # L2 scenario blocks
/tdai write-core "persona: ..."                         # L3 core memory
```

| Tool | What it does |
|------|-------------|
| `tdai_capture` | Capture conversation messages to L0 |
| `tdai_conversations` | Search L0 raw conversation history |
| `tdai_search` | Search L1 structured memories |
| `tdai_scenarios` | List L2 scenario blocks |
| `tdai_read_scenario` | Read a specific L2 scenario block |
| `tdai_core` | Read L3 core memory (persona) |
| `tdai_write_core` | Write L3 core memory (persona) |
| `tdai_recall` | Recall from all memory layers (primary retrieval) |
| `tdai_status` | Gateway + engine status |

9 Hermes tools + `/tdai` slash command.

**Self-bootstrapping:** on a fresh machine, the first `tdai_*` call clones `TencentDB-Agent-Memory` to `~/.hermes/tdai/`, runs `npm install` in `MemoryCore/`, starts the gateway on port 8420, and waits for `/health` — fully non-interactive. All state persists in `~/.hermes/tdai/` and survives reboots (gateway auto-restarts on next use).

## 🎯 The Vibe Coding Stack

```
┌─────────────────────────────────────────────────────────────────┐
│                    Hermes AI Agent Loop                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌─────────────────┐   ┌──────────────┐   ┌──────────────────┐  │
│   │  hermes-lsp     │   │ hermes-semble│   │hermes-graphify   │  │
│   │  (per-file      │   │ (semantic    │   │ (structural      │  │
│   │   depth)        │   │  search)     │   │  understanding)  │  │
│   │                 │   │              │   │                  │  │
│   │ lsp_verify      │   │ semble_search│   │ graphify_query   │  │
│   │ lsp_completions │   │ find_related │   │ graphify_path    │  │
│   │ lsp_hover       │   │ stats        │   │ graphify_explain │  │
│   │ lsp_definition  │   │ reindex      │   │ god_nodes        │  │
│   │ lsp_auto_fix    │   │ status       │   │ stats            │  │
│   │ lsp_servers     │   │              │   │ find             │  │
│   │ lsp_diagnostics │   │              │   │ community        │  │
│   └─────────────────┘   └──────────────┘   └──────────────────┘  │
│                                                                  │
│   ┌──────────────────────────────────────────────────────────┐   │
│   │  hermes-effect-engine (functional core for all tools)    │   │
│   │  effect_run • effect_scope • effect_service • inspect   │   │
│   └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│   ┌──────────────────────────────────────────────────────────┐   │
│   │  hermes-tps (t/s status bar + plugin usage indicators)  │   │
│   └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│   Workflow:                                                      │
│   1. Semble → find the right file/concept semantically           │
│   2. Graphify → explain how it connects to everything else      │
│   3. LSP → verify correctness after every edit                  │
│   4. Effect engine → compose operations with typed error safety │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### The Full Plugin Inventory (16 plugins, 93 tools)

| Plugin | Purpose |
|--------|---------|
| hermes-effect-engine | Typed functional core — effect_run, effect_scope, effect_service, effect_inspect |
| hermes-lsp | Per-file code intelligence — diagnostics, completions, hover, definition, auto-fix (auto-installs missing npm servers) |
| hermes-semble | Semantic code search — hybrid BM25 + embeddings |
| hermes-graphify | Structural understanding — knowledge graph, call chains, communities |
| hermes-codegraph | Deterministic AST code graph — callers, callees, impact |
| hermes-codegraph-context | Advanced analysis — dead code, complexity, call chains, Spring |
| hermes-orchestra | Spec-driven development — OpenSpec artifact DAG + Beads issue tracking |
| hermes-searxng | Metasearch across 170+ engines (general, news, images, video, science) |
| hermes-cloakbrowser | Stealth browser automation for JS-heavy / anti-bot pages |
| hermes-vault | Persistent memory vault — semantic search + structured notes |
| hermes-agents | Multi-agent orchestration — 20+ specialist personas, auto-synced skills |
| hermes-tps | TUI status bar — t/s, plugin usage indicators, 💭REF/🎯AGG MoA role chip |
| hermes-moa-trigger | MoA planning trigger — automatic max-reasoning pass on todo/plan writes + manual planning_trigger tool |
| hermes-memory-tdai | Four-layer agent memory — L0 conversations → L1 atoms → L2 scenarios → L3 persona via TencentDB Agent Memory |
| hermes-dsh | DeepSeek Harness integration — drive the dsh headless agent + introspect its durable session store |
| hermes-anchored | Anchored Standard — narrow the first request to a minimal tool catalog, restore the full catalog after |

### The Self-Correcting Loop

```
1. Agent edits file.py
2. Agent calls lsp_verify(filepath="file.py", content="<new content>")
3. LSP server returns diagnostics (errors, warnings)
4. If errors found:
   a. Agent calls lsp_auto_fix(filepath="file.py")
   b. Agent applies suggested fixes
   c. Agent re-verifies
5. Only when passed=true does the agent proceed
```

This eliminates the most common failure mode of AI coding agents: **silently shipping broken code**.

## 🗺️ Supported Languages

49 languages via language servers. **Auto-install:** npm-installable servers (`sql-language-server`, `typescript-language-server`, `bash-language-server`, `dockerfile-language-server-nodejs`, `yaml-language-server`, `vscode-json/html/css-languageserver`, `graphql-language-service-server`, `@prisma/language-server`, `@anthropic/pgls`, `intelephense`, `svelte-language-server`, `@vue/language-server`, `@astrojs/language-server`, `perlnavigator`, `matlab-language-server`, `makefile-language-server`) install automatically on first use (`npm install -g <pkg>`, once per session, non-interactive). Package-manager servers (clangd, marksman, gopls, rust-analyzer, etc.) show an install hint.

| Language | Server | Install |
|----------|--------|---------|
| Python | Pyright / basedpyright | `pip install pyright` |
| TypeScript / JavaScript | typescript-language-server | auto (`npm i -g typescript-language-server`) |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |
| C / C++ | clangd | `apt install clangd` / `brew install llvm` |
| JSON / YAML / HTML / CSS | vscode-* languageservers | auto (npm) |
| Bash | bash-language-server | auto (npm) |
| Dockerfile | dockerfile-language-server-nodejs | auto (npm) |
| SQL / PLSQL / TSQL / MySQL / SQLite | sql-language-server | auto (npm) |
| GraphQL | graphql-language-service-server | auto (npm) |
| Prisma | @prisma/language-server | auto (npm) |
| + 30 more | marksman, texlab, lemminx, taplo, bufls, R, Julia, Matlab, terraform-ls, nil, cmake, PowerShell, Eclipse JDT, Kotlin, Metals, Roslyn, intelephense, Solargraph, Perl, Lua, Swift, Elixir, Erlang, Haskell, Vue, Svelte, Astro, Pyright, PHP | per-language hints |

## 🏗️ Architecture

```
~/.hermes/plugins/
├── hermes-effect-engine/     # Effect-ts-style functional core (stdlib only)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # TypedError, ServiceContainer, Scope, Fiber, Effect, Schema, ToolDef
│                              # Thread-safe, .env-configured
│
├── hermes-lsp/               # LSP code intelligence — 49 languages (stdlib only)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # LSPManager, LSPClient, JSON-RPC, cross-repo fallback
│                              # Thread-safe, .env-configured
│
├── hermes-semble/            # Semantic code search (requires: pip install semble)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # _SembleEngine, BM25+semantic hybrid search
│                              # Thread-safe, .env-configured
│
├── hermes-graphify/          # Knowledge graph (requires: pip install graphifyy)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # _GraphEngine, dependency graph queries, JIT auto-build, auto-.gitignore
│                              # Thread-safe, .env-configured
│
├── hermes-tps/               # t/s status bar (stdlib only)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # post_api_request hook + HermesCLI monkey-patch
│                              # Thread-safe, no deps
│
├── hermes-memory-tdai/       # Four-layer agent memory (auto-clones TencentDB gateway)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # L0-L3 client, gateway supervisor, auto npm install
│                              # Thread-safe, stdlib HTTP client
│
├── hermes-dsh/               # DeepSeek Harness integration (managed npm install)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # dsh_run + session introspection, SSE proxy
│                              # Thread-safe, stdlib HTTP
│
├── hermes-anchored/          # Anchored Standard tool trajectory (stdlib only)
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # llm_request middleware: turn-1 anchor, turn-2+ full catalog
│                              # Thread-safe, no deps, durable state
│
└── _shared/                  # Shared dependency management
    └── deps.py               # JIT dep installer — auto-installs deps on first use (ask=False)
```

### Thread Safety Architecture

```
Main Thread (Hermes agent loop)          Reader Thread (per LSP client)
─────────────────────────────            ─────────────────────────────
send_request() ──── stdin ──────►        read_loop() ──── stdout ◄────
  ↑under _lock                             │
  │                                        ├── _read_line_timeout()
  │                                        └── _handle_message()
  │                                              │
  │                                       _diagnostics ←── under _diag_lock
  │                                              │
  ◄──── pending_requests[id].event.set() ─────────┘
       under _lock

Manager (singleton)
  _clients ─── under _lock
  _known_roots ─── under _known_roots_lock
  _cross_repo_cache ─── under _cross_repo_cache_lock
```

All shared state is protected by dedicated locks. No lock ordering deadlocks — the manager never holds a client lock while acquiring another, and vice versa.

### Environment Variable Configuration

Every timeout, limit, and interval is configurable via environment variables with sensible defaults. **70+ environment variables** across all fourteen plugins (LSP/EE/semble/graphify use `HERMES_*`; memory-tdai uses `TDAI_*`; codegraph uses `HERMES_CODEGRAPH_*`/`HERMES_CGC_*`):

```bash
# ── LSP timeouts ──────────────────────────────────────────
HERMES_LSP_REQUEST_TIMEOUT=15           # Per-request timeout (seconds)
HERMES_LSP_HEADER_TIMEOUT=5             # Header read timeout
HERMES_LSP_CONTENT_TIMEOUT=30           # Content read timeout
HERMES_LSP_DIAGNOSTICS_TIMEOUT=5        # Max wait for diagnostics after edit
HERMES_LSP_STOP_TIMEOUT=5               # Max wait for server process to stop
HERMES_LSP_CHECK_TIMEOUT=10             # Server binary check timeout

# ── LSP limits ────────────────────────────────────────────
HERMES_LSP_MAX_DIAGNOSTICS=20           # Max errors returned
HERMES_LSP_MAX_WARNINGS=20              # Max warnings returned
HERMES_LSP_MAX_INFO=10                  # Max info diagnostics returned
HERMES_LSP_MAX_COMPLETIONS=30           # Max completions returned
HERMES_LSP_MAX_CONTENT_LENGTH=10485760  # Max message body (10MB)

# ── LSP lifecycle ─────────────────────────────────────────
HERMES_LSP_CLIENT_TTL=300               # Idle client eviction (seconds)
HERMES_LSP_EVICTION_INTERVAL=60         # Eviction sweep interval
HERMES_LSP_POLL_INTERVAL=0.01           # Reader thread poll interval
HERMES_LSP_READ_CHUNK_SIZE=4096         # Stdout read chunk size
HERMES_LSP_READ_POLL_INTERVAL=0.01      # Read poll interval

# ── Cache TTLs ────────────────────────────────────────────
HERMES_LSP_SERVER_CACHE_TTL=60          # Server availability cache
HERMES_LSP_CROSS_REPO_CACHE_TTL=30      # Cross-repo lookup cache
HERMES_LSP_KNOWN_ROOTS_MAX=50           # Max tracked project roots
HERMES_LSP_CROSS_REPO_CACHE_MAX=100     # Max cross-repo cache entries

# ── Effect engine ─────────────────────────────────────────
HERMES_EFFECT_RETRY_MAX_ATTEMPTS=3      # Effect retry attempts
HERMES_EFFECT_RETRY_DELAY_MS=1000       # Delay between retries
HERMES_EFFECT_RETRY_MAX_DELAY_MS=30000  # Max exponential backoff
HERMES_EFFECT_DEFAULT_TIMEOUT_MS=30000  # Effect run timeout
HERMES_EFFECT_SHELL_TIMEOUT=30          # Shell command timeout
HERMES_EFFECT_FIBER_JOIN_TIMEOUT=30     # Fiber join timeout
HERMES_EFFECT_POOL_SIZE=4               # Thread pool size

# ── Semble ────────────────────────────────────────────────
HERMES_SEMBLE_CACHE_SIZE=10             # Max cached indexes (LRU eviction)
HERMES_SEMBLE_TOP_K=5                   # Default results per search
HERMES_SEMBLE_SNIPPET_LINES=10          # Default snippet line count
HERMES_SEMBLE_INDEX_TIMEOUT=120.0       # Max seconds to wait for indexing

# ── Graphify ──────────────────────────────────────────────
HERMES_GRAPHIFY_GRAPH=""                # Default graph path
HERMES_GRAPHIFY_CACHE_SIZE=10           # Max cached graphs (LRU eviction)
HERMES_GRAPHIFY_QUERY_DEPTH=3           # Default traversal depth
HERMES_GRAPHIFY_TOKEN_BUDGET=2000       # Default output token budget
HERMES_GRAPHIFY_MAX_FILE_SIZE=104857600 # Max graph file size (100MB)

# ── Memory-Tdai (TencentDB gateway) ───────────────────────
TDAI_GATEWAY_HOST=127.0.0.1             # Gateway host
TDAI_GATEWAY_PORT=8420                  # Gateway port
TDAI_GATEWAY_API_KEY=""                 # Optional gateway API key
TDAI_TIMEOUT=15                         # HTTP client timeout
TDAI_REPO_DIR=~/.hermes/tdai/tencentdb-agent-memory  # Gateway repo location
TDAI_DATA_DIR=~/.hermes/tdai/data       # L0-L3 storage
TDAI_LLM_BASE_URL=""                    # LLM endpoint for L1-L3 extraction
TDAI_LLM_API_KEY=""                     # LLM API key
TDAI_LLM_MODEL=""                       # LLM model name
HERMES_LSP_INSTALL_TIMEOUT=180          # npm auto-install timeout (seconds)
```

## 🔄 Comparison

| Feature | hermes-ultimate-coding | OpenCode | Claude Code |
|---------|------------------------|----------|-------------|
| **Effect-ts typed errors** | ✓ (Python) | ✓ (TypeScript) | ✗ |
| **Effect-ts DI container** | ✓ | ✓ (Layer) | ✗ |
| **Effect-ts Scope + Fiber** | ✓ | ✓ | ✗ |
| **LSP diagnostics** | ✓ (7 tools) | ✓ | ✓ |
| **LSP completions** | ✓ | ✓ | ✓ |
| **LSP go-to-definition** | ✓ + cross-repo | ✓ (single workspace) | ✓ |
| **LSP auto-fix** | ✓ | ✗ | ✗ |
| **Cross-repo resolution** | ✓ (self-adapting) | ✗ | ✗ |
| **Idle client eviction** | ✓ | ✗ | ✗ |
| **Thread safety** | ✓ (dedicated locks) | ✗ (single-threaded) | N/A |
| **Timeouts on all I/O** | ✓ (configurable) | Partial | ✓ |
| **Environment variable config** | ✓ (70+ vars) | ✗ (hardcoded) | ✗ |
| **Zero external deps (LSP + EE)** | ✓ (stdlib only) | ✗ (Effect-ts, AI SDK) | ✗ (bundled) |
| **Agent-agnostic** | ✓ (Hermes, OpenCode, Cline) | ✗ (OpenCode only) | ✗ (Claude Code only) |
| **Survives updates** | ✓ (user plugin dir) | ✗ (monorepo) | ✗ (bundled) |
| **Languages** | 49 | ~10 | ~10 |
| **Semantic code search** | ✓ (Semble) | ✗ | ✗ |
| **Knowledge graph** | ✓ (Graphify) | ✗ | ✗ |
| **Auto-.gitignore** | ✓ (graphify-out/ on build) | ✗ | ✗ |
| **JIT auto-build graph** | ✓ (builds on first use) | ✗ | ✗ |
| **t/s status bar** | ✓ (Hermes TUI) | ✗ | ✗ |
| **Four-layer agent memory** | ✓ (TencentDB gateway) | ✗ | ✗ |
| **Auto-setup on fresh machines** | ✓ (all 16 plugins self-bootstrap) | ✗ | ✗ |

## 📄 License

MIT

---

<p align="center">
  <b>hermes-ultimate-coding</b> — Ultimate vibe coding plugins for Hermes AI agent.
</p>
