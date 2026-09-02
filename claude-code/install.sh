#!/usr/bin/env bash
# Install the Hermes → Claude Code MCP bridge.
#
# Idempotent: safe to re-run. Registers the bridge as an MCP server for Claude
# Code without touching your Hermes configuration.
set -uo pipefail

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}${BOLD}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}${BOLD}[WARN]${NC} $1"; }
err()  { echo -e "${RED}${BOLD}[FAIL]${NC} $1"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"

echo ""
echo -e "${BOLD}Hermes → Claude Code MCP bridge — installer${NC}"
echo ""

# ---- preflight -----------------------------------------------------------
fail=0

if [ -d "$HERMES_HOME" ]; then
    info "Hermes home: $HERMES_HOME"
else
    err "Hermes home not found: $HERMES_HOME"
    echo "     The bridge serves the real Hermes plugin registry, so Hermes must be installed."
    echo "     Install it first, or set HERMES_HOME to an existing install."
    fail=1
fi

if [ -d "$HERMES_AGENT_DIR" ]; then
    info "hermes-agent: $HERMES_AGENT_DIR"
else
    err "hermes-agent not found: $HERMES_AGENT_DIR"
    fail=1
fi

PYTHON=""
for cand in "${HERMES_MCP_PYTHON:-}" "$HERMES_AGENT_DIR/venv/bin/python" "$(command -v python3 || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && { PYTHON="$cand"; break; }
done
if [ -n "$PYTHON" ]; then
    info "Python: $PYTHON ($("$PYTHON" --version 2>&1))"
else
    err "No usable Python interpreter found"
    fail=1
fi

if command -v claude >/dev/null 2>&1; then
    info "Claude Code CLI: $(command -v claude)"
else
    warn "Claude Code CLI not on PATH — you can still register the server manually (see below)"
fi

[ "$fail" -ne 0 ] && { echo ""; err "Preflight failed; nothing was changed."; exit 1; }

# ---- verify the bridge actually loads ------------------------------------
echo ""
echo "Verifying the bridge can load your plugins..."
if OUT="$("$REPO_DIR/claude-code/launch.sh" --selftest 2>&1)"; then
    echo "$OUT" | grep -E '^(plugins|tools listed)' | sed 's/^/     /'
    info "Bridge loads cleanly"
else
    err "Bridge selftest failed:"
    echo "$OUT" | tail -20 | sed 's/^/     /'
    exit 1
fi

# ---- register with Claude Code -------------------------------------------
echo ""
if command -v claude >/dev/null 2>&1; then
    if claude mcp list 2>/dev/null | grep -q '^hermes\b'; then
        warn "An MCP server named 'hermes' is already registered — leaving it as is."
        echo "     Re-register with:  claude mcp remove hermes && $0"
    else
        if claude mcp add hermes --scope user -- "$REPO_DIR/claude-code/launch.sh"; then
            info "Registered 'hermes' with Claude Code (user scope)"
        else
            warn "Automatic registration failed — register manually (below)"
        fi
    fi
fi

cat <<TXT

${BOLD}Manual registration${NC} (if the automatic step did not run):

  claude mcp add hermes --scope user -- $REPO_DIR/claude-code/launch.sh

${BOLD}Or as a plugin${NC} (also installs the bundled skills):

  /plugin marketplace add iskandarsulaili/hermes-ultimate-coding
  /plugin install hermes-ultimate-coding

${BOLD}Verify inside Claude Code${NC}:

  /mcp                      # 'hermes' should be connected
  Ask: "list the hermes tools you have"

${BOLD}Scoping${NC} (optional environment variables):

  HERMES_MCP_TOOLSETS=semble,lsp        expose only these toolsets
  HERMES_MCP_EXCLUDE_TOOLS=tool_a,tool_b
  HERMES_MCP_DEBUG=1                    verbose stderr tracing

TXT
info "Done."
