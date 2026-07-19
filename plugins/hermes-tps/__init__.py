"""
hermes-tps — Tokens-per-second + plugin call counts in the Hermes TUI status bar.

SELF-CONTAINED: No dependencies on Hermes core files (tools.plugin_usage, etc.).
Uses only the plugin API hooks (post_api_request, post_tool_call) that are
guaranteed to survive Hermes updates. All state is in module-level variables.

SURVIVES UPDATES: Lives entirely in ~/.hermes/plugins/hermes-tps/.
No files outside this directory are created or modified.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Toolset → prefix lookup (no Hermes core dependencies) ─────────────
_TOOLSET_PREFIXES: Dict[str, str] = {
    "effect_": "effect",
    "graphify_": "graphify",
    "lsp_": "lsp",
    "semble_": "semble",
    "searxng_": "searxng",
    "cloakbrowser_": "cloakbrowser",
    "orchestra_": "orchestra",
}

_TOOLSET_EMOJI: Dict[str, str] = {
    "effect": "\u26a1",         # ⚡
    "graphify": "\U0001f578\ufe0f",  # 🕸️
    "lsp": "\U0001f527",        # 🔧
    "semble": "\U0001f50d",       # 🔍
    "searxng": "\U0001f310",    # 🌐
    "cloakbrowser": "\U0001f4f1",  # 📱
    "orchestra": "\U0001f3b5",    # 🎵
}

_TOOLSET_LABEL: Dict[str, str] = {
    "effect": "Effect",
    "graphify": "Graphify",
    "lsp": "LSP",
    "semble": "Semble",
    "searxng": "SearXNG",
    "cloakbrowser": "Cloak",
    "orchestra": "Orch",
}


def _toolset_for_function(name: str) -> Optional[str]:
    """Resolve toolset from function name using prefix matching.
    No Hermes registry dependency — pure string matching.
    """
    for prefix, ts in _TOOLSET_PREFIXES.items():
        if name.startswith(prefix):
            return ts
    return None


# ── t/s sliding window ────────────────────────────────────────────────
_tps_lock = threading.Lock()
_last_tps: Optional[str] = None
_last_tps_time: float = 0.0
_tps_ring: Deque[Tuple[int, float]] = deque(maxlen=5)

_MIN_OUTPUT_TOKENS = 5
_MIN_API_DURATION_S = 0.05


def _store_tps(output_tokens: int, api_duration: float, finish_reason: str = "") -> None:
    """Compute and store smoothed t/s from API call metrics."""
    global _last_tps, _last_tps_time

    if finish_reason in ("error", "cancel", "cancelled", "content_filter"):
        return
    if not (api_duration >= _MIN_API_DURATION_S and output_tokens >= _MIN_OUTPUT_TOKENS):
        return

    with _tps_lock:
        _tps_ring.append((output_tokens, api_duration))
        total_tokens = sum(t for t, _ in _tps_ring)
        total_duration = sum(d for _, d in _tps_ring)
        tps = total_tokens / total_duration
        _last_tps = f"{tps:.1f}"
        _last_tps_time = time.monotonic()


def get_tps() -> Optional[str]:
    """Thread-safe read of latest t/s. Auto-expires after 120s."""
    global _last_tps, _last_tps_time
    with _tps_lock:
        if _last_tps is not None and time.monotonic() - _last_tps_time > 120:
            _tps_ring.clear()
            return None
        return _last_tps


# ── Plugin call counters (self-contained, no Hermes core deps) ────────
_call_lock = threading.Lock()
_plugin_call_counts: Dict[str, int] = {}


def record_plugin_call(toolset: str) -> None:
    """Increment call counter for a toolset."""
    with _call_lock:
        _plugin_call_counts[toolset] = _plugin_call_counts.get(toolset, 0) + 1


def get_plugin_counts() -> Dict[str, int]:
    """Snapshot of current call counts."""
    with _call_lock:
        return dict(_plugin_call_counts)


# ── Hooks ─────────────────────────────────────────────────────────────
def _on_post_api_request(**kwargs: Any) -> None:
    """Capture t/s from API responses."""
    try:
        usage = kwargs.get("usage")
        api_duration = kwargs.get("api_duration", 0.0)
        finish_reason = kwargs.get("finish_reason", "") or ""

        if not usage or not isinstance(usage, dict):
            return
        completion = usage.get("output_tokens", 0) or 0
        if not isinstance(completion, (int, float)):
            completion = 0

        _store_tps(completion, api_duration, finish_reason=finish_reason)
    except Exception:
        logger.warning("tps: post_api_request hook failed", exc_info=True)


def _on_post_tool_call(**kwargs: Any) -> None:
    """Capture plugin call counts from tool dispatches."""
    try:
        function_name = kwargs.get("function_name", "")
        toolset = _toolset_for_function(function_name)
        if toolset:
            record_plugin_call(toolset)
    except Exception:
        pass


# ── TUI status bar patching (self-contained) ──────────────────────────
_patched = False


def _patch_cli_status_bar() -> None:
    """Monkey-patch HermesCLI to show t/s and plugin counts in status bar."""
    global _patched
    if _patched:
        return
    _patched = True

    try:
        import cli as _cli_module
    except ImportError:
        logger.debug("tps: not in TUI context — skipping status bar patch")
        return

    HermesCLI = getattr(_cli_module, "HermesCLI", None)
    if HermesCLI is None:
        logger.warning("tps: HermesCLI not found")
        return

    # ── 1. Patch snapshot ──
    _orig_snapshot = HermesCLI._get_status_bar_snapshot

    @functools.wraps(_orig_snapshot)
    def _patched_snapshot(self) -> Dict[str, Any]:
        result = _orig_snapshot(self)
        result["last_api_speed"] = get_tps()
        result["plugin_usage"] = get_plugin_counts()
        return result

    HermesCLI._get_status_bar_snapshot = _patched_snapshot

    # ── 2. Patch text builder (wide format) ──
    _orig_build_text = HermesCLI._build_status_bar_text

    @functools.wraps(_orig_build_text)
    def _tps_build_text(self, width: Optional[int] = None) -> str:
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

            parts = [f"\u2695 {snapshot['model_short']}", f"ctx {percent_label}",
                     f"[{snapshot.get('state_bar', '')}]"]

            # t/s
            _speed = snapshot.get("last_api_speed")
            if _speed:
                parts.append(f"{_speed} t/s")

            # Plugin call counts
            _usage = snapshot.get("plugin_usage", {})
            if _usage:
                usage_parts = []
                for ts in sorted(_TOOLSET_PREFIXES.values()):
                    cnt = _usage.get(ts, 0)
                    emoji = _TOOLSET_EMOJI.get(ts, "?")
                    label = _TOOLSET_LABEL.get(ts, ts)
                    usage_parts.append(f"{emoji}{label}:{cnt}")
                parts.append(" | ".join(usage_parts))

            parts.append(duration_label)

            prompt_elapsed = snapshot.get("prompt_elapsed")
            if prompt_elapsed:
                parts.append(prompt_elapsed)
            idle_since = snapshot.get("idle_since")
            if idle_since:
                parts.append(idle_since)
            if yolo_active:
                parts.append("\u26a0 YOLO")

            return self._trim_status_bar_text(" \u2502 ".join(parts), width)
        except Exception:
            return _orig_build_text(self, width)

    HermesCLI._build_status_bar_text = _tps_build_text

    # ── 3. Patch TUI fragment builder ──
    _orig_fragments = HermesCLI._get_status_bar_fragments

    @functools.wraps(_orig_fragments)
    def _tps_fragments(self):
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

            bar_style = self._status_bar_context_style(percent)
            _speed = snapshot.get("last_api_speed")

            frags = [
                ("class:status-bar", " \u2695 "),
                ("class:status-bar-strong", snapshot["model_short"]),
                ("class:status-bar-dim", " \u2502 "),
                ("class:status-bar-dim", f"ctx {percent_label}"),
                ("class:status-bar-dim", " \u2502 "),
                (bar_style, self._build_context_bar(percent)),
                ("class:status-bar-dim", " "),
                (bar_style, percent_label),
            ]

            if _speed:
                frags.append(("class:status-bar-dim", " \u2502 "))
                frags.append(("class:status-bar-strong", f"{_speed} t/s"))

            # Plugin usage
            _usage = snapshot.get("plugin_usage", {})
            if _usage:
                active = []
                inactive = []
                for ts in sorted(_TOOLSET_PREFIXES.values()):
                    cnt = _usage.get(ts, 0)
                    emoji = _TOOLSET_EMOJI.get(ts, "?")
                    label = _TOOLSET_LABEL.get(ts, ts)
                    txt = f"{emoji}{label}:{cnt}"
                    if cnt > 0:
                        active.append(("class:status-bar-strong", txt))
                    else:
                        inactive.append(("class:status-bar-dim", txt))
                if active or inactive:
                    frags.append(("class:status-bar-dim", " \u2502 "))
                    for i, (style, txt) in enumerate(active + inactive):
                        if i > 0:
                            frags.append(("class:status-bar-dim", " "))
                        frags.append((style, txt))

            frags.extend([
                ("class:status-bar-dim", " \u2502 "),
                ("class:status-bar-dim", duration_label),
            ])
            prompt_elapsed = snapshot.get("prompt_elapsed")
            if prompt_elapsed:
                frags.append(("class:status-bar-dim", " \u2502 "))
                frags.append(("class:status-bar-dim", prompt_elapsed))
            idle_since = snapshot.get("idle_since")
            if idle_since:
                frags.append(("class:status-bar-dim", " \u2502 "))
                frags.append(("class:status-bar-dim", idle_since))
            if yolo_active:
                frags.append(("class:status-bar-dim", " \u2502 "))
                frags.append(("class:status-bar-yolo", "\u26a0 YOLO"))
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
    logger.info("tps: patched HermesCLI status bar — t/s and plugin counts will appear in wide format")


# ── Plugin entry point ────────────────────────────────────────────────
_hook_registered = False


def register(ctx) -> Dict[str, Any]:
    """Register the hermes-tps plugin."""
    global _hook_registered

    if not _hook_registered:
        ctx.register_hook("post_api_request", _on_post_api_request)
        ctx.register_hook("post_tool_call", _on_post_tool_call)
        _hook_registered = True

    _patch_cli_status_bar()
    logger.info("tps: registered — t/s + plugin call counts tracking active")
    return {"name": "hermes-tps", "version": "1.0.0"}
