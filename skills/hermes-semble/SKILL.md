---
name: hermes-semble
description: "Use when you need to find code by meaning rather than by exact text — \"how does auth work\", \"where is rate limiting\", \"find implementations of this interface\" — or when a grep has failed because you do not know the exact identifier. Wraps Semble (hybrid BM25 + static-embedding search with tree-sitter AST chunking) via semble_search, semble_find_related, semble_reindex, semble_stats, semble_status."
version: 2.0.0
license: MIT
---

# hermes-semble — semantic code search

[Semble](https://github.com/MinishLab/semble): tree-sitter AST chunking, dual
BM25 + Model2Vec indexing, reciprocal-rank fusion, then reranking (definition
boost, file coherence, path penalties). CPU-only, milliseconds, no API keys.

## When this beats grep

| You want | Use | Why |
|---|---|---|
| "how is authentication handled?" | `semble_search` | no exact string to grep for |
| "where is UserService.createUser" | `semble_search` | finds the definition, not 40 call sites |
| "other implementations of this interface" | `semble_find_related` | structural + semantic similarity |
| `grep -rn "TODO"` | **Grep** | you know the literal text |
| a specific error message | **Grep** | exact match is exact |
| full file contents | **Read** | after search locates it |

Semantic search and grep are complements. Reach for Semble when you cannot name
the thing you are looking for; reach for Grep the moment you can.

## Usage

```
semble_search  query="how are plugin tools registered"  repo="/path/to/repo"  top_k=5
semble_search  query="..."  max_snippet_lines=0          # locations only, cheapest
semble_search  query="..."  filter_languages=["python"]
semble_search  query="..."  filter_paths=["src/api"]
semble_find_related  ...
semble_stats  /  semble_status                            # index state, availability
semble_reindex                                            # force a rebuild
```

## Indexing behaviour

**In Claude Code** no session hooks fire, so:

- the first `semble_search` against a repo **builds the index on demand** (a
  couple of seconds on a mid-size repo) — this is normal, not an error;
- after *you* edit files the index is stale. Call `semble_reindex` before a
  search that must see your own changes;
- `repo` defaults to the current working directory. Pass it explicitly when the
  cwd is not the project root — pointing it at a home directory starts a large
  and useless walk.

**In Hermes** the index builds on session start and refreshes on file change,
so the reindex step is usually unnecessary.

## Cost

`semble_search` returns ranked snippets with file paths and line numbers instead
of whole files, which is the entire point: a broad question costs one call rather
than a grep plus a dozen reads. Preserve that — start with `max_snippet_lines=0`
when you only need to know *where*, and read the file once you do.
