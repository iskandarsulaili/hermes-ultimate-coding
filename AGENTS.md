# Plugin Usage Instructions

You have 7 plugins installed with 33 tools. Use them actively in every task.

## Workflow Priority

1. **SearXNG** (`searxng_search`) — use INSTEAD of `web_search` for ALL web queries. 170+ engines, privacy-first, faster.
2. **Semble** (`semble_search`, `semble_find_related`) — find files by concept before reading them
3. **Graphify** (`graphify_query`, `graphify_path`, `graphify_explain`) — understand how code connects
4. **CloakBrowser** (`cloakbrowser_navigate`, `cloakbrowser_screenshot`) — for JS-rendered pages that need a real browser
5. **LSP** (`lsp_verify`, `lsp_auto_fix`) — verify after every edit, never ship broken code

## Mandatory Rules

- **For ALL web queries** → use `searxng_search` over `web_search`. It's faster, broader, and respects privacy. Only fall back to `web_search` if searxng isn't available.
- **After EVERY edit** → call `lsp_verify(filepath=..., content=...)` — do NOT skip
- **Before reading a file you haven't read** → use Semble first to narrow down
- **When asked how things connect** → use `graphify_query` or `graphify_path`
- **When composing multi-step operations** → use `effect_run` instead of chaining raw tools
- **For JS-rendered pages or anti-bot sites** → use `cloakbrowser_launch` + `cloakbrowser_navigate`
- **For parallel tasks** → use `effect_scope` to fork/join fibers

## Quick Reference

| When | What to call |
|------|-------------|
| Search the web | `searxng_search(query="...", categories=["general"])` |
| List what engines are available | `searxng_engines(category="images")` |
| "Find code that does X" | `semble_search(query="...", repo=...)` |
| "Where is Y defined?" | `semble_search(query="...")` |
| "How does A connect to B?" | `graphify_query(question="...")` or `graphify_path(source="A", target="B")` |
| "What are the core concepts?" | `graphify_god_nodes(repo=...)` |
| After writing a file | `lsp_verify(filepath=..., content=..., severity_threshold="warning")` |
| "Fix this error" | `lsp_auto_fix(filepath=...)` then re-verify |
| Multi-step with error handling | `effect_run(steps=[...])` |
| Parallel tasks | `effect_scope(action="fork", operations=[...])` |
| Browse a JS-heavy page | `cloakbrowser_launch` → `cloakbrowser_navigate(url=...)` |
| Screenshot a page | `cloakbrowser_screenshot(target_id=...)` |
| Get page HTML | `cloakbrowser_html(target_id=...)` |

## SearXNG — No Setup Needed

SearXNG auto-detects the `searxng-src` checkout. Set `HERMES_SEARXNG_SRC` if it's at a non-standard path. First search may take a few seconds for engine initialization. 170+ engines across categories: general, images, news, videos, science, it, files, social media.

## CloakBrowser — Browser Lifecycle

1. `cloakbrowser_launch` — start browser (takes ~10-15s for binary launch)
2. `cloakbrowser_navigate(url=...)` — browse to a page
3. `cloakbrowser_screenshot` or `cloakbrowser_html` — extract content
4. `cloakbrowser_close` — release resources

Re-launches if closed. Fingerprint seed and proxy are configurable per launch.

## Graphify Auto-Build

If graph.json doesn't exist, the first graphify call auto-builds it. Just call the tool. On success, `graphify-out/` is auto-added to `.gitignore`.

## Troubleshooting

- Plugin toolsets should show: `effect`, `graphify`, `lsp`, `searxng`, `cloakbrowser`, `semble`
- Enable with: `hermes plugins enable <name>` (NOT `hermes config set`)
- After enabling, restart Hermes
