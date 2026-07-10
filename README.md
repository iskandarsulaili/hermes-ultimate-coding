<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/iskandarsulaili/agentic-lsp/main/assets/logo-dark.svg">
    <img src="https://raw.githubusercontent.com/iskandarsulaili/agentic-lsp/main/assets/logo-light.svg" alt="agentic-lsp" width="480">
  </picture>
</p>

<p align="center">
  <b>LSP code intelligence + Effect-ts-style typed errors for AI coding agents.</b>
</p>

<p align="center">
  <a href="#-features">Features</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-supported-languages">Languages</a> •
  <a href="#-effect-engine">Effect Engine</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-comparison">Comparison</a>
</p>

<p align="center">
  <a href="https://github.com/iskandarsulaili/agentic-lsp"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-agentic--lsp-2ea44f?style=flat-square&logo=github"></a>
  <a href="https://github.com/iskandarsulaili/agentic-lsp/blob/main/LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"></a>
  <a href="#"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square&logo=python"></a>
  <a href="#"><img alt="Zero deps" src="https://img.shields.io/badge/dependencies-zero-success?style=flat-square"></a>
</p>

---

**agentic-lsp** gives AI coding agents — Hermes, OpenCode, Claude Code, Cline, and any agent with a plugin system — two superpowers:

1. **Real-time LSP code intelligence** — diagnostics, completions, hover, go-to-definition, and auto-fix suggestions. The agent self-corrects after every edit instead of shipping broken code.
2. **Effect-ts-style functional architecture** — typed errors, structured concurrency, dependency injection, and composable effects. Tool chains that can't fail silently because every error type is tracked at compile time.

Both plugins are **pure Python with zero external dependencies** (stdlib only). They install in seconds and survive agent updates because they live in the user plugin directory, not the agent's core.

## ✨ Features

### LSP Code Intelligence (7 tools)

| Tool | What it does |
|------|-------------|
| `lsp_verify` | **The key tool.** After every edit, opens the file in the language server, sends the new content, and returns pass/fail with diagnostics. The agent self-corrects before the user sees broken code. |
| `lsp_diagnostics` | Get real-time errors, warnings, and hints from the language server. |
| `lsp_completions` | Get method names, variable names, imports, and their documentation at a cursor position. |
| `lsp_hover` | Get type signatures and documentation for any symbol. |
| `lsp_definition` | Find where a symbol is defined (file + line number). |
| `lsp_auto_fix` | Get quick-fix suggestions (like the IDE lightbulb) for diagnostics. |
| `lsp_servers` | List available language servers and their installation status. |

### Effect Engine (4 tools + DI container)

| Tool | What it does |
|------|-------------|
| `effect_run` | Execute a chain of operations as a typed effect. Each step is validated, errors are tracked by type, and the chain stops on the first typed failure. |
| `effect_scope` | Fork concurrent fibers, join results, cancel, or list running fibers. Structured concurrency — fibers auto-cancel when the scope exits. |
| `effect_service` | Register services with explicit dependencies, resolve them, or inspect the dependency graph. |
| `effect_inspect` | Inspect the service graph, tool registry, and known error types. |

### Slash Commands

- **`/lsp`** — Quick LSP status, diagnostics, and server listing
- **`/effect`** — Quick effect engine inspection

## ⚡ Quick Start

### Prerequisites

