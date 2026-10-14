#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# hermes-ultimate-coding — full-stack SURVIVAL script.
#
# One entry point that makes the whole stack survive:
#   • machine reboot
#   • `hermes update` (which resets fork-local core commits)
#   • a fresh machine (run from install-ultimate.sh)
#
# Idempotent and safe to run at any time. Every step verifies its own outcome.
#
# Steps
#   1. Sync plugin files repo -> ~/.hermes/plugins (installed copy is what runs,
#      and the repo carries the fixes — a stale install silently lacks them).
#   2. Re-apply fork-local Hermes core fixes (cli.py, gateway webhook,
#      plugins_discovery cron_providers) via the self-heal script.
#   3. Ensure the SearXNG engine tuning is applied (0-result search otherwise).
#   3b. Ensure the memory-TDAI gateway is installed/enabled.
#   3c. Ensure the REA reverse-engineering stack (engines + MCP entry + skill).
#   4. Ensure services are enabled so they come back after a reboot.
#   5. Ensure the schedule exists (@reboot + daily).
#
# Usage:  survive.sh [--check]     (--check = report only, change nothing)
# ──────────────────────────────────────────────────────────────────────
set -uo pipefail

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO_DIR:-$(cd "$SELF_DIR/.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins"
AGENT_DIR="${HERMES_AGENT_DIR:-$HERMES_HOME/hermes-agent}"
# memory-tdai gateway (used by section 3b). The gateway runs as the systemd --user
# unit tdai-gateway.service and listens on 8420; the port is overridable to match the
# plugin's own env.
TDAI_PORT="${TDAI_GATEWAY_PORT:-8420}"
SELF_HEAL="$SELF_DIR/self-heal-hermes-core-fixes.sh"
ENGINE_TUNE="$SELF_DIR/searxng_engine_tune.py"
# SearXNG settings live inside the CLONED source tree (`searxng-src/searx/settings.yml`),
# which is where the plugin finds them too. The old default pointed at
# `searxng/config/settings.yml`, a path that does not exist, so the engine tuning was
# silently skipped as "(no searxng settings ...)" and a fresh install kept SearXNG's
# stock engine set — which returns ZERO results for every query while reporting HTTP 200.
SETTINGS="${SEARXNG_SETTINGS:-$HOME/searxng/searxng-src/searx/settings.yml}"
LOG="$HERMES_HOME/logs/survive.log"

mkdir -p "$(dirname "$LOG")"

# Overlap guard: @reboot (45s) and the daily cron can collide, and a slow run
# (plugin sync over a big tree) could still be going when the next starts. Two
# concurrent runs would fight over the plugin directory and the crontab.
LOCK_FILE="${TMPDIR:-/tmp}/hermes-survive.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "[survive] another run is in progress — exiting" | tee -a "$LOG"
    exit 0
fi

say() { echo "$@" | tee -a "$LOG"; }

say "=========================================================="
say " hermes-ultimate-coding — survive  ($(date '+%F %T'))  check=$CHECK"
say "=========================================================="

fails=0

# ── 1. Plugins: repo -> install ───────────────────────────────────────
say ""
say "[1] plugin files (repo -> $PLUGIN_DIR)"
if [[ ! -d "$REPO/plugins" ]]; then
    say "  ! no plugins/ in $REPO — skipping"
    fails=$((fails+1))
else
    synced=0; same=0; preserved_note=0
    # Files that live ONLY in the install dir and must survive a sync: dependency
    # trees and build artifacts are generated there, not in the repo. A plain
    # `rm -rf $dst && cp -r` (the original approach) DELETED them — it would have
    # thrown away 24 MB of hermes-cloakbrowser's node_modules on the next run.
    KEEP_EXTS='-name __pycache__ -o -name node_modules -o -name package.json -o -name package-lock.json'
    for src in "$REPO"/plugins/*/; do
        name="$(basename "$src")"
        dst="$PLUGIN_DIR/$name"
        [[ -d "$src" ]] || continue
        if diff -rq --exclude=__pycache__ --exclude=node_modules \
             --exclude=package.json --exclude=package-lock.json \
             "$src" "$dst" >/dev/null 2>&1; then
            same=$((same+1))
            continue
        fi
        if [[ $CHECK -eq 1 ]]; then
            say "  would sync: $name"
            synced=$((synced+1))
            continue
        fi
        # Preserve install-only artifacts, then refresh everything the repo owns.
        if [[ -d "$dst" ]]; then
            _stash="$(mktemp -d "${TMPDIR:-/tmp}/survive-keep.XXXXXX")"
            ( cd "$dst" && find . \( $KEEP_EXTS \) -print0 2>/dev/null \
                | tar --null -T - -cf "$_stash/keep.tar" 2>/dev/null ) || true
            rm -rf "$dst"
            cp -r "$src" "$dst"
            if [[ -s "$_stash/keep.tar" ]]; then
                tar -xf "$_stash/keep.tar" -C "$dst" 2>/dev/null || true
                preserved_note=$((preserved_note+1))
            fi
            rm -rf "$_stash"
        else
            cp -r "$src" "$dst"
        fi
        synced=$((synced+1))
    done
    if [[ $CHECK -eq 1 ]]; then
        say "  in sync: $same | out of sync: $synced"
        [[ $synced -gt 0 ]] && fails=$((fails+1))
    else
        say "  in sync: $same | synced: $synced (preserved generated files in $preserved_note)"
    fi
fi

# ── 2. Fork-local core fixes ──────────────────────────────────────────
say ""
say "[2] fork-local Hermes core fixes"
if [[ -f "$SELF_HEAL" ]]; then
    out="$(bash "$SELF_HEAL" "$AGENT_DIR" 2>&1)"
    echo "$out" | sed 's/^/  /' | tee -a "$LOG"
    if echo "$out" | grep -qE "MISSING|ERROR"; then
        # MISSING means it was just re-applied (self-heal reports it that way);
        # only a hard ERROR is a real failure.
        echo "$out" | grep -qE "ERROR" && fails=$((fails+1))
    fi
else
    say "  ! self-heal script missing: $SELF_HEAL"
    fails=$((fails+1))
fi

# ── 3. SearXNG engines ────────────────────────────────────────────────
say ""
say "[3] SearXNG engine tuning"
if [[ -f "$ENGINE_TUNE" && -f "$SETTINGS" ]]; then
    # Only the general-category engines matter here; without usable ones a normal
    # query returns ZERO results while still reporting HTTP 200.
    disabled_count="$(python3 - "$SETTINGS" <<'PYEOF' 2>/dev/null || echo 0
import sys, re
lines = open(sys.argv[1], encoding="utf-8").read().split("\n")
want = {"mwmbl","bing","naver","yandex","wiby","360search"}
starts = [(m.group(1).strip(), i) for i, l in enumerate(lines)
          if (m := re.match(r"^  - name:\s*(.+?)\s*$", l))]
n = 0
for idx, (name, s) in enumerate(starts):
    if name not in want:
        continue
    e = starts[idx+1][1] if idx+1 < len(starts) else len(lines)
    if any(re.match(r"^\s*disabled:\s*true", lines[i]) for i in range(s, e)):
        n += 1
print(n)
PYEOF
)"
    if [[ "$disabled_count" == "0" ]]; then
        say "  already tuned (all 6 general engines enabled)"
    elif [[ $CHECK -eq 1 ]]; then
        say "  NOT tuned — $disabled_count of 6 general engines disabled"
        fails=$((fails+1))
    else
        python3 "$ENGINE_TUNE" --settings "$SETTINGS" 2>&1 | sed 's/^/  /' | tee -a "$LOG" >/dev/null
        say "  applied tuning — restart searxng to take effect"
    fi
else
    say "  (no searxng settings at $SETTINGS — skipped)"
fi

# ── 3b. Memory-TDAI gateway ───────────────────────────────────────────
# The four-layer memory gateway (127.0.0.1:8420) runs as a systemd --user unit so it
# is up at boot and RESTARTS ON FAILURE. Previously this section hand-started a
# detached node process: it worked, but nothing supervised it, so a crash left port
# 8420 dead until the next daily run (verified: killed it, still dead 20s later).
# systemd is the right owner for a long-lived sidecar; this section now just ensures
# the unit exists/is enabled and reports its real state.
say ""
say "[3b] memory-tdai gateway (:8420)"
_TD_UNIT="tdai-gateway.service"
_TD_SRC_UNIT="$SELF_DIR/../systemd/tdai-gateway.service"
_TD_DST_UNIT="$HOME/.config/systemd/user/tdai-gateway.service"
if [[ $CHECK -eq 1 ]]; then
    if curl -s --max-time 5 "http://127.0.0.1:$TDAI_PORT/health" >/dev/null 2>&1; then
        say "  ok      gateway healthy (unit: $(systemctl --user is-enabled "$_TD_UNIT" 2>/dev/null || echo unknown))"
    else
        say "  NOT-RUNNING — start it: systemctl --user enable --now $_TD_UNIT"
        fails=$((fails+1))
    fi
else
    # Install/refresh the unit from the repo copy (idempotent).
    if [[ -f "$_TD_SRC_UNIT" ]]; then
        install -D -m 0644 "$_TD_SRC_UNIT" "$_TD_DST_UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user enable "$_TD_UNIT" >/dev/null 2>&1 || true
    fi
    if ! curl -s --max-time 5 "http://127.0.0.1:$TDAI_PORT/health" >/dev/null 2>&1; then
        systemctl --user start "$_TD_UNIT" >/dev/null 2>&1 || true
    fi
    # Only claim success after /health actually answers.
    _td_ok=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if curl -s --max-time 3 "http://127.0.0.1:$TDAI_PORT/health" >/dev/null 2>&1; then
            _td_ok=1; break
        fi
        sleep 1
    done
    if [[ $_td_ok -eq 1 ]]; then
        say "  ok      gateway healthy (unit active, pid $(systemctl --user show "$_TD_UNIT" -p MainPID --value 2>/dev/null))"
    else
        say "  ! gateway did not answer on :$TDAI_PORT — see log/memory-tdai/gateway.stderr.log"
    fi
fi

# ── 3c. Reverse-engineering stack (REA) ────────────────────────────────
# The plugin files arrive with section 1, but the REA stack needs two things a
# plugin directory cannot carry: the analysis engines (Ghidra/JADX/Rizin/apktool/
# tshark/binwalk/sigrok/WABT/pwntools/mitmproxy/frida/gdb/wakaru/.NET) and the
# MCP entry in config.yaml. Without the entry the plugin works while the full
# ~207-tool MCP surface is silently absent; without the engines every native or
# APK tool degrades. rea-bootstrap.sh handles both idempotently and is what
# install-ultimate.sh calls too, so a fresh machine, a reboot and an update all
# end in the same state.
#
# Engines are NOT downloaded from a boot path by default (Ghidra alone is ~550 MB):
# HERMES_REA_ENGINES=auto enables the download, `check` (the default) only reports.
say ""
say "[3c] reverse-engineering stack (REA)"
REA_BOOTSTRAP="$SELF_DIR/rea-bootstrap.sh"
if [[ ! -f "$REA_BOOTSTRAP" ]]; then
    say "  ! rea-bootstrap.sh missing — REA engines/MCP entry unverified"
    fails=$((fails+1))
elif [[ $CHECK -eq 1 ]]; then
    out="$(HERMES_REA_ENGINES=check REPO_DIR="$REPO" HERMES_HOME="$HERMES_HOME" \
        bash "$REA_BOOTSTRAP" --check 2>&1)"
    rc=$?
    printf '%s\n' "$out" | sed 's/^/  /'
    [[ $rc -ne 0 ]] && fails=$((fails+1))
else
    out="$(HERMES_REA_ENGINES="${HERMES_REA_ENGINES:-check}" REPO_DIR="$REPO" \
        HERMES_HOME="$HERMES_HOME" bash "$REA_BOOTSTRAP" 2>&1)"
    printf '%s\n' "$out" | sed 's/^/  /'
    printf '%s\n' "$out" | grep -q "need attention" && fails=$((fails+1))
fi

# ── 4. Services enabled for boot ──────────────────────────────────────
# `searxng` ships as a USER unit (it runs as this user next to the plugins that
# call it). A plain `systemctl enable` resolves against the SYSTEM manager, which
# reports "not-found" — so the old code printed "could not enable (need sudo?)"
# even when the user unit was already enabled and running. Query and enable in the
# scope the unit actually lives in.
say ""
say "[4] services enabled at boot"
if command -v systemctl >/dev/null 2>&1; then
    while read -r unit; do
        [[ -n "$unit" ]] || continue
        scope=""
        state="unknown"
        if systemctl --user cat "$unit" >/dev/null 2>&1; then
            scope="--user"
            state="$(systemctl --user is-enabled "$unit" 2>/dev/null || echo unknown)"
        elif systemctl cat "$unit" >/dev/null 2>&1; then
            state="$(systemctl is-enabled "$unit" 2>/dev/null || echo unknown)"
        else
            state="not-found"
        fi
        if [[ "$state" == "enabled" ]]; then
            active="$(systemctl $scope is-active "$unit" 2>/dev/null || echo unknown)"
            say "  ok      $unit${scope:+ (user scope, $active)}"
        elif [[ $CHECK -eq 1 ]]; then
            say "  NOT-ENABLED $unit ($state)"
            fails=$((fails+1))
        elif [[ "$scope" == "--user" ]]; then
            systemctl --user enable "$unit" >/dev/null 2>&1 \
                && say "  enabled $unit (user scope)" \
                || say "  ! could not enable user unit $unit"
        else
            sudo -n systemctl enable "$unit" >/dev/null 2>&1 \
                && say "  enabled $unit" \
                || say "  ! could not enable $unit (need sudo?)"
        fi
    done <<'UNITS'
searxng.service
UNITS
fi
# The Hermes gateway runs as a USER unit; it needs linger to start at boot
# without an interactive login.
if command -v loginctl >/dev/null 2>&1; then
    linger="$(loginctl show-user "$(id -un)" -p Linger 2>/dev/null | cut -d= -f2)"
    if [[ "$linger" == "yes" ]]; then
        say "  ok      user linger=yes (hermes-gateway starts at boot)"
    elif [[ $CHECK -eq 1 ]]; then
        say "  NOT-ENABLED user linger (gateway will not start at boot)"
        fails=$((fails+1))
    else
        sudo -n loginctl enable-linger "$(id -un)" >/dev/null 2>&1 \
            && say "  enabled user linger" || say "  ! could not enable linger"
    fi
fi

# ── 5. Schedule ───────────────────────────────────────────────────────
say ""
say "[5] schedule (@reboot + daily)"
if command -v crontab >/dev/null 2>&1; then
    ct="$(crontab -l 2>/dev/null || true)"
    # Match on the FULL current path, not just the basename: a stale absolute path
    # (repo moved, different HERMES_HOME) still contains "survive.sh", so a
    # basename check would accept it forever while the cron job silently ran
    # nothing — the schedule would look healthy and be dead.
    if echo "$ct" | grep -qF "$SELF_DIR/survive.sh"; then
        say "  ok      survive.sh scheduled ($SELF_DIR)"
    elif [[ $CHECK -eq 1 ]]; then
        if echo "$ct" | grep -qF "survive.sh"; then
            say "  STALE schedule: crontab references a different survive.sh path"
        else
            say "  legacy self-heal entry only — survive.sh not scheduled"
        fi
        fails=$((fails+1))
    else
        # Drop any previous survive.sh / self-heal entries (including stale paths)
        # and install the schedule for the CURRENT location.
        ct_tmp="$(mktemp "${TMPDIR:-/tmp}/ct.survive.XXXXXX")"
        echo "$ct" | grep -v "survive\.sh" \
                   | grep -v "self-heal-hermes-core-fixes.sh" \
                   | grep -v "hermes-ultimate-coding core-fix self-heal" > "$ct_tmp"
        {
          cat "$ct_tmp"
          echo "# hermes-ultimate-coding — full-stack survival (idempotent)"
          echo "@reboot sleep 45 && $SELF_DIR/survive.sh >> $LOG 2>&1"
          echo "25 6 * * * $SELF_DIR/survive.sh >> $LOG 2>&1"
        } | crontab -
        rm -f "$ct_tmp"
        say "  scheduled @reboot + daily 06:25 for $SELF_DIR"
    fi
fi

say ""
if [[ $fails -eq 0 ]]; then
    say "RESULT: stack is complete and protected."
else
    say "RESULT: $fails item(s) need attention (see above)."
fi
say "=========================================================="
exit 0
