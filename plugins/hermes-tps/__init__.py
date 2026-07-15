"""
hermes-tps — Tokens-per-second in the Hermes TUI status bar.

Captures per-call generation speed from API responses and renders it
in the status bar alongside model, context %, and elapsed time.

Survives Hermes updates by living entirely in ~/.hermes/plugins/.
No external dependencies — stdlib only.
"""

from __future__ import annotations

import functools
import logging
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes-tps")

# =============================================================================
# JIT dependency management (generic — pip, npm, go, apt, rustup, …)
# =============================================================================
# Each entry in ``_DEPS_SPEC`` is a ``DepSpec``:
#
#   name          — display name (shown in status output)
#   check         — shell command that exits 0 when available
#   install       — shell command to install (``None`` = manual hint only)
#   purpose       - human-readable "why this is needed"
#   version       — minimum version constraint (e.g. ``">=1.2.3"``, optional)
#   version_check — shell command that prints the installed version
#
# ``check`` / ``install`` / ``version_check`` can be either:
#   * ``list[str]`` — executed directly via ``subprocess.run`` (safe)
#   * ``str``       — executed via ``shell=True`` (needed for pipes / redirects)
import subprocess
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class DepSpec:
    name: str
    check: list[str] | str
    install: list[str] | str | None = None
    purpose: str = ""
    version: str | None = None
    version_check: list[str] | str | None = None


