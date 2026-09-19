#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# hermes-ultimate-coding — self-heal / re-apply fork-local Hermes core fixes.
#
# Hermes on this box is a git fork of NousResearch/hermes-agent. `hermes update`
# and parallel sessions run `git reset --hard origin/main`, which DISCARDS the
# local commits below again and again. This script re-applies them.
#
#   1. cli.py — "validate plugin toolsets against the persisted plugin-key cache"
#      Without it, every enabled plugin toolset (agents, lsp, vault, searxng, ...)
#      is falsely printed as:
#          Warning: Unknown toolsets: agents, anchored, cloakbrowser, ...
#      The tools still load; it is pure warning noise on every new session.
#
#   2. gateway/platforms/webhook.py — the raw-ops caller contract
#      (_post_outcome_webhook / is_steer / RESUME-DELIVERY FIX).
#
# Detection is by injected marker comment, not commit SHA, so a history rewrite
# that preserves the code stays "applied" and a reset that reverts the file is
# re-fixed. Idempotent; safe on every boot / before any session.
#
# Usage:  self-heal-hermes-core-fixes.sh [REPO_DIR]
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="${1:-${HERMES_HOME:-$HOME/.hermes}/hermes-agent}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Interpreter for the in-script patch helper: the Hermes venv when present.
PYTHON_BIN="${HERMES_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for cand in "$REPO/venv/bin/python" "$REPO/.venv/bin/python"; do
        [[ -x "$cand" ]] && { PYTHON_BIN="$cand"; break; }
    done
