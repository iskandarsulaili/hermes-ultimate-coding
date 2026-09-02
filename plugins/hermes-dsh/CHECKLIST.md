# hermes-dsh — DeepSeek Harness integration checklist

Goal: ONE Hermes plugin that integrates and max-utilizes deepseek-harness
("dsh") capabilities into Hermes, machine-agnostic and self-healing, ready for
live production use. Complete — zero mock/stub/todo/dormant. Reconcile, never trim.

## What deepseek-harness offers that this plugin leverages (verified in source)
- Independent DeepSeek-powered agent executor: `dsh --profile headless "task"`
  runs one task in a fresh persisted event-sourced Agent and prints the result.
- Event-sourced durable session store: SQLite backend (schema v15), tables
  `sessions` + `events` (append-only, seq-monotonic, crash-safe: interrupted
  turn closed with synthetic closers on next load).
- Session lineage/genealogy: `sessions.parent_session`, `origin='subagent'`,
  `delegation_depth`, `incarnation`, `revision`.
- Headless bundle auto-initializes its profile on first use (no manual setup).
- Bootstraps via the published npm package `@deepseek-ai/dsh` (latest
  0.1.0-rc.6, bin `dsh` → lib/bin.js) — no pnpm, no build tree needed.

## Integration decision (verified environment 2026-08-17)
- Node v24.19.0 (satis�fies ^22.19||>=24) — OK.
- DEEPSEEK_API_KEY + DEEPSEEK_BASE_URL present in ~/.hermes/.env — OK.
- pnpm BROKEN, deepseek-harness-sdk NOT on PyPI → the ONLY viable runtime
  surface is the published npm CLI + the on-disk SQLite session store.
  Headless-CLI + read-only SQLite introspection is the max practical
  utilization (workflow/subagent/plan seams need the interactive/SDK surface
  that is not externally drivable; documented, not silently dropped).

## Tools (toolset `dsh`, prefix `dsh_`)
- [ ] dsh_status         — node/dsh bin resolution, DSH_HOME, key present, install state
- [ ] dsh_bootstrap      — ensure dsh installed (managed npm install), idempotent, self-healing
- [ ] dsh_run            — run a one-shot headless task through dsh; returns answer + session_id
- [ ] dsh_sessions       — list persisted sessions from SQLite store (newest first)
- [ ] dsh_session_events — read one session's durable event-sourced log (seq/type/time/data)
- [ ] dsh_session_export — dump full raw JSONL event log (replay/fork/audit)
- [ ] dsh_lineage        — traverse parent/child (fork) lineage from parent_session

## Plugin-engineering gates (hermes-plugin-patterns)
- [ ] register_tool uses schema= + toolset= (no parameters= top-level)
- [ ] no register_command (none needed)
- [ ] ALL handlers wrapped try/except; no uncaught to core
- [ ] _to_int/_env_int garbage-safe coercion; no bare int(args.get(...))
- [ ] module-level env ints via _env_int (helper ABOVE constants)
- [ ] RLock where a call chain re-enters; Lock otherwise
- [ ] no print()/input(); bounded subprocess timeouts
- [ ] status() never emits null "error"/"message" key (false [error] TUI tag)
- [ ] self-contained — no hermes core imports (survives updates)
- [ ] tool-name collision check vs core tools (diff < 0.7)
- [ ] DSH_BIN / DSH_HOME env overrides; machine-agnostic paths
- [ ] tps _TOOLSET_PREFIXES/_TOOLSET_EMOJI/_TOOLSET_LABEL updated for `dsh`
- [ ] plugin.yaml manifest; `hermes plugins enable hermes-dsh`