_DEPS_SPEC: list[DepSpec] = [
    DepSpec(
        "functools",
        ["python3", "-c", "import functools"],
        purpose="function wrapping for CLI monkey-patches",
    ),
    DepSpec(
        "threading",
        ["python3", "-c", "import threading"],
        purpose="thread-safe shared state",
    ),
    DepSpec(
        "time",
        ["python3", "-c", "import time"],
        purpose="monotonic clock for expiry",
    ),
    DepSpec(
        "collections",
        ["python3", "-c", "from collections import deque"],
        purpose="ring buffer for sliding-window average",
    ),
]
_deps_verified = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cmd(
    args: list[str] | str,
    *,
    capture: bool = False,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a shell command.

    * ``list[str]`` → direct ``subprocess.run`` (no shell)
    * ``str``       → ``shell=True`` (required for pipes, redirects)
    """
    if isinstance(args, str):
        return subprocess.run(args, shell=True, capture_output=capture, timeout=timeout)
    return subprocess.run(args, capture_output=capture, timeout=timeout)


def _stream_cmd(args: list[str] | str, label: str = "  hermes-tps") -> None:
    """Run a command and stream its output to stderr in real time."""
    kwargs: dict = {}
    if isinstance(args, str):
        kwargs["shell"] = True
        # String commands with pipes don't work well with PIPE, so we
        # use a PTY-like approach: just let output flow to stderr.
        print(f"{label}   running: {args}", file=sys.stderr)
        subprocess.check_call(args, shell=True)
        return

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout:
        for line in proc.stdout:
            if line.strip():
                print(f"{label}   {line.rstrip()}", file=sys.stderr, flush=True)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"command {' '.join(args)} exited {proc.returncode}")


def _parse_version(ver: str) -> tuple[int, ...]:
    """Parse ``"1.2.3"`` → ``(1, 2, 3)``.  Ignores leading non-digits."""
    import re
    nums = re.findall(r"\d+", ver)
    return tuple(int(n) for n in nums) if nums else (0,)


def _check_version_meets(installed_raw: str, requirement: str) -> tuple[bool, str]:
    """Check ``installed_raw`` against ``requirement`` (e.g. ``\">=1.2.3\"``).

    Returns ``(ok, message)``.
    """
    installed = _parse_version(installed_raw)

    # Strip operator prefix
    op = "=="
    req_str = requirement.strip()
    for possible in (">=", "<=", "!=", ">", "<", "=="):
        if req_str.startswith(possible):
            op = possible
            req_str = req_str[len(possible):].strip()
            break

    required = _parse_version(req_str)

    if op == ">=":
        ok = installed >= required
    elif op == "<=":
        ok = installed <= required
    elif op == ">":
        ok = installed > required
    elif op == "<":
        ok = installed < required
    elif op == "!=":
        ok = installed != required
    else:  # ==
        ok = installed == required

    if ok:
        return True, f"{'.'.join(map(str, installed))} meets {requirement}"
    return False, (
        f"{'.'.join(map(str, installed))} does NOT meet {requirement}"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _ensure_deps() -> None:
    """JIT dependency verification — runs once per process.

    For each ``DepSpec`` in ``_DEPS_SPEC``:

    1. Run the ``check`` command.  Exit 0 → available.
    2. If missing and ``install`` is set → run the installer with visible
       progress.
    3. If ``version`` is set, run ``version_check`` and compare.
       **Never auto-upgrades** — the user retains full authority.

    All output goes to *stderr* so it is visible in the terminal even
    when stdout is captured (piped, subagent, etc.).
    """
    global _deps_verified
    if _deps_verified:
        return
    _deps_verified = True

    print("  hermes-tps ⟐ verifying dependencies …", file=sys.stderr, flush=True)

    for spec in _DEPS_SPEC:
        try:
            result = _run_cmd(spec.check, capture=True, timeout=30)

            if result.returncode != 0:
                raise FileNotFoundError(f"exit {result.returncode}")

            print(
                f"  hermes-tps ✓ {spec.name}  — {spec.purpose or 'ok'}",
                file=sys.stderr, flush=True,
            )

            # Optional version check
            if spec.version and spec.version_check:
                vr = _run_cmd(spec.version_check, capture=True, timeout=15)
                if vr.returncode == 0:
                    installed_raw = vr.stdout.strip()
                    ok, msg = _check_version_meets(installed_raw, spec.version)
                    if ok:
                        logger.info("hermes-tps: %s %s", spec.name, msg)
                    else:
                        logger.warning(
                            "hermes-tps: %s %s", spec.name, msg,
                        )
                        print(
                            f"  hermes-tps ⚠ {spec.name} {msg}",
                            file=sys.stderr, flush=True,
                        )

        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            # Not available — try to install
            if spec.install is None:
                print(
                    f"  hermes-tps ⚠ {spec.name} not found — {spec.purpose}",
                    file=sys.stderr, flush=True,
                )
                print(
                    f"            Install manually, or add an install command"
                    f" to DepSpec",
                    file=sys.stderr, flush=True,
                )
                continue

            print(
                f"  hermes-tps … {spec.name} not found → installing …",
                file=sys.stderr, flush=True,
            )
            try:
                _stream_cmd(spec.install)
                print(
                    f"  hermes-tps ✓ {spec.name} installed",
                    file=sys.stderr, flush=True,
                )
            except Exception as exc:
                logger.error(
                    "hermes-tps: failed to install %s: %s", spec.name, exc,
                )
                print(
                    f"  hermes-tps ✗ failed to install {spec.name}: {exc}",
                    file=sys.stderr, flush=True,
                )
                raise

    print("  hermes-tps ✓ deps ok", file=sys.stderr, flush=True)

# =============================================================================
# Thread-safe t/s storage with sliding-window smoothing
# =============================================================================

_tps_lock = threading.Lock()
_last_tps: Optional[str] = None       # formatted t/s string, e.g. "12.3"
_last_tps_time: float = 0.0           # monotonic timestamp of last write
_patched = False                       # guard against double-patching
_hook_registered = False               # guard against double hook registration

# Sliding window: aggregate the last N valid API calls so that a single
# noisy measurement (cache hit, very short response, heavy-TTFT prompt)
# doesn't dominate the displayed t/s.  Longer generations contribute
# more weight naturally (more tokens / more time).
_RING: Deque[Tuple[int, float]] = deque(maxlen=5)

# Minimums to exclude probe calls, cache blinks, and error fragments.
_MIN_OUTPUT_TOKENS = 5
_MIN_API_DURATION_S = 0.05

# Cache the CLI helper references at patch time (not on every status-bar tick).
_cli_format_context_length = None
_cli_format_token_compact = None


def _store_tps(
    output_tokens: int,
    api_duration: float,
    *,
    finish_reason: str = "",
) -> None:
    """Compute and store t/s from API call metrics.

    Uses a **sliding-window moving average** over the last 5 valid API
    calls rather than displaying the instantaneous value.  This gives a
    more stable and representative picture of real generation speed:

    * A cache-hit call (very short duration) contributes a small amount
      of time to the denominator, so it barely moves the needle.
    * A heavy-TTFT call (long prompt processing) contributes a large
      amount of time, so it pulls the average down — correctly reflecting
      that the user waited longer for those tokens.
    * A long generation contributes proportionally more tokens and time,
      so it has greater weight — which is correct.

    Filters out noise:
    - ``finish_reason`` in ``{error, cancel, cancelled, content_filter}`` →
      skip entirely (blocked / aborted)
    - fewer than 5 output tokens → too short to measure meaningfully
    - ``api_duration`` under 0.05 s → cache-only blink, skip
    """
    global _last_tps, _last_tps_time

    # Skip aborted / blocked responses entirely.
    if finish_reason in ("error", "cancel", "cancelled", "content_filter"):
        return

    # Minimum thresholds: need a real generation, not a probe or cache blink
    if not (api_duration >= _MIN_API_DURATION_S and output_tokens >= _MIN_OUTPUT_TOKENS):
        return

    with _tps_lock:
        _RING.append((output_tokens, api_duration))

        total_tokens = sum(t for t, _ in _RING)
        total_duration = sum(d for _, d in _RING)
        tps = total_tokens / total_duration

        _last_tps = f"{tps:.1f}"
        _last_tps_time = time.monotonic()


def get_tps() -> Optional[str]:
    """Thread-safe read of the latest smoothed t/s value.

    Auto-expires after 120 seconds of inactivity so the status bar
    doesn't show a stale measurement from a prior session.
    """
    global _last_tps, _last_tps_time
    with _tps_lock:
        if _last_tps is not None and time.monotonic() - _last_tps_time > 120:
            _RING.clear()
            return None
        return _last_tps


# =============================================================================
# Hook: post_api_request — capture t/s from every API call
# =============================================================================

def _on_post_api_request(**kwargs: Any) -> None:
    """Capture completion tokens and duration from the API response.

    Excludes noise: error/cancelled calls, cache-only blips, and
    sub-threshold responses (< 5 tokens, < 0.05 s).
    """
    try:
        usage = kwargs.get("usage")
        api_duration = kwargs.get("api_duration", 0.0)
        finish_reason = kwargs.get("finish_reason", "") or ""

        if not usage or not isinstance(usage, dict):
            return

        completion = usage.get("output_tokens", 0) or 0
        # Type-safety: API responses can sometimes return non-numeric
        # token counts (e.g. {"total": 100}) — treat those as 0.
        if not isinstance(completion, (int, float)):
            completion = 0

        _store_tps(
            completion,
            api_duration,
            finish_reason=finish_reason,
        )
    except Exception:
        logger.warning("hermes-tps: hook handler failed", exc_info=True)


# =============================================================================
# CLI status bar patching
# =============================================================================

def _patch_cli_status_bar() -> None:
    """Monkey-patch HermesCLI to show t/s in the status bar.

    Patches three methods:
      1. _get_status_bar_snapshot  — add last_api_speed field
      2. _build_status_bar_text     — render in text mode
      3. _get_status_bar_fragments  — render in TUI fragment mode
    """
    global _patched
    if _patched:
        return
    _patched = True
    try:
        import cli as _cli_module
    except ImportError:
        logger.debug("hermes-tps: not in TUI context — skipping status bar patch")
        return

    HermesCLI = getattr(_cli_module, "HermesCLI", None)
    if HermesCLI is None:
        logger.warning("hermes-tps: HermesCLI not found in cli module")
        return

    # Capture CLI helper refs once at patch time, not every status-bar tick.
    global _cli_format_context_length, _cli_format_token_compact
    _cli_format_context_length = getattr(_cli_module, "_format_context_length", None)
    _cli_format_token_compact = getattr(_cli_module, "format_token_count_compact", None)

    # --- 1. Patch _get_status_bar_snapshot to inject t/s ---
    _orig_snapshot = HermesCLI._get_status_bar_snapshot

    @functools.wraps(_orig_snapshot)
    def _patched_snapshot(self) -> Dict[str, Any]:
        result = _orig_snapshot(self)
        result["last_api_speed"] = get_tps()
        return result

    HermesCLI._get_status_bar_snapshot = _patched_snapshot

    # --- 2. Patch _build_status_bar_text (wide format) ---
    _orig_build_text = HermesCLI._build_status_bar_text

    @functools.wraps(_orig_build_text)
    def _tps_build_text(self, width: Optional[int] = None) -> str:
        global _cli_format_context_length, _cli_format_token_compact
        if width is None:
            try:
                width = self._get_tui_terminal_width()
            except Exception:
                width = 80

        if width < 76:
            return _orig_build_text(self, width)

        try:
            snapshot = self._get_status_bar_snapshot()
            percent = snapshot.get("context_percent")
            percent_label = f"{percent}%" if percent is not None else "--"
            duration_label = snapshot["duration"]
            yolo_active = self._is_session_yolo_active()

            if snapshot.get("context_length"):
                if _cli_format_context_length is None:
                    from cli import _format_context_length as _fmt_ctx, format_token_count_compact as _fmt_tok
                    _cli_format_context_length = _fmt_ctx
                    _cli_format_token_compact = _fmt_tok
                ctx_total = _cli_format_context_length(snapshot["context_length"])
                ctx_used = _cli_format_token_compact(snapshot["context_tokens"])
                context_label = f"{ctx_used}/{ctx_total}"
            else:
                context_label = "ctx --"

            compressions = snapshot.get("compressions", 0)
            parts = [f"⚕ {snapshot['model_short']}", context_label, percent_label]

            _speed = snapshot.get("last_api_speed")
            if _speed:
                parts.append(f"{_speed} t/s")

            if compressions:
                parts.append(f"🗜️ {compressions}")
            bg_count = snapshot.get("active_background_tasks", 0)
            if bg_count:
                parts.append(f"▶ {bg_count}")
            bg_proc_count = snapshot.get("active_background_processes", 0)
            if bg_proc_count:
                parts.append(f"⚙ {bg_proc_count}")
            bg_subagent_count = snapshot.get("active_background_subagents", 0)
            if bg_subagent_count:
                parts.append(f"⛓ {bg_subagent_count}")
            parts.append(duration_label)
            prompt_elapsed = snapshot.get("prompt_elapsed")
            if prompt_elapsed:
                parts.append(prompt_elapsed)
            idle_since = snapshot.get("idle_since")
            if idle_since:
                parts.append(idle_since)
            if yolo_active:
                parts.append("⚠ YOLO")

            return self._trim_status_bar_text(" │ ".join(parts), width)
        except Exception:
            return _orig_build_text(self, width)

    HermesCLI._build_status_bar_text = _tps_build_text

    # --- 3. Patch _get_status_bar_fragments (TUI fragment mode) ---
    _orig_fragments = HermesCLI._get_status_bar_fragments

    @functools.wraps(_orig_fragments)
    def _tps_fragments(self):
        global _cli_format_context_length, _cli_format_token_compact
        if not self._status_bar_visible or getattr(self, '_model_picker_state', None):
            return []
        try:
            snapshot = self._get_status_bar_snapshot()
            width = self._get_tui_terminal_width()
            duration_label = snapshot["duration"]
            yolo_active = self._is_session_yolo_active()

            if width < 52:
                return _orig_fragments(self)
            if width < 76:
                return _orig_fragments(self)

            percent = snapshot.get("context_percent")
            percent_label = f"{percent}%" if percent is not None else "--"

            if snapshot.get("context_length"):
                if _cli_format_context_length is None:
                    from cli import _format_context_length as _fmt_ctx, format_token_count_compact as _fmt_tok
                    _cli_format_context_length = _fmt_ctx
                    _cli_format_token_compact = _fmt_tok
                ctx_total = _cli_format_context_length(snapshot["context_length"])
                ctx_used = _cli_format_token_compact(snapshot["context_tokens"])
                context_label = f"{ctx_used}/{ctx_total}"
            else:
                context_label = "ctx --"

            bar_style = self._status_bar_context_style(percent)
            compressions = snapshot.get("compressions", 0)
            bg_count = snapshot.get("active_background_tasks", 0)
            bg_proc_count = snapshot.get("active_background_processes", 0)
            bg_subagent_count = snapshot.get("active_background_subagents", 0)
            _speed = snapshot.get("last_api_speed")

            frags = [
                ("class:status-bar", " ⚕ "),
                ("class:status-bar-strong", snapshot["model_short"]),
                ("class:status-bar-dim", " │ "),
                ("class:status-bar-dim", context_label),
                ("class:status-bar-dim", " │ "),
                (bar_style, self._build_context_bar(percent)),
                ("class:status-bar-dim", " "),
                (bar_style, percent_label),
            ]

            if _speed:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append(("class:status-bar-strong", f"{_speed} t/s"))

            if compressions:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append((self._compression_count_style(compressions), f"🗜️ {compressions}"))
            if bg_count:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append(("class:status-bar-strong", f"▶ {bg_count}"))
            if bg_proc_count:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append(("class:status-bar-strong", f"⚙ {bg_proc_count}"))
            if bg_subagent_count:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append(("class:status-bar-strong", f"⛓ {bg_subagent_count}"))
            frags.extend([
                ("class:status-bar-dim", " │ "),
                ("class:status-bar-dim", duration_label),
            ])
            prompt_elapsed = snapshot.get("prompt_elapsed")
            if prompt_elapsed:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append(("class:status-bar-dim", prompt_elapsed))
            idle_since = snapshot.get("idle_since")
            if idle_since:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append(("class:status-bar-dim", idle_since))
            if yolo_active:
                frags.append(("class:status-bar-dim", " │ "))
                frags.append(("class:status-bar-yolo", "⚠ YOLO"))
            frags.append(("class:status-bar", " "))

            total_width = sum(
                self._status_bar_display_width(text) for _, text in frags
            )
            if total_width > width:
                plain_text = "".join(text for _, text in frags)
                trimmed = self._trim_status_bar_text(plain_text, width)
                return [("class:status-bar", trimmed)]
            return frags
        except Exception:
            return _orig_fragments(self)

    HermesCLI._get_status_bar_fragments = _tps_fragments
    logger.info("hermes-tps: patched HermesCLI status bar — t/s will appear in wide format")


# =============================================================================
# Plugin entry point
# =============================================================================

def register(ctx) -> Dict[str, Any]:
    """Register the hermes-tps plugin."""
    _ensure_deps()
    global _hook_registered
    if not _hook_registered:
        ctx.register_hook("post_api_request", _on_post_api_request)
        _hook_registered = True
    _patch_cli_status_bar()
    logger.info("hermes-tps plugin registered: captures t/s from API calls")
    return {"name": "hermes-tps", "version": "1.0.0"}
