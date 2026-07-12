<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/iskandarsulaili/agentic-lsp/main/assets/logo-dark.svg">
    <img src="https://raw.githubusercontent.com/iskandarsulaili/agentic-lsp/main/assets/logo-light.svg" alt="agentic-lsp" width="480">
  </picture>
</p>

<p align="center">
  <b>OpenCode's architecture advantages — as Hermes plugins.</b>
</p>

<p align="center">
  LSP code intelligence • Effect-ts typed errors • Structured concurrency • DI container
</p>

<p align="center">
  <a href="#-features">Features</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-opencode-architecture-replicated">Architecture</a> •
  <a href="#-comparison">Comparison</a>
</p>

<p align="center">
  <a href="https://github.com/iskandarsulaili/agentic-lsp"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-agentic--lsp-2ea44f?style=flat-square&logo=github"></a>
  <a href="https://github.com/iskandarsulaili/agentic-lsp/blob/main/LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"></a>
  <a href="#"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square&logo=python"></a>
  <a href="#"><img alt="Zero deps" src="https://img.shields.io/badge/dependencies-zero-success?style=flat-square"></a>
  <a href="https://github.com/sponsors/iskandarsulaili"><img alt="Sponsor" src="https://img.shields.io/badge/sponsor-30363D?style=flat-square&logo=GitHub-Sponsors&logoColor=EA4AAA"></a>
</p>

---

**agentic-lsp** gives Hermes the two architectural advantages that make OpenCode powerful for agentic coding:

1. **Effect-ts-style functional core** — typed errors, structured concurrency, dependency injection, composable effects. Tool chains that can't fail silently because every error type is tracked.
2. **LSP code intelligence** — real-time diagnostics, completions, hover, go-to-definition, auto-fix. The agent self-corrects after every edit instead of shipping broken code.

Both are **pure Python, zero external dependencies** (stdlib only). They install in seconds and survive Hermes updates because they live in `~/.hermes/plugins/`, not in Hermes's core. All timeouts and limits are configurable via `.env` — no hardcoded settings.

## ✨ Features

### OpenCode's Effect-ts Architecture — in Python

| What OpenCode has | What agentic-lsp provides |
|-------------------|--------------------------|
| Effect-ts `Effect<A, E, R>` | `Effect[T, E]` — compose, map, flatMap, catch, retry, withTimeout |
| Effect-ts `Schema.TaggedError` | `TypedError` — tagged errors with `_tag` discriminator, JSON round-trip |
| Effect-ts `Layer` (DI) | `ServiceContainer` — register services with deps, resolve graphs, detect cycles at register time |
| Effect-ts `Scope` + `Fiber` | `Scope` + `Fiber` — async `fork`, `join`, `interrupt`, auto-cancel on scope exit |
| Effect-ts `Logger` | Python `logging` — all configurable via env |
| TypeScript runtime | Python 3.11+ — no transpilation, no bundling |

All exposed through 4 Hermes tools:

| Tool | What it does |
|------|-------------|
| `effect_run` | Execute a chain of operations as a typed effect. Each step validated, errors tracked by type, stops on first typed failure. |
| `effect_scope` | Fork concurrent fibers, join results, cancel, or list running fibers. Auto-cancels on scope exit. |
| `effect_service` | Register services with explicit dependencies, resolve them, or inspect the graph. Cycle detection at register time. |
| `effect_inspect` | Inspect the service graph, tool registry, and known error types. |

### OpenCode's LSP Integration — for Hermes

| What OpenCode has | What agentic-lsp provides |
|-------------------|--------------------------|
| LSP diagnostics after every edit | `lsp_verify` — opens file, gets diagnostics, returns pass/fail. Agent self-corrects before shipping. |
| LSP completions | `lsp_completions` — method names, imports, documentation |
| LSP hover | `lsp_hover` — type signatures, documentation for any symbol |
| LSP go-to-definition | `lsp_definition` — file + line number, with cross-repo fallback |
| LSP code actions | `lsp_auto_fix` — quick-fix suggestions (like the IDE lightbulb) |
| Workspace symbol search | `lsp_servers` — list available servers and running clients |

