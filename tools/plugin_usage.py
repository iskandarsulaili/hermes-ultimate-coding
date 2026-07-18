"""Per-plugin-toolset call counter.

Tracks how many times our plugin toolsets have been called
during the current session.  Used by the TUI status bar to show live
plugin usage indicators without any extra LLM cost.

Only tracks the 4 hermes-ultimate-coding plugin toolsets.
MCP server toolsets and other dynamic toolsets are ignored.

Thread-safe for CPython: dict operations are atomic.  No lock needed
for the simple increment/snapshot pattern we use here.
"""

import logging
import sys
from collections import defaultdict
from typing import Dict, Optional, Set

# ── Our plugin toolsets (hermes-ultimate-coding) ────────────────────
# Only these toolsets are tracked and displayed in the status bar.
# MCP server toolsets and other dynamic registrations are excluded.
OUR_PLUGIN_TOOLSETS: Set[str] = {
    "effect", "graphify", "lsp", "semble",
    "searxng", "cloakbrowser", "orchestra",
}

# Single source of truth for status bar display constants.
# cli.py and hermes-tps import from here instead of duplicating.
PLUGIN_TOOLSET_EMOJI: Dict[str, str] = {
    "effect": "\u26a1",         # ⚡
    "graphify": "\U0001f578\ufe0f",  # 🕸️
    "lsp": "\U0001f527",        # 🔧
    "semble": "\U0001f50d",       # 🔍
    "searxng": "\U0001f310",    # 🌐 (web search)
    "cloakbrowser": "\U0001f4f1",  # 📱 (browser)
    "orchestra": "\U0001f3b5",    # 🎵
}
PLUGIN_TOOLSET_LABEL: Dict[str, str] = {
    "effect": "Effect",
    "graphify": "Graphify",
    "lsp": "LSP",
    "semble": "Semble",
    "searxng": "SearXNG",
    "cloakbrowser": "Cloak",
    "orchestra": "Orch",
}

_plugin_call_counts: Dict[str, int] = defaultdict(int)

# Log at import time so the user can confirm tracking is loaded
_debug_log = logging.getLogger("hermes-plugin-usage")
if not _debug_log.hasHandlers():
    _debug_log.setLevel(logging.DEBUG)
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setLevel(logging.DEBUG)
    _handler.setFormatter(logging.Formatter("[plugin-track] %(message)s"))
    _debug_log.addHandler(_handler)
_debug_log.debug(
    "Tracking active for: %s",
    ", ".join(sorted(OUR_PLUGIN_TOOLSETS)),
)

# ── Public API ──────────────────────────────────────────────────────

def record_plugin_call(toolset: str) -> None:
    """Increment the call counter for *toolset*, but only if it is one
    of our known plugin toolsets.  MCP and other dynamic toolsets are
    silently ignored."""
    if toolset in OUR_PLUGIN_TOOLSETS:
        _plugin_call_counts[toolset] += 1
        _debug_log.debug("+++ %s = %d", toolset, _plugin_call_counts[toolset])
    else:
        _debug_log.debug("--- %s (ignored)", toolset)


def get_plugin_call_counts() -> Dict[str, int]:
    """Return a snapshot of current plugin call counts (our toolsets only).

    Always includes all known toolsets (zero for unused ones) so the
    status bar can always render the full set even before any tool is called.
    Only includes toolsets that are actually registered in the live registry,
    so uninstalled plugins don't appear.
    """
    counts = {k: _plugin_call_counts[k] for k in list(_plugin_call_counts)}
    # Only include toolsets that are actually registered
    known = get_known_plugin_toolsets()
    if known:
        return {
            ts: counts.get(ts, 0)
            for ts in sorted(OUR_PLUGIN_TOOLSETS)
            if ts in known
        }
    # Fallback: if registry not yet populated, show all 4
    return {
        ts: counts.get(ts, 0)
        for ts in sorted(OUR_PLUGIN_TOOLSETS)
    }


def reset_plugin_call_counts() -> None:
    """Reset all counters (called on /reset or session start)."""
    _plugin_call_counts.clear()
    _debug_log.debug("reset")


# Cache for get_known_plugin_toolsets — populated once, never changes
# mid-session because plugins can't hot-reload.
_known_toolsets_cache: Optional[Set[str]] = None


def get_known_plugin_toolsets() -> Set[str]:
    """Return the set of our plugin toolsets that are actually registered
    in the live tool registry.

    This is the intersection of OUR_PLUGIN_TOOLSETS with whatever the
    registry actually knows about, so the status bar only shows emoji
    for toolsets whose tools were actually loaded.

    Result is cached after the first call (plugins can't hot-reload).
    """
    global _known_toolsets_cache
    if _known_toolsets_cache is not None:
        return _known_toolsets_cache
    try:
        from tools.registry import registry
        registered = set(registry.get_registered_toolset_names())
        _known_toolsets_cache = OUR_PLUGIN_TOOLSETS & registered
        return _known_toolsets_cache
    except Exception:
        _known_toolsets_cache = set(OUR_PLUGIN_TOOLSETS)
        return _known_toolsets_cache
