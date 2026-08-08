# Plugin Usage Instructions

You have 14 plugins with 84 tools available. Use them actively in every task.
- **hermes-tps** (0 tools): TUI status bar showing t/s + plugin call count indicators. Self-contained — survives updates.
- **hermes-vault** (6 tools): Persistent memory vault — semantic search, structured notes, standup briefing. Wraps QMD for Obsidian vault search.
- **hermes-agents** (7 tools): Multi-agent orchestration — 20+ specialized agent personas (architect, planner, executor, code-reviewer, test-engineer, security-reviewer). Auto-syncs agent-skills (24 skills) and cybersecurity-skills (817 skills).
- **hermes-codegraph** (8 tools): Deterministic AST code knowledge graph. Use INSTEAD of grep+Read.
- **hermes-codegraph-context** (8 tools): Advanced analysis — dead code, complexity, Spring, Cypher.

## Workflow Priority

1. **Orchestra** (`orchestra_propose`, `orchestra_plan`, `orchestra_track`, `orchestra_ready`, `orchestra_claim`) — spec-driven development first: define proposals, expand into artifact DAGs, create tracked work items
2. **SearXNG** (`searxng_query`) — then search the web for research. Use INSTEAD of `web_search` for ALL web queries.
3. **Semble** (`semble_search`, `semble_find_related`) — then find code files by concept
4. **Graphify** (`graphify_query`, `graphify_path`, `graphify_explain`) — understand how code connects
5. **CloakBrowser** (`cloakbrowser_navigate`, `cloakbrowser_screenshot`) — for JS-rendered pages
6. **LSP** (`lsp_verify`, `lsp_auto_fix`) — verify after every edit

## Mandatory Rules

- **Code intelligence synergy** (use this order when exploring code):
  1. `codegraph_explore` — deterministic AST query (callers, callees, impact, source code). Use INSTEAD of grep+Read.
  2. `codegraph_callers`/`codegraph_callees` — precise caller/callee chains (CodeGraph)
  3. `cgc_call_chain` / `cgc_dead_code` / `cgc_complexity` — advanced analysis (CodeGraphContext)
  4. `graphify_query` / `graphify_path` — semantic/LLM exploration for concepts and docs (Graphify)

- **For ALL web queries** → use `searxng_query` over `web_search`. It's faster, broader, and respects privacy. Only fall back to `web_search` if searxng isn't available.
- **After EVERY edit** → call `lsp_verify(filepath=..., content=...)` — do NOT skip
- **Before reading a file you haven't read** → use Semble first to narrow down
- **When asked how things connect** → use `graphify_query` or `graphify_path`
- **When composing multi-step operations** → use `effect_run` instead of chaining raw tools
- **For JS-rendered pages or anti-bot sites** → use `cloakbrowser_launch` + `cloakbrowser_navigate`
- **For parallel tasks** → use `effect_scope` to fork/join fibers

## Quick Reference

| When | What to call |
|------|-------------|
| "Start a new project" | `orchestra_init(proposal="...", overview="...")` |
| "Create a proposal" | `orchestra_propose(name="...", overview="...", requirements=[...])` |
| "Plan the work" | `orchestra_plan(proposal="...")` — expands into artifact DAG + issues |
| "Create a task" | `orchestra_track(title="...", type="task", priority=2)` |
| "What's ready to work on?" | `orchestra_ready()` — finds unblocked issues |
| "Claim this task" | `orchestra_claim(issue_id="iss-001", agent_id="default")` |
| "Update status" | `orchestra_update(issue_id="iss-001", status="in_progress")` |
| "Validate a spec" | `orchestra_validate(spec="proposal-name")` |
| "Archive a change" | `orchestra_archive(change="my-feature")` |
| "Sync with GitHub" | `orchestra_sync(direction="push", repo="owner/name", issue_id="iss-001")` |
| Search the web | `searxng_query(query="...", categories=["general"])` |
| List what engines are available | `searxng_engines(category="images")` |
| "Find code that does X" | `semble_search(query="...", repo=...)` |
| "Where is Y defined?" | `semble_search(query="...")` |
| "How does A connect to B?" | `graphify_query(question="...")` or `graphify_path(source="A", target="B")` |
| "What are the core concepts?" | `graphify_god_nodes(repo=...)` |
| After writing a file | `lsp_verify(filepath=..., content=..., severity_threshold="warning")` — covers 49 languages across programming, database, markup, config, and infra |
| "Fix this error" | `lsp_auto_fix(filepath=...)` then re-verify |
| Multi-step with error handling | `effect_run(steps=[...])` |
| Parallel tasks | `effect_scope(action="fork", operations=[...])` |
| **Vault** | `vault_search`, `vault_get`, `vault_multi_get`, `vault_reindex`, `vault_status`, `vault_standup` | Semantic search across Obsidian vault. Use for persistent memory, decisions, standup briefing. |
| **Agents** | `agents_list`, `agents_get`, `agents_delegate`, `agents_skills`, `agents_get_skill`, `agents_update`, `agents_status` | Multi-agent orchestration — delegate tasks to specialized agent personas (architect, planner, executor, code-reviewer, etc.). Auto-syncs 841 skills from upstream. |
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

## Orchestra — Spec-Driven Development & Tracking

Orchestra combines OpenSpec's artifact DAG with Beads' issue tracking.

**Workflow:**
1. `orchestra_init` — initialize workspace (creates `.hermes/orchestra/`)
2. `orchestra_propose` — create a proposal spec + epic issue
3. `orchestra_plan` — expand into artifact DAG (proposal→specs→design→tasks), creates issues for each
4. `orchestra_ready` — find issues ready to work on (all deps met)
5. `orchestra_claim` — claim an issue (5-min lease, renewable via heartbeat)
6. `orchestra_update` — transition status or add delta requirements
7. `orchestra_validate` — validate spec before closing
8. `orchestra_archive` — merge change deltas into main specs
9. `orchestra_sync` — push/pull with GitHub Issues

All state stored in `.hermes/orchestra/` — JSON files, no external DB.

## Graphify Auto-Build

If graph.json doesn't exist, the first graphify call auto-builds it. Just call the tool. On success, `graphify-out/` is auto-added to `.gitignore`.

## Troubleshooting

- Plugin toolsets should show: `codegraph`, `codegraph-context`, `effect`, `graphify`, `lsp`, `searxng`, `cloakbrowser`, `semble`, `orchestra`
- Enable with: `hermes plugins enable <name>` (NOT `hermes config set`)
- After enabling, restart Hermes
