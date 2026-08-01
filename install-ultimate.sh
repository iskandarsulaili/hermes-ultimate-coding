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

for plugin in hermes-graphify hermes-semble hermes-lsp hermes-effect-engine hermes-tps hermes-orchestra hermes-searxng hermes-cloakbrowser hermes-moa-trigger _shared; do
    src="$REPO_DIR/plugins/$plugin"
    dst="$PLUGIN_DIR/$plugin"
    if [ -d "$src" ]; then
        rm -rf "$dst"
        cp -r "$src" "$dst"
        echo "  ✓ $plugin"
    else
        warn "  Plugin $plugin not found in repo — skipping"
    fi
done

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
for plugin in hermes-graphify hermes-semble hermes-lsp hermes-effect-engine hermes-tps hermes-orchestra hermes-searxng hermes-cloakbrowser hermes-moa-trigger; do
    init="$PLUGIN_DIR/$plugin/__init__.py"
    if [ -f "$init" ]; then
        if "$PYTHON" -c "import py_compile; py_compile.compile('$init', doraise=True)" 2>/dev/null; then
            echo "  ✓ $plugin compiles"
        else
            err "  $plugin has syntax errors!"
        fi
    else
        warn "  $plugin/__init__.py not found"
    fi
done

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
