---
name: agentic-coding-enhanced
description: "Use when writing or changing code in a repo that has the Hermes tools available — hybrid semantic+lexical search (semble_*), structural knowledge-graph queries (graphify_*, codegraph_*, cgc_*), real-time LSP diagnostics across 49 languages (lsp_*), and typed effects (effect_*). Provides a self-verifying edit workflow: locate by concept, understand structure, edit, then verify with diagnostics before moving on."
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [coding, lsp, effect, typed-errors, code-quality, verification, self-correcting, search, semantic]
    related_skills: [hermes-semble, hermes-memory-tdai]
---

# Agentic Coding Enhanced

These tools make you faster and more accurate at three things you otherwise do
badly: **finding code by meaning**, **seeing structure**, and **verifying an
edit without running the whole test suite**.

## Runtime note — read this first

The same tools are reachable from two runtimes, and one behaviour differs:

| | Hermes | Claude Code (via the MCP bridge) |
|---|---|---|
| Tool names | `semble_search` | `semble_search`, namespaced `mcp__hermes__semble_search` |
| Index build | auto on session start, auto-refresh on file change | **on demand — no session hooks fire** |

In Claude Code the plugins' `session_start` and file-change hooks are not
invoked, because the bridge serves the tool registry rather than running the
Hermes session lifecycle. In practice this is fine — `semble_search` indexes on
first use (~2s on a mid-size repo) and `graphify_*` starts a background build on
first query — but it has two consequences worth knowing:

- **After you edit files, the index is stale.** Call `semble_reindex` before
  relying on a search that must reflect your own just-written code.
- **Run from the repo you mean.** Graph and index roots default to the current
  working directory. Pass an explicit `repo` / `project_dir` when the cwd is not
  the project — pointing these at a home directory triggers a large, useless walk.

## The workflow

### 1. Locate — by concept, not by string

Reach for `semble_search` when you do not already know the filename or the exact
token. It fuses BM25 with static embeddings, so it answers questions:

```
semble_search  query="how is authentication handled"  repo="/path/to/project"  top_k=5
semble_search  query="UserService.createUser"
semble_find_related  ...   # other implementations of the same idea
```

Keep using Claude Code's own **Grep** for exact patterns — regex, error strings,
`TODO` sweeps, counting occurrences. The two are complements, and grep is the
right tool whenever you know the literal text. Use **Read** to pull full context
once a search has told you where to look.

### 2. Understand structure — before you change it

Semantic search finds *a* place. The graph tools tell you what depends on it:

```
codegraph_callers   symbol="validate_token"     # who calls this
codegraph_callees   symbol="validate_token"     # what it calls
codegraph_impact    symbol="validate_token"     # blast radius of a change
graphify_query      query="how does auth reach the database"
graphify_god_nodes                              # over-connected hotspots
cgc_dead_code                                   # unreferenced definitions
cgc_call_chain      source=... target=...
```

`codegraph_impact` before editing a widely-used symbol is the single highest-value
call here — it is the difference between changing one call site and changing
seventeen.

### 3. Edit

Use Claude Code's normal Edit/Write tools. Nothing Hermes-specific here.

### 4. Verify — the step that makes this loop worth running

```
lsp_diagnostics  filepath="src/thing.py"      # errors/warnings after your edit
lsp_hover        filepath=... line=.. character=..
lsp_definition   filepath=... line=.. character=..
lsp_completions  filepath=... line=.. character=..
lsp_auto_fix     filepath=...                 # apply server-offered fixes
lsp_verify       ...
```

Call `lsp_diagnostics` on every file you edited, **before** reporting the work
done. It catches undefined names, type errors, bad imports and unused symbols in
well under a second — far cheaper than a test run, and it catches the class of
mistake that most often survives review.

`lsp_servers` lists which languages currently have a server available; a language
with no server returns no diagnostics rather than an error, so check it if a file
comes back suspiciously clean.

### 5. Typed effects (optional)

`effect_run`, `effect_scope`, `effect_service`, `effect_inspect` provide typed
error handling, structured concurrency and dependency injection in the Effect-ts
style. Use them when a task genuinely needs typed failure channels or scoped
resource cleanup — not as a default wrapper around ordinary Python.

## A worked loop

```
semble_search      query="rate limiting"                  → src/middleware/limit.py:40
codegraph_callers  symbol="RateLimiter.check"             → 3 call sites
Read               src/middleware/limit.py
Edit               src/middleware/limit.py
lsp_diagnostics    filepath="src/middleware/limit.py"     → 0 errors
semble_reindex                                            → index reflects the edit
```

## Cost discipline

`semble_search` returns located snippets rather than whole files, which is why it
is dramatically cheaper than grep-then-read-everything on a broad question. Keep
that advantage: set `max_snippet_lines=0` when you only need locations, and raise
`top_k` only when the first pass genuinely missed.
