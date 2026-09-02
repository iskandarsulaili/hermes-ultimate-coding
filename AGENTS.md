<!-- PLUGIN-INVENTORY-START -->
You have 16 plugins with 94 tools available. Use them actively in every task.

- **hermes-agents** (7 tools): Multi-agent orchestration for Hermes — 20+ specialized agent personas (architect, planner, executor, code-reviewer, test-engineer, security-reviewer, etc.). Ported from oh-my-claudecode.
- **hermes-anchored** (2 tools): Anchored Standard — narrow the first model request to a minimal tool catalog (terminal, patch, dev_tool_search) to anchor the reasoning trajectory, then restore the full tool catalog from the second request on. Tool discovery via dev_tool_search. Enabled by default (HERMES_ANCHORED_ENABLED=0 to opt out). Ported from dsh-anchored-standard.
- **hermes-cloakbrowser** (6 tools): Stealth browser automation via CloakBrowser — fingerprint rotation, proxy support, humanized interaction. No Docker.
- **hermes-codegraph** (8 tools): Code intelligence via CodeGraph (colbymchenry/codegraph). Deterministic AST-based code knowledge graph for Hermes — search symbols, trace callers/callees, analyze impact radius, explore code structure. Auto-installs via npx.
- **hermes-codegraph-context** (8 tools): Advanced code analysis via CodeGraphContext. Deterministic code relationships, dead code detection, complexity analysis, Spring/Java framework introspection, call chain tracing, and Cypher graph queries. Auto-installs via pip.
- **hermes-dsh** (7 tools): DeepSeek Harness integration — drive dsh headless agent runs and introspect its event-sourced SQLite session store, lineage, and replay-grade logs from Hermes.
- **hermes-effect-engine** (4 tools): Effect-ts-style functional architecture for Hermes: typed errors, structured concurrency, dependency injection, and runtime schema validation for tool calls. Survives Hermes updates by living entirely in ~/.hermes/plugins/.
- **hermes-graphify** (8 tools): Knowledge graph for Hermes via Graphify. Auto-builds on session start, auto-updates on file changes, injects structural context before every LLM call. Query dependency graphs, trace call chains, find subsystems, and explain concepts. Complements LSP (per-file depth) and Semble (semantic search) with structural relationships.
- **hermes-lsp** (7 tools): Language Server Protocol integration for Hermes. Provides real-time code diagnostics, completions, hover info, go-to-definition, and auto-fix suggestions during agentic coding tasks. Survives Hermes updates by living entirely in ~/.hermes/plugins/.
- **hermes-memory-tdai** (9 tools): Four-layer agent memory (L0 conversation → L1 atoms → L2 scenarios → L3 persona) via the TencentDB Agent Memory gateway. Search, recall, capture, and manage long-term memory assets.
- **hermes-moa-trigger** (1 tools): On-demand max-reasoning planning trigger for Hermes MoA — detect planning moments mid-loop and force a fresh max-depth advisory pass, without per-iteration fanout cost.
- **hermes-orchestra** (12 tools): Combined spec-driven development (OpenSpec) + version-controlled issue tracking (Beads). Artifact DAGs auto-create tracked issues, validation gates transitions, multi-agent claims/leases coordinate work.
- **hermes-searxng** (4 tools): Metasearch engine — query 170+ search providers through a single native plugin. No Docker. Privacy-first.
- **hermes-semble** (5 tools): Code search for Hermes — hybrid BM25 + semantic search via Semble. Auto-indexes on session start, auto-reindex on file changes. Uses ~98% fewer tokens than grep+read.
- **hermes-tps** (0 tools): Tokens-per-second + plugin call counts in the Hermes TUI status bar. Self-contained — survives Hermes updates.
- **hermes-vault** (6 tools): Persistent memory vault for Hermes — semantic search, structured notes, session lifecycle. Wraps QMD for Obsidian vault search.
<!-- PLUGIN-INVENTORY-END -->
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
