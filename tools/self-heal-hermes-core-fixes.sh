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

CLI_MARKER="joined yet, so validate against the persisted plugin toolset-key cache too"
WEBHOOK_MARKERS=( "_post_outcome_webhook" "is_steer" "RESUME-DELIVERY FIX" )

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
        git add cli.py
        git commit -q -m "fix(cli): validate plugin toolsets against persisted plugin-key cache (self-heal re-apply)" || true
        echo "[self-heal] cli fix re-applied and committed."
        changed=1
    else
        echo "[self-heal] ERROR: cli re-apply did not leave the marker." >&2
    fi
fi

# ── (2) gateway/platforms/webhook.py raw-ops contract ─────────────────
WH="gateway/platforms/webhook.py"
[[ -f "$WH" ]] || { echo "[self-heal] no $WH — nothing to do for (2)."; exit 0; }

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
        git add "$WH" tests/gateway/test_webhook_adapter.py 2>/dev/null || git add "$WH"
        git commit -q -m "fix(webhook): restore raw-ops outcome callback + steer routing (self-heal re-apply)" || true
        echo "[self-heal] webhook fix re-applied and committed."
        changed=1
    else
        echo "[self-heal] ERROR: webhook fix could not be restored (no local commit)." >&2
    fi
fi

if [[ $changed -eq 1 ]]; then
    echo "[self-heal] NOTE: reload the gateway to serve restored code:"
    echo "            systemctl --user kill -s SIGUSR1 hermes-gateway.service"
fi
