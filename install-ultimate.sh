#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# hermes-ultimate-coding — one-shot install wizard
# Installs all plugins, dependencies, and verifies the setup.
# Target: new Hermes user who wants plugin usage indicators,
# graphify, semble, LSP, and effect engine.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="https://github.com/iskandarsulaili/hermes-ultimate-coding.git"
REPO_DIR="${HERMES_HOME:-$HOME/.hermes}/hermes-ultimate-coding"
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins"
VENV_DIR="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv"

# Colours
BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Colour

info()  { echo -e "${GREEN}${BOLD}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}${BOLD}[WARN]${NC}  $1"; }
err()   { echo -e "${RED}${BOLD}[FAIL]${NC}  $1"; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   hermes-ultimate-coding — Install Wizard        ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── Step 1: Check Hermes home ──────────────────────────────────────
if [ ! -d "$HOME/.hermes" ]; then
    err "~/.hermes/ not found. Install Hermes Agent first:"
    echo "  pip install hermes-agent"
    exit 1
fi
info "Hermes home: $HOME/.hermes"

# ── Step 2: Clone/update repo ──────────────────────────────────────
if [ -d "$REPO_DIR/.git" ]; then
    info "Updating existing clone at $REPO_DIR"
    git -C "$REPO_DIR" pull --ff-only 2>&1 | sed 's/^/  /'
else
    info "Cloning into $REPO_DIR"
    git clone "$REPO_URL" "$REPO_DIR" 2>&1 | sed 's/^/  /'
fi

# ── Step 3: Install plugins ────────────────────────────────────────
info "Installing plugins to $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"

for plugin_dir in "$REPO_DIR"/plugins/*/; do
    plugin="$(basename "$plugin_dir")"
    src="$REPO_DIR/plugins/$plugin"
    dst="$PLUGIN_DIR/$plugin"
    # Discover every plugin on disk — never a hardcoded list. A hand-maintained
    # list had silently drifted to 11 of 17 plugins, so 7 (agents, anchored,
    # codegraph, codegraph-context, dsh, memory-tdai, vault) were missing from
    # every fresh install while the pack advertised them.
    [ -d "$src" ] || continue
    rm -rf "$dst"
    cp -r "$src" "$dst"
    echo "  ✓ $plugin"
done

PLUGIN_COUNT="$(find "$REPO_DIR"/plugins -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
INSTALLED_COUNT="$(find "$PLUGIN_DIR" -mindepth 1 -maxdepth 1 -type d \
    -exec test -e '{}/plugin.yaml' -o -e '{}/__init__.py' \; -print 2>/dev/null | wc -l | tr -d ' ')"
info "Installed $PLUGIN_COUNT plugin directories ($INSTALLED_COUNT with an entrypoint)"

# ── Step 4: Install Python dependencies ────────────────────────────
PYTHON="${VENV_DIR}/bin/python3"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
    info "No venv found at $VENV_DIR — using system python"
fi

info "Installing Python dependencies..."
DEPS=(
    "graphifyy>=0.9.15"
    "tree-sitter"
    "semble>=0.3.0"
)
for dep in "${DEPS[@]}"; do
    echo -n "  $dep ... "
    # Map pip package name to importable module name (tree-sitter vs tree_sitter)
    mod=$(echo "$dep" | cut -d'[' -f1 | cut -d'>' -f1 | cut -d'=' -f1 | tr -d ' ' | tr '-' '_')
    if "$PYTHON" -c "import $mod" 2>/dev/null; then
        echo "already installed"
    else
        "$PYTHON" -m pip install "$dep" 2>&1 | tail -1
    fi
done

# ── Step 5: Verify plugins load ────────────────────────────────────
info "Verifying plugin imports..."
VERIFY_FAILED=0
for plugin_dir in "$PLUGIN_DIR"/*/; do
    plugin="$(basename "$plugin_dir")"
    init="$PLUGIN_DIR/$plugin/__init__.py"
    [ -f "$init" ] || continue
    if "$PYTHON" -c "import py_compile; py_compile.compile('$init', doraise=True)" 2>/dev/null; then
        echo "  ✓ $plugin compiles"
    else
        err "  $plugin has syntax errors!"
        VERIFY_FAILED=1
    fi
done

# Entrypoints discoverable — a plugin dir with neither plugin.yaml nor
# __init__.py is inert (never registered), so call that out loudly.
for plugin_dir in "$PLUGIN_DIR"/*/; do
    plugin="$(basename "$plugin_dir")"
    if [ ! -f "$PLUGIN_DIR/$plugin/plugin.yaml" ] && [ ! -f "$PLUGIN_DIR/$plugin/__init__.py" ]; then
        warn "  $plugin has no plugin.yaml or __init__.py — will not be registered"
        VERIFY_FAILED=1
    fi
done

if [ "$VERIFY_FAILED" -eq 0 ]; then
    echo "  ✓ every installed plugin compiles and has an entrypoint"
fi

# ── Step 5b: Enable every installed plugin ─────────────────────────
# Copying files is not installation: a plugin that is not in
# `plugins.enabled` is never registered, so its tools never appear. Do this
# with `hermes plugins enable` when the CLI is available (it updates config +
# state correctly), and fall back to a direct, idempotent YAML edit.
info "Enabling plugins..."
HERMES_BIN="${HERMES_BIN:-$(command -v hermes || true)}"
ENABLED_OK=0
if [ -n "$HERMES_BIN" ] && [ -x "$HERMES_BIN" ]; then
    for plugin_dir in "$PLUGIN_DIR"/*/; do
        plugin="$(basename "$plugin_dir")"
        [ -f "$PLUGIN_DIR/$plugin/plugin.yaml" ] || continue
        case "$plugin" in _shared) continue ;; esac
        if "$HERMES_BIN" plugins enable "$plugin" >/dev/null 2>&1; then
            echo "  ✓ enabled $plugin"
            ENABLED_OK=1
        else
            warn "  could not enable $plugin via CLI (will retry with config edit)"
        fi
    done