fi
[[ -n "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3 || true)"
[[ -n "$PYTHON_BIN" ]] || { echo "[self-heal] no python interpreter found" >&2; exit 1; }

CLI_MARKER="joined yet, so validate against the persisted plugin toolset-key cache too"
WEBHOOK_MARKERS=( "_post_outcome_webhook" "is_steer" "RESUME-DELIVERY FIX" )
# (3) hermes_cli/plugins_discovery.py — exclude bundled `cron_providers` from the
# general PluginManager sweep (it has its own discovery; scanning it only printed
# "Failed to load plugin 'chronos': ... no attribute 'register_cron_scheduler'").
DISCOVERY_MARKER="HERMES-CORE-FIX(cron-providers-exclusion)"
DISCOVERY_FILE="hermes_cli/plugins_discovery.py"
DISCOVERY_SNIPPET='{"memory", "context_engine", "platforms", "model-providers", "cron_providers"}'

# Local commits carrying the fixes (first found wins).
CLI_COMMITS=(cd941ed43e 015284f910 0e843a0d2c 1590dd7ce5 866d8486d8)
WEBHOOK_COMMITS=(a22a2b0944 f15e3fa1c8)

# Fallback when every local commit is gone (history rewrite): vendored patch.
CLI_PATCH="${CLI_PATCH_OVERRIDE:-$SRC_DIR/hermes-toolset-fix.patch}"

[[ -f "$REPO/cli.py" ]] || { echo "[self-heal] no cli.py at $REPO — nothing to do"; exit 0; }
cd "$REPO"

changed=0

# ── (1) cli.py toolset validation ────────────────────────────────────
if grep -qF "$CLI_MARKER" cli.py; then
    echo "[self-heal] cli toolset-validation fix already present."
else
    echo "[self-heal] cli toolset-validation fix MISSING — re-applying..."
    applied=0
    for sha in "${CLI_COMMITS[@]}"; do
        if git cat-file -e "${sha}^{commit}" 2>/dev/null; then
            # NOTE: `git cherry-pick --no-commit` can exit 0 without applying the
            # hunk (observed: it prints "Auto-merging cli.py" and returns 0 while
            # the marker is still absent). So verify the OUTCOME, not the exit
            # code, and always clean up a half-started cherry-pick — otherwise the
            # next git command fails with "cherry-pick is already in progress".
            git cherry-pick --no-commit "$sha" >/dev/null 2>&1 || true
            if grep -qF "$CLI_MARKER" cli.py; then
                applied=1
            else
                git cherry-pick --abort >/dev/null 2>&1 || true
            fi
            if [[ $applied -eq 1 ]]; then break; fi

            git show "$sha" -- cli.py | git apply - >/dev/null 2>&1 || true
            if grep -qF "$CLI_MARKER" cli.py; then applied=1; break; fi
        fi
    done
    if [[ $applied -eq 0 ]]; then
        if [[ -f "$CLI_PATCH" ]]; then
            git apply "$CLI_PATCH" >/dev/null 2>&1 || true
            grep -qF "$CLI_MARKER" cli.py && applied=1 \
                || echo "[self-heal] git apply failed (tree dirty?) — see $CLI_PATCH" >&2
        else
            echo "[self-heal] WARN: no local commit and no patch at $CLI_PATCH" >&2
        fi
    fi
    git cherry-pick --abort >/dev/null 2>&1 || true
    if [[ $applied -eq 1 ]] && grep -qF "$CLI_MARKER" cli.py; then
        # --only: commit ONLY the file(s) named here. A bare `git commit` commits
        # everything staged, so a parallel session's staged WIP would be swept into
        # this commit (and, as observed, could become the ONLY file committed while
        # the actual fix was left out).
        git commit -q --only cli.py \
            -m "fix(cli): validate plugin toolsets against persisted plugin-key cache (self-heal re-apply)" || true
        if git diff --quiet HEAD -- cli.py 2>/dev/null; then
            echo "[self-heal] cli fix present in the working tree and already in HEAD."
        else
            echo "[self-heal] cli fix re-applied and committed."
        fi
        changed=1
    else
        echo "[self-heal] ERROR: cli re-apply did not leave the marker." >&2
    fi
fi

# ── (2) gateway/platforms/webhook.py raw-ops contract ─────────────────
WH="gateway/platforms/webhook.py"

if [[ ! -f "$WH" ]]; then
    echo "[self-heal] no $WH — skipping (2)."
else
    missing=0
    for m in "${WEBHOOK_MARKERS[@]}"; do
        grep -qF "$m" "$WH" || missing=1
    done

    if [[ $missing -eq 0 ]]; then
        echo "[self-heal] webhook raw-ops fix already present."
    else
        echo "[self-heal] webhook raw-ops fix MISSING — re-applying..."
        restored=0
        for sha in "${WEBHOOK_COMMITS[@]}"; do
            if git cat-file -e "${sha}^{commit}" 2>/dev/null; then
                if git checkout "$sha" -- "$WH" 2>/dev/null; then restored=1; break; fi
            fi
        done
        if [[ $restored -eq 1 ]]; then
            for tsha in "${WEBHOOK_COMMITS[@]}"; do
                if git cat-file -e "${tsha}^{commit}" 2>/dev/null \
                   && git show "$tsha:tests/gateway/test_webhook_adapter.py" >/dev/null 2>&1; then
                    git checkout "$tsha" -- tests/gateway/test_webhook_adapter.py 2>/dev/null || true
                    break
                fi
            done
            if git cat-file -e "HEAD:tests/gateway/test_webhook_adapter.py" 2>/dev/null \
               || [[ -f tests/gateway/test_webhook_adapter.py ]]; then
                git commit -q --only "$WH" tests/gateway/test_webhook_adapter.py \
                    -m "fix(webhook): restore raw-ops outcome callback + steer routing (self-heal re-apply)" || true
            else
                git commit -q --only "$WH" \
                    -m "fix(webhook): restore raw-ops outcome callback + steer routing (self-heal re-apply)" || true
            fi
            if git diff --quiet HEAD -- "$WH" 2>/dev/null; then
                echo "[self-heal] webhook fix present and already in HEAD."
            else
                echo "[self-heal] webhook fix re-applied and committed."
            fi
            changed=1
        else
            echo "[self-heal] ERROR: webhook fix could not be restored (no local commit)." >&2
        fi
    fi
fi

# ── (3) hermes_cli/plugins_discovery.py — cron_providers exclusion ────
# Without it every startup prints:
#   Failed to load plugin 'chronos': 'PluginContext' object has no attribute
#   'register_cron_scheduler'
# The provider still loads through its own discovery, so this is noise — but it
# trains the user to ignore startup errors. Applied by targeted text edit because
# the surrounding line is upstream-owned and may drift.
if [[ ! -f "$DISCOVERY_FILE" ]]; then
    echo "[self-heal] no $DISCOVERY_FILE — skipping (3)."
elif grep -qF "$DISCOVERY_MARKER" "$DISCOVERY_FILE"; then
    echo "[self-heal] cron_providers exclusion already present."
else
    echo "[self-heal] cron_providers exclusion MISSING — re-applying..."
    "$PYTHON_BIN" - "$DISCOVERY_FILE" <<PYEOF || true
import re, sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
old = '{"memory", "context_engine", "platforms", "model-providers"}'
new = '{"memory", "context_engine", "platforms", "model-providers", "cron_providers"}'
if old not in src:
    print("[self-heal] WARN: anchor line changed upstream — patch (3) needs review", file=sys.stderr)
    sys.exit(0)
note = (
    "    # HERMES-CORE-FIX(cron-providers-exclusion): cron_providers belongs in this set for the\n"
    "    # same reason memory and context_engine do (own discovery; register_cron_scheduler is not\n"
    "    # on the general PluginContext), so scanning it printed a startup failure for chronos.\n"
)
src = src.replace(old, new, 1)
# Put the marker comment immediately above the scan call's category set owner line.
src = src.replace('    repo_plugins = _origin.get_bundled_plugins_dir()',
                  note + '    repo_plugins = _origin.get_bundled_plugins_dir()', 1)
open(path, "w", encoding="utf-8").write(src)
print("[self-heal] cron_providers exclusion applied.")
PYEOF
    if grep -qF "$DISCOVERY_MARKER" "$DISCOVERY_FILE"; then
        git commit -q --only "$DISCOVERY_FILE" \
            -m "fix(plugins): exclude cron_providers from the general PluginManager sweep (self-heal re-apply)" || true
        if git diff --quiet HEAD -- "$DISCOVERY_FILE" 2>/dev/null; then
            echo "[self-heal] cron_providers exclusion present and already in HEAD."
        else
            echo "[self-heal] cron_providers exclusion committed."
        fi
        changed=1
    else
        echo "[self-heal] ERROR: patch (3) did not apply (anchor drifted?)." >&2
    fi
fi

if [[ $changed -eq 1 ]]; then
    # Reload the gateway automatically so the restored code is actually served.
    # Previously this only PRINTED a note, so after a `hermes update` the fix landed
    # on disk while the running gateway kept executing the old bytecode — the exact
    # failure mode behind the silently dropped outcome webhook. Only fired when
    # something changed.
    #
    # `systemctl --user` needs XDG_RUNTIME_DIR (and DBUS_SESSION_BUS_ADDRESS). Cron
    # and @reboot start with a bare environment, where systemctl --user fails with
    # "Failed to connect to bus: No medium found" — so the check MUST export these or
    # it silently reports the unit as not-running and skips the reload in exactly the
    # automated path this exists for.
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "$XDG_RUNTIME_DIR/bus" ]]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        echo "[self-heal] systemd not available — reload manually if needed."
    elif systemctl --user is-active --quiet hermes-gateway.service 2>/dev/null; then
        echo "[self-heal] restarting hermes-gateway to serve the restored code..."
        if systemctl --user kill -s SIGUSR1 hermes-gateway.service 2>/dev/null; then
            # SIGUSR1 is a graceful drain; the process is replaced once the active
            # turn finishes, so do not wait for it here.
            echo "[self-heal] reload signalled (graceful — drains the current turn first)."
        else
            echo "[self-heal] WARNING: could not signal the gateway; reload it manually:"
            echo "            systemctl --user kill -s SIGUSR1 hermes-gateway.service"
        fi
    else
        echo "[self-heal] gateway not running (or bus unreachable) — nothing to reload."
    fi
fi