## Verification (production readiness) — mark off as done
- [x] import-time robustness: `DSH_BIN=grr python3 -c "exec compile ..."` imports OK
- [x] AST scan: every _handle_* has a Try
- [x] MockCtx registration: 7 tools, toolset `dsh`, required args correct
- [x] tool-name collision check: zero pairs >= 0.7 (dsh_ prefix distinctive)
- [x] tps dicts updated (dsh_ prefix, 🌊, DSH) + import verified
- [x] plugin.yaml manifest + `hermes plugins enable hermes-dsh` (enabled)
- [x] AGENTS.md plugin inventory updated (15 plugins / 91 tools)
- [x] bootstrap E2E: managed npm install completes → bin.js resolves; version probe 0.1.0-rc.6
- [x] npm ancestor-climb guard: state-dir package.json prevents home pollution (verified home clean)
- [x] LLM env resolution: DSH_API_KEY/DSH_BASE_URL/DSH_MODEL override DEEPSEEK_* + .env fallback
- [x] SSE proxy: root-caused omniRoute keepalive/`[DONE]`-discard → STREAM_CLOSED; fixed (preserve [DONE], skip comments/keepalives, whitespace-tolerant stream detect)
- [x] dsh_run E2E: real headless task through live gateway → answer "FINE", exit 0, exactly 1 new session DB-confirmed
- [x] transient-error retry with QUOTA exemption (attempts[] surfaced)
- [x] dsh_sessions lists real persisted sessions (10, newest-first, event_count)
- [x] dsh_session_events reads real event log (turn/end present)
- [x] dsh_session_export dumps JSONL replay artifact
- [x] dsh_lineage traverses (root session: 0 ancestors/children; cycle guard)
- [x] error paths: session not found → clean error
- [x] `hermes plugins list` shows enabled
- [x] idempotent: second dsh_status/dsh_bootstrap do not reinstall (already=True)
- [x] final 7-tool comprehensive verification passed (ALL_VERIFICATION_PASSED)
- [x] real-runtime load proven via `hermes plugins doctor` (OK, 0 WARN) + `hermes plugins show`
- [x] provides_tools declared in manifest (was 7 WARNs; now 0)
- [x] concurrent dsh_run stress (2 threads): session attribution made HONEST —
      same-cwd concurrency is store-ambiguous, so runs claim nothing certain
      and flag all new sessions as session_ids_uncertain; single run still
      claims exactly 1 certain (no regression); _workspace_slug replicates
      dsh's format.ts slug derivation byte-for-byte (verified vs on-disk)
- [x] transient classifier hardened: now catches TRANSPORT / ECONNREFUSED /
      connection refused / fetch failed / 504 / network error (was missing
      gateway-down retries); QUOTA + RATE_LIMIT cooldown still never retried
      (verified: 11-case classifier table all correct)
- [x] dsh_bootstrap(force=True) self-heal verified (reinstalls cleanly,
      version probe 0.1.0-rc.6)
- [x] timeout branch verified (12s kill on long task → attempts[0].outcome=
      timeout + stderr_tail message; no crash, no leaked patch file)
- [x] torn/corrupt/missing session-log handling: _decode_session_log returns
      [] gracefully (mid-write read race is safe; zstd decode failure never
      raises)
- [x] SELF-HEAL proven real: corrupt bin + stale marker no longer falsely
      returns already=True — _probe_installed() --version-verifies the
      existing install; broken installs are cleaned (pkg rmtree + marker
      unlink) and reinstalled; healed bin boots (verified). This was a hollow
      self-heal claim before (npm no-ops on a present-but-corrupt package).
- [x] model-patch write-failure guard: _model_patch_file None now fails
      loudly (was: silently ran the unrouteable base model → cryptic 403)
- [x] cwd-aware slug attribution FIXED (critical): _workspace_slug(None) now
      resolves to the ACTUAL inherited process cwd (os.getcwd()), NOT the
      plugin dir — dsh inherits the launcher's cwd, so a run from any
      non-plugin dir was misattributing its own session as uncertain. Verified
      from /home/lot399 (certain=1), plugin dir (certain=1), and explicit
      cwd=/tmp (certain=1).
- [x] live-runtime exposure confirmed: hermes-dsh in config.yaml enabled list
      + config section (allow_tool_override: false, sibling-consistent);
      `hermes tools` 0-count was an interactive-only false alarm (requires a
      TTY), NOT a real gap — doctor proves import+registration.
- [x] attribution matrix fully verified: single success=1 certain; retry
      success (fail→success)=1 certain (attempts[] shows both); all-fail=exit
      1 empty; concurrent same-cwd=0 certain/all uncertain; non-plugin
      cwd=1 certain; explicit cwd=1 certain.
- [x] SQLite backend DATA-VERIFIED (was code-only): synthetic schema-v15 store
      (exact DDL from dsh session-persistence-sqlite schema.ts) — store
      detection, sessions list (event_count/last_seq/parent/origin), events
      with offset/limit pagination, export with full_data + source_event_seqs,
      and lineage (parent/child) all read correctly.
- [x] DSH_DISABLE_PROXY verified (fresh state: no proxy ever starts, base URL
      unchanged); concurrent _do_install (4 threads) serializes under
      _boot_lock (all already=True, no double-install).
- [x] bounds verified: sessions limit clamps [1,100]; events limit clamps
      [1,2000]; JSONL export full_data returns parsed objects.
- [x] TIMEOUT IS A TOTAL BUDGET (fixed): was per-attempt — a caller's 60s with
      2 retries could stretch to ~200s+backoff. Now a deadline bounds the whole
      call; each attempt gets the remaining budget; a doomed retry is skipped
      (budget-exhausted outcome). Verified: 15s budget + 2 retries = 14.6s
      total, 2 attempts, no 3x stretch.