**Cross-repo resolution** — when `goto_definition` can't find a symbol in the current repo, it automatically queries all other running LSP servers of the same language. Self-adapting: discovers related repos organically as you open files. No config needed.

**14 languages** — C, C++, Python, TypeScript, JavaScript, JSON, YAML, Rust, Go, HTML, CSS, Bash, Dockerfile, SQL.

All exposed through 7 Hermes tools + `/lsp` slash command.

### What OpenCode Doesn't Have

| Feature | agentic-lsp | OpenCode |
|---------|-------------|----------|
| **Idle client eviction** | ✓ — clients auto-evicted after TTL | ✗ — clients live forever |
| **Server availability cache** | ✓ — caches binary checks for 60s | ✗ — checks every time |
| **Project root cache** | ✓ — caches root discovery | ✗ — re-discovers every file |
| **Thread safety** | ✓ — every shared state has a lock | ✗ — single-threaded only |
| **Timeouts on every I/O** | ✓ — reads, writes, stops all have configurable timeouts | Partial |
| **.env configuration** | ✓ — 25+ env vars for all timeouts/limits | ✗ — hardcoded |
| **Cross-repo LSP fallback** | ✓ — queries other repos on miss | ✗ — single workspace only |
| **Survives agent updates** | ✓ — lives in user plugin dir | ✗ — bundled in monorepo |
| **Agent-agnostic** | ✓ — works with Hermes, OpenCode, Cline, any plugin system | ✗ — OpenCode only |

## ⚡ Quick Start

### Prerequisites

