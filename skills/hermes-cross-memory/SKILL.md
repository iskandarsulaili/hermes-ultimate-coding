---
name: hermes-cross-memory
description: "Use when you need to share memory between Claude Code and Hermes — sync the file-based stores of both agents, search a fact either agent saved, write a fact from one agent so the other sees it, or clean up a stale memory entry. Wraps cross_memory_status, cross_memory_search, cross_memory_claude_list/read/write, cross_memory_hermes_list/add, cross_memory_sync, cross_memory_forget."
version: 1.0.0
license: MIT
---

# hermes-cross-memory — Claude Code ↔ Hermes memory sync

Hermes and Claude Code each keep persistent, file-based memory that are independent
silos by default:

```
Hermes        ~/.hermes/memories/MEMORY.md   ( §-delimited paragraphs )
              ~/.hermes/memories/USER.md
Claude Code   ~/.claude/projects/<cwd-hash>/memory/  ( MEMORY.md index + one .md fact per entry )
```

This plugin bi-directionally crosses them so neither agent loses a fact the other
saved, and both can search one combined memory. Because the Claude Code **MCP
bridge** exposes every Hermes plugin tool to Claude Code, the same tools serve both
agents from a single implementation.

## When to use

| Goal | Tool |
|------|------|
| Find a fact across BOTH agents | `cross_memory_search(query)` |
| Are stores reachable / how full | `cross_memory_status` |
| List Claude Code facts for a project | `cross_memory_claude_list(cwd=...)` |
| Read one Claude fact | `cross_memory_claude_read(name, cwd=...)` |
| Write/update a Claude fact (+ fresh index line) | `cross_memory_claude_write(name, body, description=...)` |
| List Hermes MEMORY/USER entries | `cross_memory_hermes_list` |
| Append a Hermes entry (topic-deduped) | `cross_memory_hermes_add(body, file="MEMORY.md")` |
| **Mirror everything both ways** | `cross_memory_sync(confirm=true)` |
| Delete ONE named entry | `cross_memory_forget(store, name, confirm=true)` |

## Usage essentials

- Default project cwd for the Claude store is the current working dir; pass
  `cwd=` when you mean a different repo. `claude_dir=`/`hermes_dir=` overrides let
  you point at a copy for audits.
- **`cross_memory_sync` is two-way and idempotent.** It mirrors Claude facts into
  Hermes MEMORY.md and Hermes sections into Claude fact files, deduped by content
  (whitespace/case-normalized) so re-runs add nothing, and it tags Claude imports
  (`[cross-memory: claude:...]`) so it never re-imports its own mirror back.
  Requires `confirm=true` OR `dry_run=true` (use `dry_run=true` first to preview).
- **`cross_memory_forget` is single-entry only.** Pass the exact topic (Hermes) or
  fact filename (Claude). It never deletes a whole store.
- Atomic writes + path-safety are built in: no mid-write truncation, no
  traversal/absolute paths can escape the stores.

## Availability in Claude Code

All nine `cross_memory_*` tools are bridged over MCP and work normally inside
Claude Code. Note the MCP bridge exposes **Hermes'** plugin tools; the Claude-side
store this plugin reads/writes is Claude Code's own `~/.claude/projects/.../memory/`
— so `cross_memory_claude_write` from inside Claude Code writes a fact Claude Code
actually loads, and `cross_memory_sync` makes it visible to Hermes too.

## Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `HERMES_CROSS_MEMORY_HERMES_DIR` | `~/.hermes/memories` | Hermes store |
| `HERMES_CROSS_MEMORY_CLAUDE_DIR` | `~/.claude/projects` | Claude project memory root |
| `HERMES_CROSS_MEMORY_GLOBAL_CLAUDE` | `~/.claude/CLAUDE.md` | Global CLAUDE.md (searched, optional) |
| `HERMES_CROSS_MEMORY_CWD` | `$PWD` | cwd used to derive the Claude project dir |
| `HERMES_CROSS_MEMORY_SEARCH_LIMIT` | `20` | default cross-store search limit |

## Slash command

```
/cross-memory status               — store paths + entry/fact counts
/cross-memory search <query>       — search both stores
/cross-memory list                 — current project's Claude facts
/cross-memory sync dry-run         — preview the bidirectional sync
```
