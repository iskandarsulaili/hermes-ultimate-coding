# Plugin Usage Instructions

You have 5 plugins installed with 23 tools. Use them actively in every coding task.

## The Three-Tool Workflow

1. **Semble** (`semble_search`, `semble_find_related`) — find files by concept before reading them
2. **Graphify** (`graphify_query`, `graphify_path`, `graphify_explain`) — understand how code connects
3. **LSP** (`lsp_verify`, `lsp_auto_fix`) — verify after every edit, never ship broken code

## Mandatory Rules

- **After EVERY edit** → call `lsp_verify(filepath=..., content=...)` — do NOT skip
- **Before reading a file you haven't read** → use Semble first to narrow down
- **When asked how things connect** → use `graphify_query` or `graphify_path`
- **When composing multi-step operations** → use `effect_run` instead of chaining raw tools
- **For parallel tasks** → use `effect_scope` to fork/join fibers

## Quick Reference

| When | What to call |
|------|-------------|
| "Find code that does X" | `semble_search(query="...", repo=...)` |
| "Where is Y defined?" | `semble_search(query="...")` |
| "How does A connect to B?" | `graphify_query(question="...")` or `graphify_path(source="A", target="B")` |
| "What are the core concepts?" | `graphify_god_nodes(repo=...)` |
| After writing a file | `lsp_verify(filepath=..., content=..., severity_threshold="warning")` |
| "Fix this error" | `lsp_auto_fix(filepath=...)` then re-verify |
| Multi-step with error handling | `effect_run(steps=[...])` |
| Parallel tasks | `effect_scope(action="fork", operations=[...])` |

## Graphify Auto-Build

If graph.json doesn't exist, the first graphify call auto-builds it. Just call the tool. On success, `graphify-out/` is auto-added to `.gitignore`.
