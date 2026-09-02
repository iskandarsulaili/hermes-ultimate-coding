#!/usr/bin/env bash
# Launch the Hermes MCP bridge with the correct Python interpreter.
#
# The bridge imports hermes-agent, so it MUST run on the interpreter that has
# Hermes and its dependencies installed — normally the venv inside the Hermes
# install, not the system python. Resolution order:
#
#   1. $HERMES_MCP_PYTHON            explicit override
#   2. $HERMES_HOME/hermes-agent/venv/bin/python
#   3. $HERMES_AGENT_DIR/venv/bin/python
#   4. python3 on PATH               (works only if hermes-agent is pip-installed)
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

pick_python() {
    if [ -n "${HERMES_MCP_PYTHON:-}" ] && [ -x "${HERMES_MCP_PYTHON}" ]; then
        echo "$HERMES_MCP_PYTHON"; return 0
    fi
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
    echo "  Looked for \$HERMES_MCP_PYTHON, $HERMES_AGENT_DIR/venv/bin/python, and python3." >&2
    exit 1
}

echo "hermes-mcp: python=$PYTHON HERMES_HOME=$HERMES_HOME" >&2
exec "$PYTHON" "$BRIDGE" "$@"
