#!/usr/bin/env bash
# hermes-plugin-sync-cron — run the plugin-sync pipeline and commit any drift to the repo.
# Runs the deterministic scanner, regenerates SOUL.md + AGENTS.md plugin sections,
# and if they changed, mirrors them into the repo and commits. Safe to run on a cron.
set -u

SYNC_SCRIPT="$HOME/agentic-lsp/tools/hermes-plugin-sync.py"
REPO="$HOME/agentic-lsp"
LOG="$HOME/.hermes/logs/plugin-sync.log"

mkdir -p "$(dirname "$LOG")"

if [ ! -f "$SYNC_SCRIPT" ]; then
  echo "[$(date '+%F %T')] sync script missing: $SYNC_SCRIPT" >> "$LOG"
  exit 1
fi

# 1) Regenerate live SOUL.md + AGENTS.md (idempotent).
OUT="$(python3 "$SYNC_SCRIPT" --targets soul agents 2>&1)"
echo "[$(date '+%F %T')] $OUT" >> "$LOG"

if echo "$OUT" | grep -q "WROTE\|CHANGED"; then
  # 2) Mirror the updated live files into the repo.
  cp "$HOME/.hermes/SOUL.md" "$REPO/SOUL.md"
  cp "$HOME/.hermes/AGENTS.md" "$REPO/AGENTS.md"
  cd "$REPO"
  git add SOUL.md AGENTS.md
  if ! git diff --cached --quiet; then
    git commit -m "chore: plugin-sync — auto-update SOUL.md + AGENTS.md plugin inventory" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1 && echo "[$(date '+%F %T')] pushed" >> "$LOG"
  fi
fi
