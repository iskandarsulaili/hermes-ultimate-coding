---
name: agentic-coding-enhanced
description: "Use when performing agentic coding tasks that benefit from typed error handling, structured concurrency, dependency injection, and real-time LSP diagnostics. Provides self-verifying edit workflow using hermes-effect-engine and hermes-lsp plugins."
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [coding, lsp, effect, typed-errors, code-quality, verification, self-correcting]
    related_skills: [hermes-self-improving-architecture, hermes-agent-skill-authoring]
---

# Agentic Coding Enhanced

This skill teaches you to use the **hermes-effect-engine** and **hermes-lsp** plugins to produce higher-quality code with fewer iterations.

## Workflow

### 1. Before Writing Code — Understand the Codebase

Use LSP tools to explore before editing:

```
lsp_hover filepath="src/main.py" line=42 character=10
lsp_definition filepath="src/main.py" line=42 character=10
lsp_completions filepath="src/main.py" line=50 character=0
```

### 2. While Writing Code — Use Effect-Typed Operations

For multi-step operations, use `effect_run` to chain steps with typed error handling:

```
effect_run steps=[
  {"operation": "read_file", "params": {"path": "src/config.py"}},
  {"operation": "validate", "params": {"schema_type": "Config"}},
  {"operation": "write_file", "params": {"path": "src/config.py", "content": "..."}},
]
```

Register services with explicit dependencies:

```
effect_service action="register" name="Database" deps=["Config"]
effect_service action="register" name="UserService" deps=["Database"]
effect_service action="resolve" name="UserService"
```

### 3. After Every Edit — Verify with LSP

This is the most important step. After every file write or edit, immediately verify:

```
lsp_verify filepath="src/main.py" content="<new content>" severity_threshold="warning"
```

If verification fails:
1. Use `lsp_diagnostics` to see the full list
2. Use `lsp_auto_fix` to get fix suggestions
3. Apply fixes and re-verify
4. Only proceed when `passed: true`

### 4. For Concurrent Operations — Use Structured Concurrency

When you need to run multiple independent operations:

```
effect_scope action="fork" operations=[
  {"name": "lint", "command": "ruff check src/"},
  {"name": "typecheck", "command": "mypy src/"},
  {"name": "test", "command": "pytest tests/ -x"},
]
```

Then join results:

```
effect_scope action="join" fiber_id="<id>"
```

### 5. Inspect the System

```
effect_inspect target="services"   # See registered services
effect_inspect target="tools"      # See registered effect tools
effect_inspect target="errors"     # See known error types
lsp_servers action="status"        # See running LSP clients
```

## Why This Matters

- **Typed errors** catch failures at the boundary, not deep in a stack trace
- **LSP diagnostics** catch type errors, undefined references, and import issues before the user runs the code
- **Structured concurrency** prevents resource leaks from abandoned tool calls
- **Dependency injection** makes service dependencies explicit and verifiable
- **Self-verification** after every edit means the user sees working code, not broken code

## Pitfalls

- LSP servers must be installed separately (`pip install pyright`, `npm install -g typescript-language-server`, etc.)
- The effect engine is in-process — it doesn't survive Hermes restarts (services must be re-registered)
- LSP diagnostics are cached — use `lsp_verify` with content to force a refresh
- Not all languages have LSP servers available — check `lsp_servers action="list"` first
