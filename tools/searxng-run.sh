#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Run SearXNG on the plugin pack's runtime interpreter.
#
# WHY THIS EXISTS
# A systemd unit cannot ask "which interpreter is the runtime venv today?" —
# ExecStart is static, and the PM-managed venv path is keyed by an environment
# hash that CHANGES whenever Hermes rebuilds its dependency generation
# (observed: environments/6f60b4fe… → environments/ffa16bbb… in one day). A unit
# that hardcodes that hash keeps running until it is restarted, then fails
# forever with ModuleNotFoundError — the service looks enabled, the port dies,
# and every searxng_* tool silently degrades.
#
# So the unit calls THIS script instead: it resolves the interpreter the same
# way the pack's other tooling does (one resolver, never re-derived), makes sure
# SearXNG's own requirements are importable in that interpreter, and only then
# execs the webapp. `exec` keeps uvicorn/flask as the unit's main PID so systemd
# supervises and signals the right process.
#
# Idempotent and cheap: once the deps import, the check is one subprocess.
#
# Usage:  searxng-run.sh [extra searx args…]
# ──────────────────────────────────────────────────────────────────────
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO_DIR:-$(cd "$SELF_DIR/.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME

SEARX_SRC="${SEARXNG_SRC:-$HOME/searxng/searxng-src}"
SETTINGS="${SEARXNG_SETTINGS:-$SEARX_SRC/searx/settings.yml}"

log() { echo "[searxng-run] $*" >&2; }

# ── Resolve the runtime interpreter (delegates to the single resolver) ──
PY="$(REPO_DIR="$REPO" HERMES_HOME="$HERMES_HOME" bash "$SELF_DIR/hermes-runtime-python.sh" 2>/dev/null)"
if [[ -z "$PY" || ! -x "$PY" ]]; then
    log "could not resolve the Hermes runtime interpreter"
    exit 1
fi

if [[ ! -f "$SETTINGS" ]]; then
    log "no SearXNG settings at $SETTINGS — clone https://github.com/searxng/searxng to $SEARX_SRC"
    exit 1
fi

# ── Ensure SearXNG's requirements are importable in THAT interpreter ──
# Requirements live in the clone, so they follow the source, not this script.
# uv is preferred because the PM venv ships no pip (python -m pip is absent).
if ! "$PY" -c "import searx.webapp" >/dev/null 2>&1; then
    REQ="$SEARX_SRC/requirements.txt"
    [[ -f "$REQ" ]] || { log "no $REQ — cannot install SearXNG deps"; exit 1; }
    log "SearXNG not importable in $PY — installing requirements"
    UV="${UV_BIN:-$HOME/.hermes/bin/uv}"
    if [[ -x "$UV" ]]; then
        "$UV" pip install --python "$PY" -r "$REQ" || { log "uv install failed"; exit 1; }
    elif "$PY" -m pip --version >/dev/null 2>&1; then
        "$PY" -m pip install -r "$REQ" || { log "pip install failed"; exit 1; }
    else
        log "neither uv ($UV) nor pip is available to install SearXNG requirements"
        exit 1
    fi
    "$PY" -c "import searx.webapp" >/dev/null 2>&1 \
        || { log "SearXNG still not importable after install"; exit 1; }
fi

# ── Hand off to the webapp ──
# SEARXNG_SETTINGS_PATH is what searx itself reads; cwd must be the clone so
# relative template/static paths resolve.
log "starting searx.webapp  python=$PY  settings=$SETTINGS"
cd "$SEARX_SRC" || exit 1
export SEARXNG_SETTINGS_PATH="$SETTINGS"
exec "$PY" -m searx.webapp "$@"