- **Hermes Agent** (recommended) — plugins auto-discover from `~/.hermes/plugins/`
- **Python 3.11+** — no other dependencies
- **Language servers** — install the ones you need (see [Supported Languages](#-supported-languages))

### Install

```bash
# Clone into Hermes user plugins
git clone https://github.com/iskandarsulaili/agentic-lsp.git ~/.hermes/plugins/agentic-lsp

# Or just the LSP plugin
cp -r agentic-lsp/plugins/hermes-lsp ~/.hermes/plugins/hermes-lsp

# Or just the effect engine
cp -r agentic-lsp/plugins/hermes-effect-engine ~/.hermes/plugins/hermes-effect-engine
```

Restart Hermes. The tools appear automatically — no config changes needed.

### Install Language Servers

```bash
# Python
pip install pyright

# TypeScript / JavaScript
npm install -g typescript-language-server

# Rust
rustup component add rust-analyzer

# Go
go install golang.org/x/tools/gopls@latest

# JSON / YAML / HTML / CSS / Bash / Dockerfile
npm install -g vscode-json-languageserver yaml-language-server bash-language-server dockerfile-language-server-nodejs
```

### Verify Installation

```bash
# In Hermes, run:
/lsp servers
# or
lsp_servers action="list"
```

## 🗺️ Supported Languages

| Language | Server | Install |
|----------|--------|---------|
| Python | Pyright / basedpyright | `pip install pyright` |
| TypeScript / JavaScript | typescript-language-server | `npm i -g typescript-language-server` |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |
| JSON | vscode-json-languageserver | `npm i -g vscode-json-languageserver` |
| YAML | yaml-language-server | `npm i -g yaml-language-server` |
| HTML | vscode-html-languageserver | `npm i -g vscode-html-languageserver` |
| CSS | vscode-css-languageserver | `npm i -g vscode-css-languageserver` |
| Bash | bash-language-server | `npm i -g bash-language-server` |
| Dockerfile | dockerfile-language-server-nodejs | `npm i -g dockerfile-language-server-nodejs` |
| SQL | sql-language-server | `npm i -g sql-language-server` |

## 🧠 Effect Engine

The effect engine brings Effect-ts's functional architecture to Python AI agents:

### Typed Errors

```python
class NotFoundError(TypedError):
    _tag = "NotFoundError"
    entity_type: str
    entity_id: str

raise NotFoundError(entity_type="file", entity_id="config.py")
```

Every error has a `_tag` discriminator and survives JSON serialization — tool chains can match on error type across process boundaries.

### Service Container (DI)

```python
container = ServiceContainer()
DB = ServiceTag("Database")
Cache = ServiceTag("Cache")

container.register(DB, lambda: PostgresDB())
container.register(Cache, lambda: RedisCache(), deps=[DB])

db = container.get(DB)      # resolves DB + its deps
cache = container.get(Cache) # resolves Cache -> DB -> Cache
```

Circular dependencies and missing deps fail fast at registration time, not at runtime.

### Structured Concurrency (Scope + Fiber)

```python
async with Scope() as scope:
    fiber = await scope.fork(long_running_task())
    result = await fiber.join()
# fiber is auto-cancelled if scope exits before it completes
```

### Composable Effects

```python
effect = (
    succeed(data)
    .map(validate)
    .flat_map(lambda v: write_to_db(v))
    .catch(NotFoundError, lambda e: fallback())
    .retry(max_attempts=3, delay_ms=1000)
    .with_timeout(30000)
)
result = effect.run()
```

## 🏗️ Architecture

```
~/.hermes/plugins/
├── hermes-effect-engine/     # Effect-ts-style functional core
│   ├── plugin.yaml           # Plugin manifest
│   └── __init__.py           # TypedError, ServiceContainer, Scope, Fiber, Effect, Schema, ToolDef
│
├── hermes-lsp/               # LSP code intelligence
│   ├── plugin.yaml           # Plugin manifest
│   └── __init__.py           # LSPManager, LSPClient, JSON-RPC protocol, 12 language servers
│
└── agentic-coding-enhanced/  # Skill (optional — teaches the agent the workflow)
    └── SKILL.md
```

Both plugins are **pure Python with zero imports from Hermes internals**. They use only stdlib + optional Pydantic. The Hermes plugin system discovers them automatically via `plugin.yaml` + `register(ctx)`.

## 🔄 Comparison

| Feature | agentic-lsp | Claude Code LSP | OpenCode LSP |
|---------|-------------|-----------------|--------------|
| **Agent-agnostic** | ✓ (Hermes, OpenCode, any plugin system) | ✗ (Claude Code only) | ✗ (OpenCode only) |
| **Zero deps** | ✓ (stdlib only) | ✗ (bundled) | ✗ (Effect-ts, AI SDK) |
| **Typed errors** | ✓ (Effect-ts-style) | ✗ | ✓ (Effect-ts) |
| **DI container** | ✓ | ✗ | ✓ (Effect-ts Layer) |
| **Structured concurrency** | ✓ | ✗ | ✓ (Effect-ts Scope) |
| **Self-verifying workflow** | ✓ (lsp_verify) | ✗ | ✗ |
| **Survives agent updates** | ✓ (user plugin dir) | ✗ (bundled) | ✗ (monorepo) |
| **Languages** | 12 | ~10 | ~10 |

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

## 📦 Project Structure

```
agentic-lsp/
├── plugins/
│   ├── hermes-effect-engine/     # Effect-ts-style functional core
│   │   ├── plugin.yaml
│   │   └── __init__.py
│   └── hermes-lsp/               # LSP code intelligence
│       ├── plugin.yaml
│       └── __init__.py
├── skills/
│   └── agentic-coding-enhanced/  # Workflow skill
│       └── SKILL.md
├── README.md
├── LICENSE
└── .gitignore
```

## 🤝 Contributing

PRs welcome! Areas that need work:

- **More language servers** — add entries to `LANGUAGE_SERVERS` in `hermes-lsp/__init__.py`
- **Pydantic schemas** — add typed schemas for common tool inputs/outputs
- **OpenCode plugin adapter** — adapt the Hermes `register(ctx)` pattern to OpenCode's plugin system
- **Claude Code plugin adapter** — adapt for Claude Code's plugin marketplace

## 📄 License

MIT

---

<p align="center">
  <b>agentic-lsp</b> — because AI coding agents should verify their own code.
</p>
