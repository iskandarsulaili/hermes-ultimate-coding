---
name: hermes-semble
description: "Hermes plugin that wraps Semble for hybrid BM25+semantic code search. Complements grep+read: Semble for semantic/concept search, grep for exact patterns, read for full context."
trigger: semble_search, semble_find_related, semble_stats, semble_reindex
---

# hermes-semble

A Hermes plugin wrapping [Semble](https://github.com/MinishLab/semble) — fast hybrid code search using BM25 + Model2Vec static embeddings with tree-sitter AST chunking.

## Architecture

Semble's 4-stage pipeline:

1. **Chunking** — tree-sitter splits code at AST boundaries into ~750-char chunks
2. **Dual indexing** — BM25 (lexical) + Model2Vec static embeddings (semantic)
3. **Fusion** — Reciprocal Rank Fusion (RRF) blends the two result sets
4. **Reranking** — definition boost, file coherence, path penalties, identifier stems, adaptive weighting

All runs on CPU in milliseconds. No GPU, no API keys.

## Complement with grep+read

| Search type | Tool | When |
|-------------|------|------|
| Natural language concepts | `semble_search` | "how is auth handled?" |
| Symbol lookup | `semble_search` | "where is UserService.createUser?" |
| Find related code | `semble_find_related` | "all implementations of this interface" |
| Exact pattern/regex | `grep` via terminal | "grep -rn 'TODO' src/" |
| Full file contents | `read_file` | Get complete file after Semble finds the location |

## Tools

- `semble_search(query, repo, top_k, max_snippet_lines)` — code search
- `semble_find_related(file_path, line, repo, top_k, max_snippet_lines)` — similar code
- `semble_stats(repo)` — index statistics
- `semble_reindex(repo)` — force rebuild
- `semble_status()` — engine state

## Configuration via .env

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_SEMBLE_CACHE_SIZE` | 10 | Max cached indexes |
| `HERMES_SEMBLE_TOP_K` | 5 | Default results per search |
| `HERMES_SEMBLE_SNIPPET_LINES` | 10 | Default snippet line count |
| `HERMES_SEMBLE_ROOT_CACHE_TTL` | 3600 | Root revalidation (seconds) |