fi

if [ "$ENABLED_OK" -eq 0 ]; then
    warn "  hermes CLI unavailable — enabling via config.yaml edit"
    CFG="${HERMES_HOME:-$HOME/.hermes}/config.yaml"
    if [ -f "$CFG" ]; then
        "$PYTHON" - "$CFG" "$PLUGIN_DIR" <<'PYEOF'
import os, re, sys

# Surgical, comment-preserving edit. A yaml.safe_load/safe_dump round-trip
# silently strips every comment in config.yaml (~4 KB of documentation here),
# so operate on the text instead: append only the names that are missing.
cfg_path, plugin_dir = sys.argv[1], sys.argv[2]

names = []
for d in sorted(os.listdir(plugin_dir)):
    full = os.path.join(plugin_dir, d)
    if d == "_shared" or not os.path.isdir(full):
        continue
    if os.path.isfile(os.path.join(full, "plugin.yaml")):
        names.append(d)

with open(cfg_path, encoding="utf-8") as f:
    text = f.read()

lines = text.split("\n")
# Locate `plugins:` then its `  enabled:` list, collecting existing entries.
try:
    pstart = next(i for i, l in enumerate(lines) if re.match(r"^plugins:\s*$", l))
except StopIteration:
    print("  ! no top-level 'plugins:' section — add one, then re-run")
    sys.exit(0)

estart = None
for i in range(pstart + 1, len(lines)):
    if re.match(r"^[A-Za-z_]", lines[i]):        # next top-level key
        break
    if re.match(r"^\s+enabled:\s*$", lines[i]):
        estart = i
        break

if estart is None:
    print("  ! no 'plugins.enabled' list found — enable manually: hermes plugins enable <name>")
    sys.exit(0)

i = estart + 1
existing, insert_at = set(), estart + 1
while i < len(lines) and re.match(r"^\s+-\s+", lines[i]):
    existing.add(lines[i].split("-", 1)[1].strip())
    insert_at = i + 1
    i += 1

added = [n for n in names if n not in existing]
if added:
    lines[insert_at:insert_at] = ["    - %s" % n for n in added]
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
print("  ✓ enabled via config: %d new (%s)" % (len(added), ", ".join(added) or "none"))
PYEOF
    else
        err "  no config.yaml at $CFG — enable plugins manually with: hermes plugins enable <name>"
    fi
fi

# ── Step 6: Patch semble file_walker for PermissionError handling ──
info "Patching semble file_walker to handle PermissionError..."
SEMBLE_WALKER="$("$PYTHON" -c "import semble.index.file_walker; print(semble.index.file_walker.__file__)" 2>/dev/null || true)"
if [ -n "$SEMBLE_WALKER" ] && [ -f "$SEMBLE_WALKER" ]; then
    # P1: Wrap _load_ignore_for_dir in try/except PermissionError
    if grep -q 'def _load_ignore_for_dir' "$SEMBLE_WALKER"; then
        # Check if already patched
        if grep -q 'except PermissionError' "$SEMBLE_WALKER" 2>/dev/null; then
            echo "  ✓ already patched"
        else
            # Apply both patches using python to be safe
            "$PYTHON" -c "
