# Changelog

Releases are git tags. Behaviour in this file describes what was **verified**, not merely
what was written — where a fix was found to have failed, that is recorded rather than hidden.

## v1.3.0 — 2026-09-19

The durability and honesty release. 77 commits since v1.2.0; 16 plugin toolsets, 103 tools.

### Durability: survives reboot, `hermes update`, and a fresh machine

- **`tools/survive.sh`** — one idempotent entry point: syncs plugin files repo → install,
  re-applies the fork-local Hermes core fixes, applies the SearXNG engine tuning, ensures the
  memory gateway, enables services at boot, and installs its own schedule (`@reboot` + daily)
  behind a `flock` overlap guard. `--check` reports only and exits non-zero on gaps.
- **`tools/self-heal-hermes-core-fixes.sh`** — re-applies three fork-local core fixes that
  upstream still lacks (the plugin-toolset validation cache, the raw-ops outcome webhook, and
  the `cron_providers` exclusion). Detects by injected marker, not commit SHA, and verifies the
  **outcome** — `git cherry-pick --no-commit` can return 0 without applying anything.
- **`systemd/tdai-gateway.service`** — the four-layer memory gateway is now supervised
  (`Restart=always`, enabled at boot with linger). Previously a crash left port 8420 dead until
  the next daily run.
- **`install-ultimate.sh`** — derives the plugin list dynamically (it had a hardcoded list that
  missed 7 plugins), verifies every plugin, **enables** them (copying files alone left Hermes
  ignoring them), and calls survive.sh so a fresh machine is ready in one step.

### Fixed: checks that reported success while doing nothing

The dominant defect class of this release — a check verifying a proxy instead of the real thing.

- `lsp_verify` returned `passed: true` with errors present.
- `effect_run` counted an unrecognised operation as a **completed step** (`success: true`,
  `errors: 0` for a chain that did nothing).
- `cgc_analyze` reported `success: true` while the payload carried a query error.
- `tdai_status` / `agents_status` / `searxng_status` reported `ready: false` while each was
  fully usable — they reported a flag cached at import instead of probing.
- `survive.sh`'s gateway warm-up printed `READY` for a gateway that died the instant the script
  returned (the plugin's atexit shutdown killed it).
- The repo's committed plugin inventory said *16 plugins / 94 tools* for weeks while its own
  cron logged `in sync` — the cron only mirrored when the **live** file changed, never when the
  repo copy **differed**.
- `hermes-agents` treated an unreachable GitHub as fatal, reporting the plugin broken while all
  three repos were present and usable locally.

### Fixed: guards that could not fire

- **Semble's pre-flight cap** was set from intuition, not measurement, and could never trip for
  the tree it was written to protect. Measured cost is **not** a clean function of file count
  (a 116k-file tree indexes in 100 s while a 97k-file tree fails at 144 s), so the guard now
  hard-refuses only above 150,000 files and **warns** in the 90k–150k grey zone. Refusing a tree
  that would have worked is its own defect.
- **Graphify** reported a failed build forever, and auto-built `$HOME` (which cannot finish in
  the budget). Now retried after a cooldown and refused up front with an actionable message.
- **SearXNG returned 0 results for every query** while reporting HTTP 200 — stock settings left
  five general engines on and most were CAPTCHA'd. Tuned to engines that measurably yield
  (benchmarked **0 → 449 results across 8 queries, 0 zero-yield**).

### Fixed: data and state integrity

- The vault plugin cached **negative** collection lookups permanently, so a first call landing
  before the collection existed would break every later search for the process's lifetime.
- The plugin sync did `rm -rf` + copy, which destroyed install-only generated files
  (24 MB of `node_modules` and `package.json`).
- The vault plugin no longer downloads models. Per the standing constraint it serves keyword
  search and reports `mode: keyword`, so the degradation is visible rather than silent;
  `HERMES_VAULT_ALLOW_MODEL_DOWNLOAD=1` opts in.

### Fixed: correctness of the automation itself

- Self-heal used a bare `git commit`, which sweeps a **parallel session's staged work** into its
  commit — reproduced: the commit ended up containing only the other session's file, so the fix
  silently did not land. Now `git commit --only`.
- Self-heal re-applied the fixes but never **reloaded the gateway**, so after `hermes update`
  the running process kept serving old bytecode. Now signals a graceful reload, and exports
  `XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS` so it also works from cron (where
  `systemctl --user` otherwise fails with "No medium found").
- The schedule check matched the *filename* rather than the *path*, so a stale absolute path
  looked healthy while the job ran nothing.
- `plugin_usage.py` tracked 8 of 16 toolsets while the status bar tracked 15 — calls to
  codegraph, vault, agents, memory-tdai, dsh, anchored and others were never counted.

### Dependencies

Upgraded, and verified by running the tools afterwards: `semble` 0.5.2 → **0.6.0**,
`codegraphcontext` 0.5.3 → **0.6.13**, `graphifyy` 0.9.26 → **0.9.64**. `qmd` 2.8.3 and
`omniroute` 3.8.50 were already current. See `docs/VERSION_MATRIX.md` for the two recorded
caveats (a declared `protobuf` conflict that is not a runtime break, and a stale user-site
`graphify` CLI copy).

### Known limits (deliberate, not defects)

- Vault semantic search needs an embedder + reranker that are **not installed** by choice;
  keyword search is the agreed fallback.
- `tdai`'s `gateway_script` reports `null` (cosmetic); the gateway is served on :8420.
- The stale user-site `graphify` CLI copy is left in place, documented.