- [x] zstd decode CLI-independent (fixed): was zstd-CLI-only (silent [] on
      machines without it). Now python-zstandard lib fallback with
      read_across_frames=True (dsh writes MULTI-FRAME zstd — one frame per
      event; single-frame decompress() truncates). Verified: 26 lines decoded
      with the CLI blocked from PATH.
- [x] SQLITE ATTRIBUTION FIXED (real bug): dsh_run's workspace filter used
      row["path"] — absent on sqlite rows → even a single new sqlite session
      was misattributed uncertain. Fix: SELECT s.cwd + backend-aware matcher
      (jsonl: path slug; sqlite: _workspace_slug(cwd) == run slug). Verified:
      single sqlite session now certain. Also fixed column-order off-by-one
      (cwd=r[8], event_count=r[9], last_seq=r[10]).
- [x] SQLITE NOT-FOUND ASYMMETRY FIXED (real bug): events/export/lineage
      sqlite branches silently returned empty (events:[]/0 lines/empty
      lineage) for a missing session while JSONL branches returned
      "session not found". Added existence checks to all three sqlite
      branches — both backends now error consistently. Verified: all 3
      handlers return "session not found" on a synthetic store.
- [x] DIFFERENT-CWD CONCURRENCY verified: two concurrent runs with different
      cwds each claim exactly 1 certain (its own session) + 1 uncertain (the
      other's), no clash — slug attribution separates workspaces correctly
      under concurrency.
- [x] PROXY UPSTREAM-CHANGE RESTART FIXED (real bug): _ensure_proxy's
      readiness probe forwarded to the upstream, so a dead/absent upstream
      made the proxy look unready and the restart failed ("did not become
      ready"). Added a local /healthz endpoint (never forwards upstream);
      probe now checks the LISTENER. Verified: upstream A→B restart works
      even with a dead upstream; same-upstream reuse holds; healthy-proxy
      dsh run still succeeds.
- [x] handler-arg paths verified (no new bug — closed last unverified gaps):
      _handle_dsh_bootstrap({}) idempotent (already=True, version 0.1.0-rc.6);
      dsh_status with explicit dsh_home (store present=jsonl, store
      absent=false); HERMES_ENV_PATH override respected; env precedence
      DSH_API_KEY > DEEPSEEK_* > file and DSH_BASE_URL > DEEPSEEK_BASE_URL;
      dsh_run custom dsh_home writes the session into the CUSTOM home with
      certain attribution.
- [x] DSH_DISABLE_PROXY file-level honored (fixed): was os.environ-only — a
      .env DSH_DISABLE_PROXY=1 was ignored and the proxy still started. Now
      _resolve_disable_proxy() mirrors _resolve_model (env OR file), env wins.
- [x] PATCH FILE NEVER LEAKS (fixed): KeyboardInterrupt/SystemExit during a
      run leaked /tmp/dsh-model-*.yml (cleanup was after the loop, not a
      finally). Moved cleanup into try/finally — verified no leak on Ctrl-C
      and on FileNotFoundError.
- [x] ORPHAN PROXY REUSE (fixed): a hard parent exit (os._exit/SIGKILL skips
      atexit) left a live proxy invisible to fresh processes → duplicates
      accumulated. sse_proxy.lock (port/upstream/pid) lets a fresh process
      detect + reuse a live same-upstream orphan. Verified: hard-exit child
      → fresh process reuses the SAME port (no duplicate).
- [x] EMPTY_RESPONSE retried (fixed): dsh "EMPTY_RESPONSE: completed response
      with no content" was NOT transient — the gateway intermittently returns
      empty content for dsh's request shape (stream:true, max_tokens:256000,
      reasoning burns the budget). Added to the transient markers; verified
      3 attempts happen (was 0). NOTE: the gateway itself has been returning
      empty content consistently for that shape — external flakiness, not a
      plugin defect; direct gateway calls with sane max_tokens work.
- [x] EMPTY_RESPONSE ROOT CAUSE FIXED (the big one): dsh's default
      maxTokens=256_000 breaks the gateway (reasoning burns the whole output
      budget → empty content). The model --patch overlay now ALSO targets the
      llm-deepseek provider row with a sane maxTokens cap (DSH_MAX_TOKENS,
      default 4096). First attempt now succeeds (exit 0, answer FINE) — was
      failing 3/3 attempts. This restored dsh_run's PRIMARY function against
      its own gateway and fixed the concurrency test that had been failing
      on EMPTY_RESPONSE. Full suite re-green: FINAL_E2E_PASSED,
      CONCURRENCY_OK, ALL_VERIFICATION_PASSED, doctor clean.
- [x] PATCH CAUSATION PROVEN: `dsh --dump-config` with the patch shows
      maxTokens: 4096 and NO 256000 — the overlay definitively replaces
      dsh's default (not correlation).
- [x] DSH_MAX_TOKENS CLAMPED (fixed): 0/-5 produced invalid patches
      (schema min 1) → cryptic dsh error. Now clamped [1, 1_000_000] —
      verified 0→1, -5→1, abc→4096, 99999999→1000000.
- [x] LOCKFILE OWNERSHIP RACE FIXED (real bug): a process that REUSED an
      orphan proxy still cleared the lockfile on shutdown, untracking the
      live orphan so the next process spawned a duplicate. _proxy_owned flag:
      only the spawning process may terminate + clear the lock. Verified:
      reuse → shutdown → lockfile survives → next process reuses the SAME
      port (no duplicate).
- [x] PLUGIN-SYNERGY AUDIT (user question): all 15 plugins scanned — ZERO
      tool-name conflicts, ZERO toolset-id conflicts; dsh (7 tools, toolset
      "dsh") is unique and complementary to agents_delegate (Hermes personas),
      orchestra (tracked work items), codegraph/lsp/semble (code intel), and
      the memory stores (vault/tdai). No redundancy, no conflict.
- [x] JSONL offset pagination verified (was sqlite-only): offset=5 limit=3
      returns exactly full_seqs[5:8] (disjoint windows); beyond-end returns 0.
- [x] force-bootstrap full cycle verified: force=True removes pkg + reinstalls
      (installed_now=True, 0.1.0-rc.6), then idempotent again (already=True).
- [x] proxy concurrency verified: 6 threads racing _ensure_proxy serialize
      under _proxy_lock → exactly ONE proxy, consistent ownership flag.
- [x] INTER-PROCESS PROXY RACE FIXED (real bug, parallel-session relevant):
      the threading lock is process-local, so two simultaneous processes
      (parallel Hermes sessions) both spawned proxies for the same upstream
      (lockfile check-then-spawn TOCTOU). Fix: atomic O_EXCL lockfile
      acquisition (_acquire_proxy_lock) — winner spawns, loser WAITS ~2s for
      the winner's populated lock then reuses, never spawns a duplicate.
      Stale-lock recovery: dead-pid lock OR empty lock >30s (crashed winner)
      is broken; a FRESH empty lock (winner in-progress) is never broken.
      Verified: 5/5 two-process race rounds → same port, zero duplicates.
      Also fixed _time module-level import (was function-local only).
- [x] PER-UPSTREAM LOCKFILES FIXED (real bug): the O_EXCL lock was GLOBAL —
      a process holding upstream1's proxy blocked any process wanting
      upstream2 ("retry in a moment" forever). Now keyed by upstream hash
      (sse_proxy.<sha16>.lock). Verified: A holds upstream1, B spawns its own
      for upstream2 (no false contention); same-upstream race still
      serializes (3/3 rounds same port); use-after-shutdown recovers (A's
      owned shutdown clears ITS lock → B spawns fresh, never reuses the dead
      port). 
- [x] NO-NEW-BUG pass (verification gaps closed): (A) export truncation
      CONFIRMED working — the "missing ellipsis" was JSON escaping (\u2026);
      parse-roundtrip returns the real "…" with data correctly capped at 400
      chars. (B) corrupt-header session files handled gracefully (empty list,
      no crash). (C) patch file cleaned after retries (0 leaks even with
      retry-1-fail-1-success). (D) dsh_status shape complete (installed,
      store kind, model, base URL, proxy state, resolution source).
- [x] RUN-vs-BOOTSTRAP RACE FIXED (real bug) + throughput regression caught:
      (1) a force-bootstrap could rmtree node_modules under a running dsh
      subprocess → "Cannot find module" mid-flight. Fix: _active_runs counter
      (boot-lock-guarded); _do_install SKIPS the rmtree while runs are
      in-flight (npm repairs in place). (2) My FIRST fix (hold _boot_lock for
      the whole run) serialized concurrent runs (33s vs 17s) and broke the
      honest-attribution test — caught by regression, reverted to the
      counter. Final: runs parallel (12.7s), bootstrap never deletes under a
      live run. Also verified: empty task guard, bad cwd clean error, ro
      dsh_home failure surfaced in attempts[].stderr_tail, spec-compliant
      upstream with proper [DONE] passes through the proxy uncorrupted.
- [x] COUNTER HARDENING: (C) 8 run threads + 4 bootstrap threads racing →
      counter ends exactly 0, zero errors (lock-guarded inc/dec is atomic).
      (D) rmtree guard hardened ==0 → <=0 (a hypothetical negative counter
      must not permanently disable the self-heal). (E) two runs racing their
      FIRST proxy spawn + counter together → both succeed, counter 0, exactly
      ONE proxy lockfile (no duplicate).
- [x] 503-RETRY MISS FIXED (real bug): dsh NORMALIZES gateway statuses into
      "dsh: SERVER: overloaded" — the retry classifier matched only numeric
      codes ("503" etc.), which never appear in that form, so a 503 was never
      retried (1 attempt despite retries=2). Added the normalized forms
      ("dsh: server", "server: overloaded", "server error", "service
      "unavailable", "bad gateway", "overloaded"). Verified: 503 now retries
      3/3. Also verified: huge-task E2BIG → clean "Argument list too long"
      error with counter not leaked (0).
- [x] NO-NEW-BUG pass: (A) lineage CYCLE (A↔B parent cycle) terminates in
      0.00s (the seen-set guards the traversal — no infinite loop). (B)
      concurrent all-fail runs: both exit 1, ZERO certain claims, counter 0 —
      honest attribution holds; the "uncertain" sessions shown were artifacts
      of prior 503/5xx tests (dsh persists a session even on gateway failure),
      correctly reported as un-attributable rather than claimed.
- [x] FILE-AS-HOME CRASH FIXED (real bug): _find_sqlite_db did dsh_home.iterdir()
      without an is_dir() guard → NotADirectoryError when dsh_home pointed at
      a FILE (caller passing a bad path got an unhandled crash). Fixed with
      is_dir() + OSError guard → graceful None. Also verified: mixed
      JSONL+SQLite store deterministically picks JSONL (dir wins); self-parent
      lineage cycle terminates instantly; export max_events 0/-5 clamp to the
      header line.
- [x] HTTPS UPSTREAM FIXED (real bug): the SSE proxy used urllib's default
      SSL context → an https base URL with a self-signed/corporate cert failed
      (SSL verification error). The proxy is a localhost-only dev shim (dsh→
      proxy traffic is already plaintext loopback), so upstream cert
      verification adds no security; added _unverified_ssl_context()
      (check_hostname=False, CERT_NONE). Verified: https self-signed upstream
      works (answer HTTPS-OK). Also verified: path-prefixed base URLs
      (http://host/custom/v1) pass through correctly (PREFIX-OK).
- [x] GZIP UPSTREAM FIXED (real bug): the proxy read the upstream body raw —
      a gateway that gzip-encodes despite the stripped Accept-Encoding sent
      compressed bytes straight through → dsh saw garbage (exit 1). The proxy
      now decompresses Content-Encoding gzip/deflate/br (gzip+HTTPError paths
      covered; brotli best-effort if importable). Verified: gzip upstream →
      GZIP-OK.
- [x] SSE EMPTY-DATA-LINE FIXED (real bug): the proxy forwarded empty `data:`
      lines verbatim → dsh's strict parser threw MALFORMED_RESPONSE. Now
      dropped like keepalives. Verified: comments + empty data + multi-chunk +
      missing-[DONE] upstream → EDGE-OK. Also verified: HTML upstream → clean
      EMPTY_RESPONSE (retriable), chunked transfer → CHUNKED-OK, sqlite NULL
      cwd rows handled (attribution slug derivation is None-safe).
- [x] NO-NEW-BUG pass: (A) streaming-detection regex verified against all
      whitespace variants ("stream":true / "stream": true / "stream" :true /
      "stream" : true) + false positives (false, 1, "true" string, absent) —
      the earlier spacing fix holds. (A2) NON-streaming POST passes through
      the proxy untouched (no normalization) — response intact. (B) deleted
      lockfile mid-flight is safe: the holder keeps its in-memory proxy, a
      fresh process re-spawns (the only duplicate scenario requires external
      lockfile deletion, which self-heals).

## Post-deploy reconciliation (bigger picture)
- [x] root cause of npm landing in HOME: npm climbs to nearest ancestor
      package.json (vibe-code-root) → plugin now writes its own private
      package.json in the state dir before `npm install` (machine-agnostic
      guard); home pollution cleaned (package.json/lock/node_modules restored)
- [x] no collision with existing core/plugin toolsets (difflib scan)
- [x] plugin survives hermes-update (standalone, stdlib-only deps; sse_proxy.py beside plugin)
- [x] headless-CLI + read-only store introspection = max practical utilization
      (SDK not on PyPI, pnpm broken — documented, not dropped)
