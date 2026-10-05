#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Print the interpreter the plugin pack should run Python tooling under.
#
# Hermes runs plugins inside a PM-managed uv venv, NOT a repo venv and NOT
# the base toolchain interpreter. That venv is NOT simply "any environment
# under installs/": a second, dependency-less environment exists there too,
# so `ls ... | head -1` can pick one that cannot import the pack's own
# dependencies (observed: no networkx, no semble) and every probe run
# against it reports false "not installed" failures.
#
# Resolution delegates to claude-code/launch.sh's own pm_venv_python(), which
# reads facts.json for the selected environment, so there is exactly ONE
# resolver and the docs can never drift from what the bridge actually runs.
#
# Usage:  "$(tools/hermes-runtime-python.sh)" claude-code/test_coverage.py
# ──────────────────────────────────────────────────────────────────────
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO_DIR:-$(cd "$SELF_DIR/.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
LAUNCH="$REPO/claude-code/launch.sh"

# Preferred: the launcher's resolver (facts.json keyed by the agent tree).
if [ -f "$LAUNCH" ]; then
    resolved="$(
        HERMES_HOME="$HERMES_HOME" HERMES_AGENT_DIR="$HERMES_AGENT_DIR" \
        bash -c '
            set -uo pipefail
            # shellcheck disable=SC1090
            . "'"$LAUNCH"'" >/dev/null 2>&1 || exit 1
            declare -F pm_venv_python >/dev/null || exit 1
            pm_venv_python
        ' 2>/dev/null
    )"
    if [ -n "$resolved" ] && [ -x "$resolved" ]; then
        printf '%s\n' "$resolved"
        exit 0
    fi
fi

# Fallback: read facts.json directly (same contract as pm_venv_python).
resolved="$(python3 - "$HERMES_HOME" "$HERMES_AGENT_DIR" <<'PY' 2>/dev/null || true
import hashlib, json, os, sys
home, agent = sys.argv[1], sys.argv[2]
root = os.path.realpath(agent)
key = hashlib.sha256(root.encode()).hexdigest()[:16]
try:
    with open(os.path.join(home, "installs", key, "facts.json")) as fh:
        env = json.load(fh).get("packages", {}).get("venv", {}).get("environment")
except Exception:
    env = None
# facts.json normally holds the ENV DIR (.../venv), so the interpreter is
# <env>/bin/python. Tolerate it already holding the interpreter path itself:
# appending blindly then yields .../venv/venv/bin/python, a 127 that looks like
# "Hermes is broken" rather than a caller bug (this exact double-append happened
# while probing the pack by hand).
if env and env.endswith(os.path.join("bin", "python")):
    cand = env
elif env:
    cand = os.path.join(env, "bin", "python")
else:
    cand = None
if cand and os.path.isfile(cand):
    print(cand)
PY
)"
if [ -n "$resolved" ]; then
    printf '%s\n' "$resolved"
    exit 0
fi

# Last resort: the legacy repo venv, then PATH python3. Reported on stderr so a
# caller can tell this is the degraded path rather than a real resolution.
for cand in "$HERMES_AGENT_DIR/venv/bin/python" "$HERMES_AGENT_DIR/.venv/bin/python"; do
    if [ -x "$cand" ]; then
        printf '%s\n' "$cand"
        exit 0
    fi
done
echo "hermes-runtime-python: no PM runtime venv found (facts.json missing?); falling back to python3" >&2
command -v python3