import re
with open('$SEMBLE_WALKER') as f:
    src = f.read()

# Patch 1: _load_ignore_for_dir — wrap entire body in try/except
src = src.replace(
    'def _load_ignore_for_dir(directory: Path) -> GitIgnoreSpec | None:\n    \"\"\"Loads a gitignore and sembleignore for a dir.\"\"\"\n    gitignore = directory / \".gitignore\"\n    sembleignore = directory / \".sembleignore\"\n\n    lines = []\n    if gitignore.is_file():\n        lines.extend(gitignore.read_text(encoding=\"utf-8\", errors=\"ignore\").splitlines())\n    if sembleignore.is_file():\n        lines.extend(sembleignore.read_text(encoding=\"utf-8\", errors=\"ignore\").splitlines())\n    if lines:\n        return GitIgnoreSpec.from_lines(lines)\n    return None',
    'def _load_ignore_for_dir(directory: Path) -> GitIgnoreSpec | None:\n    \"\"\"Loads a gitignore and sembleignore for a dir.\"\"\"\n    try:\n        gitignore = directory / \".gitignore\"\n        sembleignore = directory / \".sembleignore\"\n\n        lines = []\n        if gitignore.is_file():\n            lines.extend(gitignore.read_text(encoding=\"utf-8\", errors=\"ignore\").splitlines())\n        if sembleignore.is_file():\n            lines.extend(sembleignore.read_text(encoding=\"utf-8\", errors=\"ignore\").splitlines())\n        if lines:\n            return GitIgnoreSpec.from_lines(lines)\n    except PermissionError:\n        pass\n    return None'
)

# Patch 2: _walk — wrap iterdir() in try/except PermissionError
src = src.replace(
    '    for item in sorted(directory.iterdir()):',
    '    try:\n        entries = sorted(directory.iterdir())\n    except PermissionError:\n        return\n\n    for item in entries:'
)

with open('$SEMBLE_WALKER', 'w') as f:
    f.write(src)
print('  ✓ patched')
" 2>&1
        fi
    fi
else
    warn "  Could not find semble.file_walker — skipping patch. Install semble first."
fi

# ── Step 7: Check plugin usage tracking in Hermes core ─────────────
CORE_PLUGIN_USAGE="$HOME/.hermes/hermes-agent/tools/plugin_usage.py"
if [ -f "$CORE_PLUGIN_USAGE" ]; then
    echo "  ✓ plugin_usage.py exists (core tracking)"
else
    warn "  plugin_usage.py not found — Hermes core may not have tracking installed"
    warn "  Make sure you're running hermes-agent >= 1.0.3 or apply the patches manually"
fi

# ── Step 8: Full-stack survival (core fixes + plugin sync + services) ──
# This is the single entry point that makes the whole stack survive a reboot,
# `hermes update`, and a fresh machine: it syncs plugin files repo->install,
# re-applies the fork-local core fixes, applies the SearXNG engine tuning, and
# schedules itself (@reboot + daily). Idempotent.
info "Securing the full stack (survive.sh)..."
SURVIVE="$REPO_DIR/tools/survive.sh"
if [ -f "$SURVIVE" ]; then
    chmod +x "$SURVIVE"
    REPO_DIR="$REPO_DIR" HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
        bash "$SURVIVE" 2>&1 | sed 's/^/  /'
else
    warn "  survive.sh not found at $SURVIVE — falling back to core-fix self-heal only"
    SELF_HEAL="$REPO_DIR/tools/self-heal-hermes-core-fixes.sh"
    if [ -f "$SELF_HEAL" ]; then
        chmod +x "$SELF_HEAL"
        bash "$SELF_HEAL" "${HERMES_HOME:-$HOME/.hermes}/hermes-agent" 2>&1 | sed 's/^/  /'
    else
        warn "  no self-heal script either — core fixes NOT applied"
    fi
fi

# ── Done ────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   Installation complete!                         ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  ${BOLD}Next steps:${NC}"
echo "  1. Restart Hermes (or start a new session)"
echo "  2. The status bar will show plugin usage indicators:"
echo "     🔧 LSP ⚡ Effect 🕸️ Graphify 🔍 Semble"
echo "  3. Tools are registered automatically — call them normally"
echo "  4. Graphify auto-builds on session start and auto-updates on file changes"
echo ""
echo "  ${BOLD}Try it:${NC}"
echo "    -> Ask: 'audit this code with LSP'"
echo "    -> Ask: 'explain how the auth module connects to the database'"
echo "       (triggers graphify_query — graph is already built)"
echo ""
