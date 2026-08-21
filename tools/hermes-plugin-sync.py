#!/usr/bin/env python3
"""
hermes-plugin-sync — keep SOUL.md / AGENTS.md / MEMORY.md in sync with the
installed Hermes plugin pack.

Scans ~/.hermes/plugins/hermes-* for the authoritative truth:
  - plugin.yaml description (canonical per-plugin purpose)
  - register_tool( blocks (authoritative tool names + count)

Rewrites the plugin-inventory section of SOUL.md, AGENTS.md and MEMORY.md so
every plugin's purpose and tools are documented and discoverable, so each
plugin gets fully utilized.

The section is delimited by HTML comment markers. If a target has no markers
yet, the existing "Plugin Usage" section (SOUL.md) or "Plugin Usage
Instructions" section (AGENTS.md) is recognized by its header and replaced
in place with a marked version. MEMORY.md gets a marked section appended.
Idempotent, stdlib-only. --dry-run prints without writing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HOME = Path.home()
PLUGINS_DIR = HOME / ".hermes" / "plugins"
SOUL = HOME / ".hermes" / "SOUL.md"
AGENTS = HOME / ".hermes" / "AGENTS.md"
MEMORY = HOME / ".hermes" / "memories" / "MEMORY.md"

SOUL_START = "<!-- PLUGIN-USAGE-START -->"
SOUL_END = "<!-- PLUGIN-USAGE-END -->"
AGENTS_START = "<!-- PLUGIN-INVENTORY-START -->"
AGENTS_END = "<!-- PLUGIN-INVENTORY-END -->"
MEMORY_START = "<!-- PLUGIN-UTILIZATION-START -->"
MEMORY_END = "<!-- PLUGIN-UTILIZATION-END -->"


def _yaml_desc(plugin_dir: Path) -> str:
    py = plugin_dir / "plugin.yaml"
    try:
        for line in py.read_text().splitlines():
            s = line.strip()
            if s.startswith("description:"):
                return s.split(":", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return ""


def _tool_names(plugin_dir: Path) -> list[str]:
    """Extract tool names from each register_tool( ... ) block, robust to kwarg order."""
    ini = plugin_dir / "__init__.py"
    try:
        src = ini.read_text()
    except Exception:
        return []
    names: list[str] = []
    i, n = 0, len(src)
    while True:
        start = src.find("register_tool(", i)
        if start == -1:
            break
        depth, j, end = 0, start + len("register_tool(") - 1, -1
        while j < n:
            c = src[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        block = src[start : end + 1] if end != -1 else src[start : start + 4000]
        m = re.search(r'name=(["\'])([^"\']+)\1', block)
        if m:
            names.append(m.group(2))
        i = (end if end != -1 else start + len("register_tool(")) + 1
    seen: set[str] = set()
    out: list[str] = []
    for nm in names:
        if nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def _scan() -> list[dict]:
    inv: list[dict] = []
    if not PLUGINS_DIR.is_dir():
        return inv
    for d in sorted(PLUGINS_DIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("hermes-"):
            continue
        tools = _tool_names(d)
        inv.append({"name": d.name, "desc": _yaml_desc(d), "tools": tools, "count": len(tools)})
    return inv


def _bullet(p: dict, with_tools: bool = True) -> str:
    name, desc, tools, n = p["name"], p["desc"], p["tools"], p["count"]
    if with_tools and tools:
        tstr = ", ".join(f"`{t}`" for t in tools)
        return f"- **{name}** ({n} tools): {tstr}. {desc}"
    return f"- **{name}** ({n} tools): {desc}"


def build_soul(inv: list[dict], total: int) -> str:
    lines = ["# Plugin Usage", ""]
    lines.append(f"You have {len(inv)} plugins with {total} tools available. Use them actively in every task.")
    lines.append("")
    lines += [_bullet(p) for p in inv]
    return "\n".join(lines)


def build_agents(inv: list[dict], total: int) -> str:
    lines = [f"You have {len(inv)} plugins with {total} tools available. Use them actively in every task.", ""]
    for p in inv:
        lines.append(f"- **{p['name']}** ({p['count']} tools): {p['desc']}")
    return "\n".join(lines)


def build_memory(inv: list[dict], total: int) -> str:
    lines = ["# Plugin Utilization", ""]
    lines.append(f"The plugin pack ships {len(inv)} plugins / {total} tools. Use the right plugin per task:")
    lines.append("")
    for p in inv:
        lines.append(f"- **{p['name']}** ({p['count']} tools): {p['desc']}")
        if p["tools"]:
            lines.append(f"  Tools: {', '.join(p['tools'])}")
    return "\n".join(lines)


def _replace_marked(text: str, start: str, end: str, section: str) -> str:
    """Replace everything between start/end markers (must both exist)."""
    head = text.split(start, 1)[0]
    tail = text.split(end, 1)[1]
    return head + start + "\n" + section + "\n" + end + tail


def _apply(path: Path, start: str, end: str, header_hint: str, section: str, label: str) -> bool:
    """Apply the section to `path`. Handles: marked present, unmarked header present, or append."""
    if not path.exists():
        print(f"  [SKIP] {label}: {path} missing")
        return False
    text = path.read_text()
    if start in text and end in text:
        new_text = _replace_marked(text, start, end, section)
        if new_text == text:
            print(f"  [OK] {label}: in sync")
            return False
        path.write_text(new_text)
        print(f"  [WROTE] {label}: {path.name} updated (marked)")
        return True
    # No markers. If the header_hint heading exists, replace the plugin-bullet
    # body that follows it in place (stop at next '#' heading OR a non-bullet
    # non-blank line, e.g. SOUL's trailing "Workflow:" line).
    header_ln = f"# {header_hint}"
    lines = text.splitlines(keepends=True)
    si = None
    for i, ln in enumerate(lines):
        if ln.strip() == header_ln:
            si = i
            break
    if si is not None:
        ei = len(lines)
        for j in range(si + 1, len(lines)):
            s = lines[j].strip()
            if s.startswith("# "):
                ei = j
                break
            if s and not s.startswith("- ") and not s.startswith("<!--"):
                ei = j  # stop before non-bullet content (e.g. Workflow:)
                break
        block = start + "\n" + section + "\n" + end + "\n"
        new_text = "".join(lines[:si]) + block + "".join(lines[ei:])
        path.write_text(new_text)
        print(f"  [WROTE] {label}: {path.name} header body replaced")
        return True
    # Neither markers nor header: append marked section at end.
    new_text = text.rstrip() + "\n\n" + start + "\n" + section + "\n" + end + "\n"
    path.write_text(new_text)
    print(f"  [ADDED] {label}: appended marked section to {path.name}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--targets",
        nargs="*",
        default=["soul", "agents"],
        help="which docs to sync: soul agents memory (memory is opt-in; MEMORY.md is near its token budget)",
    )
    args = ap.parse_args()

    inv = _scan()
    if not inv:
        print("ERROR: no plugins found in", PLUGINS_DIR)
        return 1
    total = sum(p["count"] for p in inv)
    print(f"Scanned {len(inv)} plugins, {total} tools total:")

    if args.dry_run:
        if "soul" in args.targets:
            print("\n=== SOUL.md ===\n" + build_soul(inv, total))
        if "agents" in args.targets:
            print("\n=== AGENTS.md ===\n" + build_agents(inv, total))
        if "memory" in args.targets:
            print("\n=== MEMORY.md ===\n" + build_memory(inv, total))
        return 0

    changed = False
    if "soul" in args.targets:
        changed = _apply(SOUL, SOUL_START, SOUL_END, "Plugin Usage", build_soul(inv, total), "SOUL") or changed
    if "agents" in args.targets:
        changed = _apply(AGENTS, AGENTS_START, AGENTS_END, "Plugin Usage Instructions", build_agents(inv, total), "AGENTS") or changed
    if "memory" in args.targets:
        changed = _apply(MEMORY, MEMORY_START, MEMORY_END, "Plugin Utilization", build_memory(inv, total), "MEMORY") or changed

    print(f"\n{'CHANGED' if changed else 'in sync'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