- **Hermes Agent** — plugins auto-discover from `~/.hermes/plugins/`
- **Python 3.11+** — no other dependencies
- **Language servers** — install the ones you need (see [Supported Languages](#-supported-languages))

### Install

```bash
git clone https://github.com/iskandarsulaili/agentic-lsp.git /tmp/agentic-lsp

# Install both plugins
cp -r /tmp/agentic-lsp/plugins/hermes-lsp ~/.hermes/plugins/hermes-lsp
cp -r /tmp/agentic-lsp/plugins/hermes-effect-engine ~/.hermes/plugins/hermes-effect-engine

# Clean up
rm -rf /tmp/agentic-lsp
```

> **Important:** Each plugin must be a direct subdirectory of `~/.hermes/plugins/`. Cloning the whole repo into `~/.hermes/plugins/agentic-lsp/` will NOT work.

### Enable Plugins

```bash
hermes config set plugins.enabled '["hermes-lsp","hermes-effect-engine"]'
```

### Restart & Verify

```bash
# In Hermes:
/lsp servers
/effect
```

## 🗺️ Supported Languages

| Language | Server | Install |
|----------|--------|---------|
| Python | Pyright / basedpyright | `pip install pyright` |
| TypeScript / JavaScript | typescript-language-server | `npm i -g typescript-language-server` |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |
| C / C++ | clangd | `apt install clangd` / `brew install llvm` |
| JSON | vscode-json-languageserver | `npm i -g vscode-json-languageserver` |
| YAML | yaml-language-server | `npm i -g yaml-language-server` |
| HTML | vscode-html-languageserver | `npm i -g vscode-html-languageserver` |
| CSS | vscode-css-languageserver | `npm i -g vscode-css-languageserver` |
| Bash | bash-language-server | `npm i -g bash-language-server` |
| Dockerfile | dockerfile-language-server-nodejs | `npm i -g dockerfile-language-server-nodejs` |
| SQL | sql-language-server | `npm i -g sql-language-server` |

## 🏗️ Architecture

```
~/.hermes/plugins/
├── hermes-effect-engine/     # Effect-ts-style functional core
│   ├── plugin.yaml           # Hermes plugin manifest
│   └── __init__.py           # TypedError, ServiceContainer, Scope, Fiber, Effect, Schema, ToolDef
│                              # Thread-safe, .env-configured, 0 external deps
│
└── hermes-lsp/               # LSP code intelligence (14 languages)
    ├── plugin.yaml           # Hermes plugin manifest
    └── __init__.py           # LSPManager, LSPClient, JSON-RPC, cross-repo fallback
                               # Thread-safe, .env-configured, 0 external deps
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

### .env Configuration

Every timeout, limit, and interval is configurable via environment variables with sensible defaults:

```bash
# LSP timeouts
HERMES_LSP_REQUEST_TIMEOUT=15           # Per-request timeout (seconds)
HERMES_LSP_HEADER_TIMEOUT=5             # Header read timeout
HERMES_LSP_CONTENT_TIMEOUT=30           # Content read timeout
HERMES_LSP_DIAGNOSTICS_TIMEOUT=5        # Max wait for diagnostics after edit
HERMES_LSP_STOP_TIMEOUT=5               # Max wait for server process to stop

# LSP limits
HERMES_LSP_MAX_DIAGNOSTICS=20           # Max errors returned
HERMES_LSP_MAX_WARNINGS=20              # Max warnings returned
HERMES_LSP_MAX_COMPLETIONS=30           # Max completions returned
HERMES_LSP_MAX_CONTENT_LENGTH=10485760  # Max message body (10MB)

# LSP lifecycle
HERMES_LSP_CLIENT_TTL=300               # Idle client eviction (seconds)
HERMES_LSP_EVICTION_INTERVAL=60         # Eviction sweep interval

# Cache TTLs
HERMES_LSP_SERVER_CACHE_TTL=60          # Server availability cache
HERMES_LSP_CROSS_REPO_CACHE_TTL=30     # Cross-repo lookup cache
HERMES_LSP_KNOWN_ROOTS_MAX=50           # Max tracked project roots
HERMES_LSP_CROSS_REPO_CACHE_MAX=100     # Max cross-repo cache entries

# Effect engine
HERMES_EFFECT_RETRY_MAX_ATTEMPTS=3      # Effect retry attempts
HERMES_EFFECT_RETRY_DELAY_MS=1000       # Delay between retries
HERMES_EFFECT_RETRY_MAX_DELAY_MS=30000  # Max exponential backoff
HERMES_EFFECT_DEFAULT_TIMEOUT_MS=30000  # Effect run timeout
HERMES_EFFECT_SHELL_TIMEOUT=30          # Shell command timeout
HERMES_EFFECT_FIBER_JOIN_TIMEOUT=30     # Fiber join timeout
HERMES_EFFECT_POOL_SIZE=4               # Thread pool size for Effect.with_timeout
```

## 🔄 Comparison

| Feature | agentic-lsp | OpenCode | Claude Code |
|---------|-------------|----------|-------------|
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
| **.env configuration** | ✓ (25+ vars) | ✗ (hardcoded) | ✗ |
| **Zero external deps** | ✓ (stdlib only) | ✗ (Effect-ts, AI SDK) | ✗ (bundled) |
| **Agent-agnostic** | ✓ (Hermes, OpenCode, Cline) | ✗ (OpenCode only) | ✗ (Claude Code only) |
| **Survives updates** | ✓ (user plugin dir) | ✗ (monorepo) | ✗ (bundled) |
| **Languages** | 14 | ~10 | ~10 |

## 🧪 How It Works

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

### The Effect Chain

```
1. Agent defines operations as typed steps
2. effect_run validates each step's input/output against its schema
3. On typed error, the chain stops with a structured error report
4. The agent can catch specific error types and handle them
```

## 📄 License

MIT

---

<p align="center">
  <b>agentic-lsp</b> — OpenCode's architecture, for Hermes.
</p>
