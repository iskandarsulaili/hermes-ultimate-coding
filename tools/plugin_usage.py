"""Per-plugin-toolset call counter.

Tracks how many times our plugin toolsets have been called
during the current session.  Used by the TUI status bar to show live
plugin usage indicators without any extra LLM cost.

Thread-safe for CPython: dict operations are atomic.  No lock needed
for the simple increment/snapshot pattern we use here.
"""

import logging
import sys
from collections import defaultdict
from typing import Dict, Optional, Set

# ── Our plugin toolsets (hermes-ultimate-coding) ────────────────────
# Keep this in step with plugins/hermes-tps/_TOOLSET_PREFIXES, which tracks the
# SAME set of toolsets for the status bar. The two lists had drifted (8 here vs 15
# there), so counts for half the pack were silently never collected. The full pack
# is 17 toolsets; google_meet has no tools and moa-trigger exposes only
# planning_trigger, which the tps prefix map already attributes.
OUR_PLUGIN_TOOLSETS: Set[str] = {
    "effect", "graphify", "lsp", "semble",
    "searxng", "cloakbrowser", "orchestra", "cross-memory",
    "codegraph", "codegraph-context", "vault", "agents",
    "memory-tdai", "dsh", "anchored", "moa-trigger",
}

PLUGIN_TOOLSET_EMOJI: Dict[str, str] = {
    "effect": "\u26a1",         # ⚡
    "graphify": "\U0001f578\ufe0f",  # 🕸️
    "lsp": "\U0001f527",        # 🔧
    "semble": "\U0001f50d",       # 🔍
    "searxng": "\U0001f310",    # 🌐
    "cloakbrowser": "\U0001f4f1",  # 📱
    "orchestra": "\U0001f3b5",    # 🎵
    "cross-memory": "\U0001f9e0",  # 🧠
    "codegraph": "\U0001f9ea",        # 🧪
    "codegraph-context": "\U0001f52c",  # 🔬
    "vault": "\U0001f5c4\ufe0f",    # 🗄️
    "agents": "\U0001f465",        # 👥
    "memory-tdai": "\U0001f9e9",   # 🧩
    "dsh": "\U0001f680",           # 🚀
    "anchored": "\u2693",         # ⚓
    "moa-trigger": "\U0001f9ed",   # 🧭
}
PLUGIN_TOOLSET_LABEL: Dict[str, str] = {
    "effect": "Effect",
    "graphify": "Graphify",
    "lsp": "LSP",
    "semble": "Semble",
    "searxng": "SearXNG",
    "cloakbrowser": "Cloak",
    "orchestra": "Orch",
    "cross-memory": "XMEM",
    "codegraph": "CG",
    "codegraph-context": "CGC",
    "vault": "Vault",
    "agents": "Agents",
    "memory-tdai": "TDAI",
    "dsh": "DSH",
    "anchored": "Anchor",
    "moa-trigger": "MoA",
}

_plugin_call_counts: Dict[str, int] = defaultdict(int)

_debug_log = logging.getLogger("hermes-plugin-usage")
if not _debug_log.hasHandlers():
    _debug_log.setLevel(logging.DEBUG)
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setLevel(logging.DEBUG)
    _handler.setFormatter(logging.Formatter("[plugin-track] %(message)s"))
    _debug_log.addHandler(_handler)
_debug_log.debug("Tracking active for: %s", ", ".join(sorted(OUR_PLUGIN_TOOLSETS)))


def record_plugin_call(toolset: str) -> None:
    """Increment the call counter for *toolset*."""
    if toolset in OUR_PLUGIN_TOOLSETS:
        _plugin_call_counts[toolset] += 1
        _debug_log.debug("+++ %s = %d", toolset, _plugin_call_counts[toolset])
    else:
        _debug_log.debug("--- %s (ignored)", toolset)


def get_plugin_call_counts() -> Dict[str, int]:
    """Return a snapshot of current plugin call counts."""
    return {
        ts: _plugin_call_counts.get(ts, 0)
        for ts in sorted(OUR_PLUGIN_TOOLSETS)
    }


def reset_plugin_call_counts() -> None:
    """Reset all counters (called on /reset or session start)."""
    _plugin_call_counts.clear()
    _debug_log.debug("reset")
