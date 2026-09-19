#!/usr/bin/env bash
# hermes-plugin-sync-cron — run the plugin-sync pipeline and commit any drift to the repo.
# Runs the deterministic scanner, regenerates SOUL.md + AGENTS.md plugin sections,
# and if they changed, mirrors them into the repo and commits. Safe to run on a cron.
set -u

# Resolve the repo the same way install-ultimate.sh does. The cron previously
# hardcoded $HOME/hermes-ultimate-coding while the installer clones to
# ${HERMES_HOME:-$HOME/.hermes}/hermes-ultimate-coding, so on any machine that sets
# HERMES_HOME the cron pointed at a directory that does not exist (and would fail
# silently from cron, where stderr goes nowhere). Prefer the derived location, and
# fall back to $HOME so an existing install at the old path keeps working.
_HOME_REPO="${HERMES_HOME:-$HOME/.hermes}/hermes-ultimate-coding"
if [[ -f "$_HOME_REPO/tools/hermes-plugin-sync.py" ]]; then
    REPO="$_HOME_REPO"
else
    REPO="$HOME/hermes-ultimate-coding"
fi
SYNC_SCRIPT="$REPO/tools/hermes-plugin-sync.py"
LOG="$HOME/.hermes/logs/plugin-sync.log"

mkdir -p "$(dirname "$LOG")"

if [ ! -f "$SYNC_SCRIPT" ]; then
  echo "[$(date '+%F %T')] sync script missing: $SYNC_SCRIPT" >> "$LOG"
  exit 1
fi

# 1) Regenerate live SOUL.md + AGENTS.md (idempotent).
OUT="$(python3 "$SYNC_SCRIPT" --targets soul agents 2>&1)"
echo "[$(date '+%F %T')] $OUT" >> "$LOG"

# 2) Mirror the live files into the repo whenever they differ from the repo copy —
# not only when the live file was just rewritten. The old condition (only mirror if
# the live file CHANGED) meant that once the live file was correct, a stale repo
# copy was never refreshed: the log said "in sync" every day while the committed
# 16-plugin inventory stayed out of date. Comparing the two files is the real
# condition for "the repo needs updating".
MIRROR=0
for f in SOUL.md AGENTS.md; do
  if ! cmp -s "$HOME/.hermes/$f" "$REPO/$f"; then
    cp "$HOME/.hermes/$f" "$REPO/$f"
    MIRROR=1
  fi
done

if [[ $MIRROR -eq 1 ]]; then
  cd "$REPO"
  git add SOUL.md AGENTS.md
  if ! git diff --cached --quiet; then
    git commit -m "chore: plugin-sync — auto-update SOUL.md + AGENTS.md plugin inventory" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1 && echo "[$(date '+%F %T')] pushed" >> "$LOG"
  else
    echo "[$(date '+%F %T')] repo copy refreshed (no staged change)" >> "$LOG"
  fi
fi
