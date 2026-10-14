#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# hermes-rea bootstrap — engines + MCP registration + agent skill.
#
# REA (reverse-engineer-anything) is the pack's 18th plugin. The plugin alone
# gives Hermes 54 task-shaped tools; its MCP server adds the full ~207-tool
# surface, and both talk to real engines (Ghidra, JADX, Rizin, apktool, tshark,
# binwalk/unblob, sigrok, WABT, pwntools, mitmproxy, frida, gdb, wakaru, .NET).
#
# A fresh machine would otherwise get the plugin with none of that: the engines
# are not pip/npm packages and the MCP entry is config, not code. This script is
# the single idempotent entry point for both, so install-ultimate.sh and
# survive.sh cannot drift from each other.
#
# The plugin is stdlib-only by design, so this runs under ANY python3 — it never
# needs the PM runtime venv (and therefore cannot fail because that venv moved).
#
# Usage:  rea-bootstrap.sh [--check] [--mcp-only|--engines-only] [--install-skill]
#   --check          report only; exit 1 when something is missing
#   --mcp-only       ensure the Hermes MCP entry + skill (fast, no downloads)
#   --engines-only   bootstrap engines, skip MCP
#   --install-skill  also (re)install REA's agent skill into $HERMES_HOME/skills
#
# Env:  HERMES_REA_ENGINES=auto|check|skip   (default: check — never a 550 MB
#       Ghidra download on a boot path; install-ultimate.sh passes auto)
# ──────────────────────────────────────────────────────────────────────
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO_DIR:-$(cd "$SELF_DIR/.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/hermes-rea"
ENGINE_SCRIPT="$SELF_DIR/rea-install-engines.sh"

CHECK=0
DO_MCP=1
DO_ENGINES=1
INSTALL_SKILL=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --mcp-only) DO_ENGINES=0 ;;
        --engines-only) DO_MCP=0 ;;
        --install-skill) INSTALL_SKILL=1 ;;
        *) echo "rea-bootstrap: unknown argument $arg" >&2; exit 2 ;;
    esac
done

ENGINE_MODE="${HERMES_REA_ENGINES:-check}"
if [ "$CHECK" = "1" ]; then ENGINE_MODE="check"; fi

PY=""
if [ -x "$REPO/tools/hermes-runtime-python.sh" ]; then
    PY="$(bash "$REPO/tools/hermes-runtime-python.sh" 2>/dev/null || true)"
fi
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "rea-bootstrap: no python3 available" >&2
    exit 2
fi

fails=0
echo "[rea] plugin=$PLUGIN_DIR python=$PY engines=$ENGINE_MODE check=$CHECK"

# ── 1. Engines ────────────────────────────────────────────────────────
if [ "$DO_ENGINES" = "1" ]; then
    if [ ! -f "$ENGINE_SCRIPT" ]; then
        echo "  ! engine bootstrap missing: $ENGINE_SCRIPT"
        fails=$((fails+1))
    else
        missing="$("$PY" - "$PLUGIN_DIR" <<'PY' 2>/dev/null || echo "?"
import importlib.util, json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "__init__.py"
# The plugin imports its pack-internal helper (`_shared.deps`), so the plugins
# directory itself must be importable — exec_module alone fails otherwise and the
# bootstrap would report a false "MCP not registered".
sys.path.insert(0, str(p.parent.parent))
if not p.is_file():
    print("NO_PLUGIN"); raise SystemExit
spec = importlib.util.spec_from_file_location("hermes_rea", p)
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except Exception as exc:
    print("IMPORT_FAIL:%s" % exc); raise SystemExit
rep = m._engine_report()
miss = [r["engine"] for r in rep if not r["ready"]]
print(json.dumps({"ready": sum(1 for r in rep if r["ready"]), "total": len(rep), "missing": miss}))
PY
)"
        echo "  engines: $missing"
        case "$missing" in
            *NO_PLUGIN*) echo "  ! plugin not installed at $PLUGIN_DIR"; fails=$((fails+1)) ;;
            *IMPORT_FAIL*) echo "  ! plugin failed to import"; fails=$((fails+1)) ;;
            *)
                if echo "$missing" | grep -q '"missing": \[\]'; then
                    echo "  ok      every engine ready"
                elif [ "$ENGINE_MODE" = "auto" ]; then
                    echo "  bootstrapping missing engines (downloads on first run)..."
                    bash "$ENGINE_SCRIPT" 2>&1 | tail -20
                    after="$("$PY" - "$PLUGIN_DIR" <<'PY' 2>/dev/null || echo "?"
import importlib.util, json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "__init__.py"
sys.path.insert(0, str(p.parent.parent))
spec = importlib.util.spec_from_file_location("hermes_rea", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
rep = m._engine_report()
print(json.dumps({"ready": sum(1 for r in rep if r["ready"]), "total": len(rep),
                  "missing": [r["engine"] for r in rep if not r["ready"]]}))
PY
)"
                    echo "  engines after bootstrap: $after"
                    echo "$after" | grep -q '"missing": \[\]' || { echo "  ! engines still incomplete"; fails=$((fails+1)); }
                elif [ "$ENGINE_MODE" = "skip" ]; then
                    echo "  skipped (HERMES_REA_ENGINES=skip) — run: bash $ENGINE_SCRIPT"
                else
                    echo "  ! engines missing — run: bash $ENGINE_SCRIPT"
                    fails=$((fails+1))
                fi
                ;;
        esac
    fi
fi

# ── 2. MCP entry (+ optional skill) ───────────────────────────────────
if [ "$DO_MCP" = "1" ]; then
    if [ "$CHECK" = "1" ]; then
        state="$("$PY" - "$PLUGIN_DIR" <<'PY' 2>/dev/null || echo "?"
import importlib.util, json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "__init__.py"
sys.path.insert(0, str(p.parent.parent))

spec = importlib.util.spec_from_file_location("hermes_rea", p)
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except Exception as exc:
    print(json.dumps({"error": str(exc)})); raise SystemExit
print(m._h_mcp_status({}))
PY
)"
        echo "  mcp: $state"
        echo "$state" | grep -qE '"registered": *(true|"true")' || { echo "  ! REA MCP server not registered"; fails=$((fails+1)); }
    else
        if [ "$INSTALL_SKILL" = "1" ]; then APPLY_SKILL="True"; else APPLY_SKILL="False"; fi
        out="$("$PY" - "$PLUGIN_DIR" "$APPLY_SKILL" <<'PY' 2>&1 | tail -12
import importlib.util, json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "__init__.py"
install_skill = sys.argv[2] == "True"
sys.path.insert(0, str(p.parent.parent))
spec = importlib.util.spec_from_file_location("hermes_rea", p)
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except Exception as exc:
    print(json.dumps({"ok": False, "error": "plugin import failed: %s" % exc})); raise SystemExit
res = json.loads(m._h_mcp_setup({"apply": True, "install_skill": install_skill, "tune": True}))
print(json.dumps({k: res.get(k) for k in ("ok", "registered", "verified", "tuned", "skill", "wrapper", "error")}))
PY
)"
        echo "  mcp: $out"
        echo "$out" | grep -q '"ok": *true' || { echo "  ! MCP registration/verification failed"; fails=$((fails+1)); }
    fi
fi

if [ "$fails" -gt 0 ]; then
    echo "[rea] RESULT: $fails item(s) need attention"
    [ "$CHECK" = "1" ] && exit 1
    exit 0
fi
echo "[rea] RESULT: REA stack complete (engines + MCP entry verified)"
exit 0
