#!/usr/bin/env python3
"""Enable the SearXNG general-category engines that actually answer.

Why this exists
---------------
The stock SearXNG settings.yml disables most general engines, and the few it
leaves on (brave, startpage, duckduckgo) are CAPTCHA'd or rate-limited from a
data-centre IP. A fresh instance therefore returns **zero results for every
query** while still reporting HTTP 200 — the plugin looks healthy and is
useless. Measured before this ran: 0/8 queries yielded anything.

Measured yield per engine (query "python asyncio", this host):

    mwmbl       81      bing        10      wiby        7
    naver       15      yandex      10      360search   7

and these returned nothing (CAPTCHA / suspended / denied / timeout), so they are
deliberately left disabled:

    google, duckduckgo, brave, startpage, qwant, presearch, yep, mojeek,
    stract, marginalia, right dao, searx.be-style mirrors, sogou, baidu,
    quark, seznam, yahoo, crowdview, wikidata

Re-probe anytime with --probe; engines rot as providers tighten rate limits.

Editing method
--------------
Text-surgical, NOT a yaml.safe_load/safe_dump round-trip: the live settings.yml
carries ~600 comment lines of documentation and the round-trip silently deletes
every one of them. This only flips `disabled:` on the named engine blocks.

Usage:
    searxng_engine_tune.py [--settings PATH] [--probe] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Engines measured to work from this host. Order = rough usefulness.
ENABLE = ["mwmbl", "bing", "naver", "yandex", "wiby", "360search"]

# Known-blocked general engines: explicitly KEEP disabled so a later upstream
# default flip cannot silently re-arm a provider that only returns errors.
# Only names actually declared in this SearXNG version belong here — the tuner
# reports `missing` for anything else (harmless, but noise).
KEEP_DISABLED = [
    "brave", "startpage", "duckduckgo", "google", "qwant", "presearch",
    "yep", "mojeek", "marginalia", "sogou", "baidu", "quark", "seznam",
    "yahoo", "crowdview", "wikidata",
]

DEFAULT_SETTINGS = "/home/lot399/searxng/config/settings.yml"


def _engine_blocks(lines: list[str]) -> list[tuple[str, int, int]]:
    """[(name, start, end)] for each top-level engine entry in the engines list."""
    starts: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^  - name:\s*(.+?)\s*$", line)
        if m:
            starts.append((m.group(1).strip().strip('"\''), i))
    blocks = []
    for idx, (name, start) in enumerate(starts):
        end = starts[idx + 1][1] if idx + 1 < len(starts) else len(lines)
        blocks.append((name, start, end))
    return blocks


def set_disabled(lines: list[str], name: str, disabled: bool) -> str:
    """Set/remove `disabled:` inside the engine block called *name*.

    Returns "set", "cleared", "already" or "missing".
    """
    for bname, start, end in _engine_blocks(lines):
        if bname != name:
            continue
        for i in range(start, end):
            m = re.match(r"^(\s*)disabled:\s*(\S+)\s*$", lines[i])
            if m:
                if not disabled:
                    del lines[i]
                    return "cleared"
                if m.group(2).lower() == "true":
                    return "already"
                lines[i] = f"{m.group(1)}disabled: true"
                return "set"
        if not disabled:
            return "already"  # absent == enabled by default
        # No disabled key: insert one matching the block's indentation.
        indent = "    "
        for i in range(start + 1, end):
            m = re.match(r"^(\s+)\S", lines[i])
            if m:
                indent = m.group(1)
                break
        lines.insert(start + 1, f"{indent}disabled: true")
        return "set"
    return "missing"


def probe(base_url: str = "http://127.0.0.1:8080", query: str = "python asyncio",
          engines: list[str] | None = None, timeout: int = 25) -> dict[str, int]:
    """Live yield per engine (results returned for *query*)."""
    out: dict[str, int] = {}
    for e in (engines or ENABLE):
        url = f"{base_url}/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "engines": e})
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                out[e] = len((json.load(r).get("results") or []))
        except Exception:
            out[e] = -1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default=DEFAULT_SETTINGS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="report live per-engine yield and exit (no edits)")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    args = ap.parse_args()

    if args.probe:
        print(json.dumps(probe(args.base_url), indent=2))
        return 0

    path = Path(args.settings)
    if not path.is_file():
        print(f"settings file not found: {path}", file=sys.stderr)
        return 1

    lines = path.read_text(encoding="utf-8").split("\n")
    changes: list[str] = []

    for name in ENABLE:
        r = set_disabled(lines, name, False)
        changes.append(f"  enable {name:12} -> {r}")

    for name in KEEP_DISABLED:
        r = set_disabled(lines, name, True)
        if r in ("set", "missing"):
            changes.append(f"  keep-disabled {name:12} -> {r}")

    print("\n".join(changes))

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    new = "\n".join(lines)
    if new == path.read_text(encoding="utf-8"):
        print("\nno changes needed")
        return 0

    path.write_text(new, encoding="utf-8")
    print(f"\nwrote {path}")
    print("restart to apply: sudo systemctl restart searxng")
    return 0


if __name__ == "__main__":
    sys.exit(main())
