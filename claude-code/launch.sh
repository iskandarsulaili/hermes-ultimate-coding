#!/usr/bin/env bash
# Launch the Hermes MCP bridge with the correct Python interpreter.
#
# The bridge imports hermes-agent, so it MUST run on the interpreter that has
# Hermes and its dependencies installed — normally the venv inside the Hermes
# install, not the system python. Resolution order:
#
#   1. $HERMES_MCP_PYTHON            explicit override
#   2. the PM-managed runtime venv   discovered from the install's own facts.json
#   3. $HERMES_HOME/hermes-agent/venv/bin/python
#   4. $HERMES_AGENT_DIR/venv/bin/python
#   5. python3 on PATH               (works only if hermes-agent is pip-installed)
#
# (2) matters: modern Hermes resolves its dependencies into
# ``<home>/installs/<key>/environments/<gen>/venv`` and the legacy
# ``hermes-agent/venv`` no longer exists, so the old list fell through to the
# *base* interpreter. That one lacks Hermes' own third-party deps (ruamel), and
# the bridge died with "No module named 'ruamel'" for every single tool.
#
# Diagnostics go to stderr; stdout is reserved for the MCP protocol.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
export HERMES_HOME HERMES_AGENT_DIR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="$SCRIPT_DIR/hermes_mcp_bridge.py"

if [ ! -f "$BRIDGE" ]; then
    echo "hermes-mcp: bridge not found at $BRIDGE" >&2
    exit 1
fi

# Discover the PM runtime venv for the hermes-agent tree. The install state is
# keyed by a sha256 of the resolved project root; read facts.json so this stays
# correct across generations instead of hardcoding an environment hash.
pm_venv_python() {
    local root key facts env
    root="$(cd "$HERMES_AGENT_DIR" 2>/dev/null && pwd -P)" || return 1
    key="$(printf '%s' "$root" | sha256sum | cut -c1-16)" || return 1
    facts="$HERMES_HOME/installs/$key/facts.json"
    [ -f "$facts" ] || return 1
    env="$(python3 - "$facts" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    v = d.get("packages", {}).get("venv", {}).get("environment")
    if v:
        print(v)
except Exception:
    pass
PY
)"
    [ -n "$env" ] && [ -x "$env/bin/python" ] && { echo "$env/bin/python"; return 0; }
    return 1
}

pick_python() {
    if [ -n "${HERMES_MCP_PYTHON:-}" ] && [ -x "${HERMES_MCP_PYTHON}" ]; then
        echo "$HERMES_MCP_PYTHON"; return 0
    fi
    local cand
    cand="$(pm_venv_python)" && { echo "$cand"; return 0; }
    for cand in \
        "$HERMES_AGENT_DIR/venv/bin/python" \
        "$HERMES_HOME/hermes-agent/venv/bin/python" \
        "$HERMES_HOME/venv/bin/python"
    do
        [ -x "$cand" ] && { echo "$cand"; return 0; }
    done
    command -v python3 2>/dev/null && return 0
    return 1
}

PYTHON="$(pick_python)" || {
    echo "hermes-mcp: no usable Python interpreter found." >&2
    echo "  Looked for \$HERMES_MCP_PYTHON, the PM runtime venv, $HERMES_AGENT_DIR/venv/bin/python, and python3." >&2
    exit 1
}

# Do NOT let hermes_bootstrap's lazy-install path re-exec this process.
# ``prepare_launch`` restarts onto the PM *store* interpreter whenever the
# install looks mid-update (a stale ``.install.lock`` is enough). That interpreter
# carries only the managed toolchain — not Hermes' dependency set — so the bridge
# died with "No module named 'ruamel'" and every one of its tools reported
# "Hermes bridge unavailable". This launcher already selected a complete,
# dependency-bearing interpreter, so the re-exec has nothing to add.
export HERMES_DISABLE_LAZY_INSTALLS=1

echo "hermes-mcp: python=$PYTHON HERMES_HOME=$HERMES_HOME" >&2
exec "$PYTHON" "$BRIDGE" "$@"
