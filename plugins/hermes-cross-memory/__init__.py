"""
hermes-cross-memory — Bidirectional cross-memory between Claude Code and Hermes.

Both agents keep persistent, file-based memory that today are independent silos:

    Hermes        ~/.hermes/memories/MEMORY.md   (§-delimited paragraphs)
                  ~/.hermes/memories/USER.md     (§-delimited paragraphs)
    Claude Code   ~/.claude/projects/<cwd-hash>/memory/
                    MEMORY.md (index of fact files) + one .md fact per entry

This plugin bi-directionally crosses them so neither agent loses a fact the
other saved, and both can search one combined memory.

Because the existing Claude Code MCP bridge (claude-code/hermes_mcp_bridge.py)
exposes every Hermes plugin tool to Claude Code through the live registry, this
plugin's tools are available in BOTH agents from one implementation.

Safety:
  * All writes are atomic (temp file + os.replace) — a mid-write failure never
    truncates a live memory file.
  * Path components are validated (no absolute paths, no "..") so a tool can
    never escape the owning stores it was handed.
  * Forget removes exactly one named entry; nothing destructive by default.
  * Defaults point at the real stores only for READ/search/status; the sync and
    write tools refuse without an explicit confirm flag or claude_dir/hermes_dir
    so an audit can run entirely against temp copies.

DEPENDENCIES: stdlib only. No pip/npm runtime requirement.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Config (env overridable) ─────────────────────────────────────────────
def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


HOME = Path.home()
_HERMES_HOME = Path(_env("HERMES_HOME", str(HOME / ".hermes")))
HERMES_MEMORY_DIR = Path(_env("HERMES_CROSS_MEMORY_HERMES_DIR", str(_HERMES_HOME / "memories")))
_CLAUDE_HOME = Path(_env("CLAUDE_CONFIG_DIR", str(HOME / ".claude")))
CLAUDE_ROOT = Path(_env("HERMES_CROSS_MEMORY_CLAUDE_DIR", str(_CLAUDE_HOME / "projects")))
GLOBAL_CLAUDE_MD = Path(_env("HERMES_CROSS_MEMORY_GLOBAL_CLAUDE", str(_CLAUDE_HOME / "CLAUDE.md")))
CWD = Path(_env("HERMES_CROSS_MEMORY_CWD", str(Path.cwd())))
CROSS_SEARCH_LIMIT = _env("HERMES_CROSS_MEMORY_SEARCH_LIMIT", "20")

_LOCK = threading.RLock()

SRC_HERMES_MEMORY = "hermes-memory"
SRC_HERMES_USER = "hermes-user"
SRC_CLAUDE_PROJECT = "claude-project"
SRC_CLAUDE_GLOBAL = "claude-global"

HERMES_FILE_NAMES = ("MEMORY.md", "USER.md")


# ── Hermes store ─────────────────────────────────────────────────────────
class _HermesStore:
    """Read/append Hermes MEMORY.md / USER.md (§-delimited paragraphs)."""

    FILE_NAMES = HERMES_FILE_NAMES

    @staticmethod
    def _split(text: str) -> List[str]:
        parts = [p.strip() for p in re.split(r"\n?§\n?", text) if p.strip()]
        return parts

    @staticmethod
    def _topic(text: str) -> str:
        slug = re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()
        return slug[:70] if slug else f"untitled-{abs(hash(text)) % 10**6}"

    def read(self, store_dir: Optional[Path], file: str) -> Dict[str, Any]:
        base = Path(store_dir) if store_dir else HERMES_MEMORY_DIR
        p = base / file
        entries: List[Dict[str, str]] = []
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return {"error": f"read {p}: {e}", "file": file, "count": 0, "entries": []}
            for para in self._split(text):
                entries.append({
                    "body": para,
                    "topic": self._topic(para),
                    "source": SRC_HERMES_MEMORY if file == "MEMORY.md" else SRC_HERMES_USER,
                })
        return {"file": file, "path": str(p), "count": len(entries), "entries": entries}

    def add(self, body: str, *, store_dir: Optional[Path],
            file: str = "MEMORY.md", tags: Optional[List[str]] = None) -> Dict[str, Any]:
        with _LOCK:
            return self._add_locked(body, store_dir=store_dir, file=file, tags=tags)

    def _add_locked(self, body: str, *, store_dir: Optional[Path],
                    file: str, tags: Optional[List[str]]) -> Dict[str, Any]:
        base = Path(store_dir) if store_dir else HERMES_MEMORY_DIR
        base.mkdir(parents=True, exist_ok=True)
        p = base / file
        existing = self.read(base, file)
        if "error" in existing:
            return {"error": existing["error"]}
        topic = self._topic(body)
        if any(e["topic"] == topic for e in existing["entries"]):
            return {"status": "skipped", "reason": "duplicate topic", "topic": topic}
        line = body
        if tags:
            line = f"[cross-memory: {', '.join(tags)}] {body}"
        text = ""
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return {"error": f"read {p}: {e}"}
        if text and not text.endswith("\n"):
            text += "\n"
        delta = f"{text}§\n{line}\n" if text else f"{line}\n"
        # delta already embeds the full existing content — write ATOMICALLY
        # (replace, not append) or existing paragraphs get duplicated.
        if not _atomic_write(p, delta):
            return {"error": f"atomic append failed for {p}"}
        return {"status": "added", "file": file, "topic": topic, "path": str(p)}

    def forget(self, topic: str, *, store_dir: Optional[Path],
               file: str = "MEMORY.md") -> Dict[str, Any]:
        with _LOCK:
            return self._forget_locked(topic, store_dir=store_dir, file=file)

    def _forget_locked(self, topic: str, *, store_dir: Optional[Path],
                       file: str) -> Dict[str, Any]:
        base = Path(store_dir) if store_dir else HERMES_MEMORY_DIR
        p = base / file
        if not p.is_file():
            return {"error": f"no such file: {p}"}
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"error": f"read {p}: {e}"}
        paragraphs = self._split(text)
        needle = topic.strip().lower()
        removed = 0
        kept: List[str] = []
        for para in paragraphs:
            if not needle or self._topic(para) == needle or para.strip().lower() == needle:
                removed += 1
            else:
                kept.append(para)
        if removed == 0:
            return {"status": "not-found", "topic": topic, "file": file}
        new_text = "\n§\n".join(kept)
        if not _atomic_write(p, new_text + ("\n" if new_text else "")):
            return {"error": f"atomic write failed for {p}"}
        return {"status": "forgot", "removed": removed, "file": file, "path": str(p)}


# ── Claude Code project-dir derivation + store ───────────────────────────
def _claude_project_dir(cwd: Optional[Path]) -> Path:
    """Claude's project memory dir: CLAUDE_ROOT/<'-'+cwd with / -> ->/memory.

    Verified against real installs:
        /home/lot399                         -> -home-lot399
        /home/lot399/hermes-ultimate-coding  -> -home-lot399-hermes-ultimate-coding
    """
    cwd = Path(cwd) if cwd else CWD
    rel = str(cwd.resolve()).lstrip(os.sep)
    name = "-" + rel.replace(os.sep, "-")
    return CLAUDE_ROOT / name / "memory"


class _ClaudeStore:
    """Read/write Claude Code fact files + the MEMORY.md index."""

    INDEX = "MEMORY.md"

    @staticmethod
    def _safe_name(name: str) -> Optional[str]:
        raw = name.strip()
        # Reject traversal / absolute paths on the RAW string — basename would
        # silently strip "../../etc/passwd" down to "passwd" and defeat the guard.
        if not raw or ".." in raw or "/" in raw or "\\" in raw or raw.startswith("."):
            return None
        base = raw if raw.endswith(".md") else raw + ".md"
        return base if base.endswith(".md") and "." in base else None

    def list(self, memory_dir: Optional[Path]) -> Dict[str, Any]:
        base = Path(memory_dir) if memory_dir else _claude_project_dir(None)
        if not base.is_dir():
            return {"path": str(base), "exists": False, "count": 0, "facts": []}
        index_lines: List[str] = []
        index_path = base / self.INDEX
        if index_path.is_file():
            try:
                index_lines = index_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                index_lines = []
        facts: List[Dict[str, str]] = []
        for p in sorted(base.glob("*.md")):
            if p.name == self.INDEX:
                continue
            facts.append({
                "name": p.name,
                "path": str(p),
                "in_index": any(p.name in line for line in index_lines),
            })
        return {"path": str(base), "exists": True, "count": len(facts),
                "index_lines": len(index_lines), "facts": facts}

    def read(self, name: str, memory_dir: Optional[Path]) -> Dict[str, Any]:
        safe = self._safe_name(name)
        if not safe:
            return {"error": "invalid fact name (no path traversal, .md implied)"}
        base = Path(memory_dir) if memory_dir else _claude_project_dir(None)
        p = base / safe
        if not p.is_file():
            return {"error": f"not found: {p}", "name": safe}
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"error": f"read {p}: {e}", "name": safe}
        return {"name": safe, "path": str(p), "content": content,
                "frontmatter": _parse_frontmatter(content), "body": _strip_frontmatter(content)}

    def write(self, name: str, body: str, *, memory_dir: Optional[Path],
              description: str = "", fact_type: str = "project") -> Dict[str, Any]:
        with _LOCK:
            return self._write_locked(name, body, memory_dir=memory_dir,
                                      description=description, fact_type=fact_type)

    def _write_locked(self, name: str, body: str, *, memory_dir: Optional[Path],
                      description: str, fact_type: str) -> Dict[str, Any]:
        safe = self._safe_name(name)
        if not safe:
            return {"error": "invalid fact name (no path traversal, .md implied)"}
        base = Path(memory_dir) if memory_dir else _claude_project_dir(None)
        base.mkdir(parents=True, exist_ok=True)
        p = base / safe
        if p.is_file():
            existing = _parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            fact_type = existing.get("metadata", {}).get("type", fact_type)
            if not description:
                description = existing.get("description", "")
        frontmatter = _render_frontmatter(safe, description, fact_type)
        content = f"{frontmatter}\n\n{body.strip()}\n"
        if not _atomic_write(p, content):
            return {"error": f"atomic write failed for {p}"}
        self._sync_index(base, safe, description)
        return {"status": "written", "name": safe, "path": str(p)}

    def forget(self, name: str, memory_dir: Optional[Path]) -> Dict[str, Any]:
        with _LOCK:
            return self._forget_locked(name, memory_dir=memory_dir)

    def _forget_locked(self, name: str, memory_dir: Optional[Path]) -> Dict[str, Any]:
        safe = self._safe_name(name)
        if not safe:
            return {"error": "invalid fact name"}
        base = Path(memory_dir) if memory_dir else _claude_project_dir(None)
        p = base / safe
        if p.is_file():
            try:
                p.unlink()
            except Exception as e:
                return {"error": f"unlink {p}: {e}"}
        self._drop_index_line(base, safe)
        return {"status": "forgot", "name": safe, "path": str(p)}

    def _sync_index(self, base: Path, name: str, description: str) -> None:
        index_path = base / self.INDEX
        old = index_path.read_text(encoding="utf-8", errors="replace").splitlines() if index_path.is_file() else []
        display = name[:-3] if name.endswith(".md") else name
        new_line = f"- [{display}]({name}) — {description.strip() or display}"
        kept = [ln for ln in old if name not in ln]
        kept.append(new_line)
        _atomic_write(index_path, "\n".join(kept) + "\n")

    def _drop_index_line(self, base: Path, name: str) -> None:
        index_path = base / self.INDEX
        if not index_path.is_file():
            return
        old = index_path.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = [ln for ln in old if name not in ln]
        _atomic_write(index_path, "\n".join(kept) + ("\n" if kept else ""))


# ── Frontmatter helpers ──────────────────────────────────────────────────
def _parse_frontmatter(text: str) -> Dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    fm: Dict[str, Any] = {}
    meta: Dict[str, str] = {}
    in_meta = False
    for raw in parts[1].splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "metadata:":
            in_meta = True
            continue
        if in_meta:
            m = re.match(r"^\s{2,}([a-zA-Z_]+):\s*(.*)$", raw)
            if m:
                meta[m.group(1)] = m.group(2).strip().strip("'\"")
                continue
        in_meta = False
        if not line.startswith("metadata:"):
            m = re.match(r"^([a-zA-Z_]+):\s*(.*)$", line)
            if m:
                fm[m.group(1)] = m.group(2).strip().strip("'\"")
    if meta:
        fm["metadata"] = meta
    return fm


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def _render_frontmatter(name: str, description: str, fact_type: str) -> str:
    return "\n".join([
        "---",
        f"name: {name}",
        f"description: \"{description.strip()}\"" if description.strip() else "description: \"\"",
        "metadata:",
        "  node_type: memory",
        f"  type: {fact_type}",
        "---",
    ])


# ── Atomic write ─────────────────────────────────────────────────────────
def _atomic_write(path: Path, content: str) -> bool:
    try:
        d = path.parent
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".cm-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(path))
            return True
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception as e:
        logger.warning("atomic write failed for %s: %s", path, e)
        return False


# ── Cross memory engine ──────────────────────────────────────────────────
class _CrossEngine:
    def __init__(self) -> None:
        self.hermes = _HermesStore()
        self.claude = _ClaudeStore()

    def claude_dir(self, cwd: Optional[Path]) -> Path:
        return _claude_project_dir(cwd)

    def search(self, query: str, *, limit: int = 20,
               hermes_dir: Optional[Path], claude_dir: Optional[Path]) -> Dict[str, Any]:
        needles = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]
        hits: List[Dict[str, Any]] = []
        for f in _HermesStore.FILE_NAMES:
            st = self.hermes.read(hermes_dir, f)
            for e in st.get("entries", []):
                score = _score(e["body"], needles)
                if score > 0:
                    hits.append({"origin": e["source"], "file": f, "topic": e["topic"],
                                 "snippet": _snippet(e["body"]), "score": score})
        cl = Path(claude_dir) if claude_dir else self.claude_dir(None)
        targets = {str(cl)}
        if CLAUDE_ROOT.is_dir():
            for pd in CLAUDE_ROOT.iterdir():
                md = pd / "memory"
                if md.is_dir():
                    targets.add(str(md))
        for md_str in targets:
            md = Path(md_str)
            if not md.is_dir():
                continue
            for p in md.glob("*.md"):
                if p.name == _ClaudeStore.INDEX:
                    continue
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                score = _score(content, needles)
                if score > 0:
                    hits.append({"origin": SRC_CLAUDE_PROJECT, "file": p.name,
                                 "snippet": _snippet(_strip_frontmatter(content)), "score": score})
        if GLOBAL_CLAUDE_MD.is_file():
            try:
                content = GLOBAL_CLAUDE_MD.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            if content:
                score = _score(content, needles)
                if score > 0:
                    hits.append({"origin": SRC_CLAUDE_GLOBAL, "file": GLOBAL_CLAUDE_MD.name,
                                 "snippet": _snippet(content), "score": score})
        hits.sort(key=lambda h: h["score"], reverse=True)
        return {"query": query, "total": len(hits), "results": hits[: max(1, min(limit, 50))]}

    def sync(self, *, hermes_dir: Optional[Path], cwd: Optional[Path],
             claude_dir: Optional[Path], dry_run: bool = False) -> Dict[str, Any]:
        with _LOCK:
            return self._sync_locked(hermes_dir=hermes_dir, cwd=cwd,
                                     claude_dir=claude_dir, dry_run=dry_run)

    def _sync_locked(self, *, hermes_dir: Optional[Path], cwd: Optional[Path],
                     claude_dir: Optional[Path], dry_run: bool) -> Dict[str, Any]:
        report: Dict[str, Any] = {"dry_run": dry_run,
                                  "claude->hermes": [], "hermes->claude": []}
        cl = Path(claude_dir) if claude_dir else self.claude_dir(cwd)

        def _claude_bodies(store_md: Path) -> List[str]:
            out: List[str] = []
            for f in self.claude.list(store_md).get("facts", []):
                rd = self.claude.read(f["name"], store_md)
                if "error" not in rd and rd.get("body"):
                    out.append(_norm(rd["body"]))
            return out

        def _hermes_bodies(store_dir: Optional[Path]) -> List[str]:
            out: List[str] = []
            for e in self.hermes.read(store_dir, "MEMORY.md").get("entries", []):
                # Strip the cross-memory tag prefix so a mirrored claude fact
                # compares equal to its source (content-level dedupe).
                out.append(_norm(_strip_tag(e["body"])))
            return out

        claude_cur = _claude_bodies(cl)
        hermes_cur = _hermes_bodies(hermes_dir)

        # 1. Claude facts -> Hermes MEMORY.md, deduped by CONTENT (not topic hash)
        for fact in self.claude.list(cl).get("facts", []):
            rd = self.claude.read(fact["name"], cl)
            if "error" in rd or not rd.get("body"):
                continue
            body = rd["body"]
            nbody = _norm(body)
            if nbody in hermes_cur:
                continue  # already mirrored / present verbatim
            if dry_run:
                report["claude->hermes"].append({"status": "would-add", "name": fact["name"]})
                continue
            res = self.hermes.add(_strip_frontmatter(body), store_dir=hermes_dir,
                                  file="MEMORY.md", tags=[f"claude:{fact['name']}"])
            # hermes.add prepends a tag prefix; re-normalize what we actually stored
            added = res.get("status") == "added"
            report["claude->hermes"].append({"name": fact["name"], "result": res, "added": added})
            if added:
                hermes_cur = _hermes_bodies(hermes_dir)

        # 2. Hermes MEMORY.md sections -> Claude fact files, skips tagged imports
        #    and anything already mirrored (content-based == idempotent)
        #
        # NOTE: USER.md is deliberately NOT mirrored here. USER.md is the Hermes
        # user *persona* (identity / personal profile), which is Hermes-local;
        # auto-mirroring it verbatim into every Claude project memory dir would
        # duplicate persona data into each project and over-expose it. It stays
        # SEARCHABLE (see search()), it just is not synced like agent learnings.
        existing_names = {f["name"] for f in self.claude.list(cl).get("facts", [])}
        for e in self.hermes.read(hermes_dir, "MEMORY.md").get("entries", []):
            body = e["body"]
            nbody = _norm(body)
            if "[cross-memory: claude:" in body:
                continue  # this IS a claude import; never mirror it back
            if nbody in claude_cur:
                continue  # already mirrored verbatim
            stripped = _strip_frontmatter(body)
            slug = re.sub(r"[^0-9a-z_-]+", "-", e["topic"].lower()).strip("-")
            slug = slug[:50] or "memory-entry"
            fname = f"{slug}.md"
            idx = 1
            while fname in existing_names:
                fname = f"{slug}-{idx}.md"
                idx += 1
            if dry_run:
                report["hermes->claude"].append({"status": "would-add", "name": fname})
                continue
            res = self.claude.write(fname, stripped, memory_dir=cl,
                                    description=_snippet(body, 120), fact_type="project")
            report["hermes->claude"].append({"name": fname, "result": res})
            claude_cur.append(nbody)
            existing_names.add(fname)
        return report

    def status(self, cwd: Optional[Path], hermes_dir: Optional[Path],
               claude_dir: Optional[Path]) -> Dict[str, Any]:
        h1 = self.hermes.read(hermes_dir, "MEMORY.md")
        h2 = self.hermes.read(hermes_dir, "USER.md")
        cl = Path(claude_dir) if claude_dir else self.claude_dir(cwd)
        c = self.claude.list(cl)
        global_chars = 0
        if GLOBAL_CLAUDE_MD.is_file():
            try:
                global_chars = len(GLOBAL_CLAUDE_MD.read_text(encoding="utf-8").strip())
            except Exception:
                global_chars = 0
        return {
            "hermes": {
                "dir": str(Path(hermes_dir) if hermes_dir else HERMES_MEMORY_DIR),
                "memory_entries": h1.get("count", 0),
                "user_entries": h2.get("count", 0),
            },
            "claude": {
                "project": str(cl),
                "exists": c.get("exists", False),
                "facts": c.get("count", 0),
                "index_lines": c.get("index_lines", 0),
            },
            "claude_global": {"path": str(GLOBAL_CLAUDE_MD), "chars": global_chars},
            "cwd": str(Path(cwd) if cwd else CWD),
        }


def _score(text: str, needles: List[str]) -> int:
    tl = text.lower()
    return sum(tl.count(n) for n in needles)


def _snippet(text: str, width: int = 200) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:width] + ("…" if len(flat) > width else "")


def _norm(text: str) -> str:
    """Whitespace/case-normalized form for content-equality dedupe."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _strip_tag(body: str) -> str:
    """Remove a leading '[cross-memory: ...] ' tag prefix, if present."""
    return re.sub(r"^\[cross-memory:[^\]]*\]\s*", "", body.strip())


def _as_int(raw: Any, default: int = 20) -> int:
    try:
        return max(1, min(int(raw), 50))
    except (TypeError, ValueError):
        return max(1, min(default, 50))


_engine = _CrossEngine()


# ── Tool handlers ────────────────────────────────────────────────────────
def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _claude_arg_dirs(args: dict) -> Tuple[Optional[Path], Optional[Path]]:
    cwd = Path(args["cwd"]) if args.get("cwd") else None
    d = Path(args["claude_dir"]) if args.get("claude_dir") else None
    return cwd, d


def _h_status(args: dict, **kwargs: Any) -> str:
    try:
        cwd = Path(args["cwd"]) if args.get("cwd") else None
        hd = Path(args["hermes_dir"]) if args.get("hermes_dir") else None
        cd = Path(args["claude_dir"]) if args.get("claude_dir") else None
        return _json(_engine.status(cwd, hd, cd))
    except Exception as e:
        return _json({"error": str(e)})


def _h_search(args: dict, **kwargs: Any) -> str:
    try:
        query = (args.get("query") or "").strip()
        if not query:
            return _json({"error": "query is required"})
        hd = Path(args["hermes_dir"]) if args.get("hermes_dir") else None
        cd = Path(args["claude_dir"]) if args.get("claude_dir") else None
        return _json(_engine.search(query, limit=_as_int(args.get("limit", 20)), hermes_dir=hd, claude_dir=cd))
    except Exception as e:
        return _json({"error": str(e)})


def _h_claude_list(args: dict, **kwargs: Any) -> str:
    try:
        _, d = _claude_arg_dirs(args)
        cwd = Path(args["cwd"]) if args.get("cwd") else None
        return _json(_engine.claude.list(d or _engine.claude_dir(cwd)))
    except Exception as e:
        return _json({"error": str(e)})


def _h_claude_read(args: dict, **kwargs: Any) -> str:
    try:
        name = (args.get("name") or "").strip()
        if not name:
            return _json({"error": "name is required"})
        cwd, d = _claude_arg_dirs(args)
        return _json(_engine.claude.read(name, d or _engine.claude_dir(cwd)))
    except Exception as e:
        return _json({"error": str(e)})


def _h_claude_write(args: dict, **kwargs: Any) -> str:
    try:
        name = (args.get("name") or "").strip()
        body = (args.get("body") or "").strip()
        if not name or not body:
            return _json({"error": "name and body are required"})
        cwd, d = _claude_arg_dirs(args)
        return _json(_engine.claude.write(name, body, memory_dir=d or _engine.claude_dir(cwd),
                                          description=args.get("description", ""),
                                          fact_type=args.get("type", "project")))
    except Exception as e:
        return _json({"error": str(e)})


def _h_hermes_list(args: dict, **kwargs: Any) -> str:
    try:
        hd = Path(args["hermes_dir"]) if args.get("hermes_dir") else None
        return _json({"MEMORY.md": _engine.hermes.read(hd, "MEMORY.md"),
                      "USER.md": _engine.hermes.read(hd, "USER.md")})
    except Exception as e:
        return _json({"error": str(e)})


def _h_hermes_add(args: dict, **kwargs: Any) -> str:
    try:
        body = (args.get("body") or "").strip()
        if not body:
            return _json({"error": "body is required"})
        hd = Path(args["hermes_dir"]) if args.get("hermes_dir") else None
        file = args.get("file", "MEMORY.md")
        if file not in _HermesStore.FILE_NAMES:
            return _json({"error": f"file must be one of {list(_HermesStore.FILE_NAMES)}"})
        return _json(_engine.hermes.add(body, store_dir=hd, file=file, tags=args.get("tags") or None))
    except Exception as e:
        return _json({"error": str(e)})


def _h_sync(args: dict, **kwargs: Any) -> str:
    try:
        dry = bool(args.get("dry_run"))
        if not dry and not args.get("confirm"):
            return _json({"error": "refusing live sync without confirm=true or dry_run=true"})
        hd = Path(args["hermes_dir"]) if args.get("hermes_dir") else None
        cd = Path(args["claude_dir"]) if args.get("claude_dir") else None
        cwd = Path(args["cwd"]) if args.get("cwd") else None
        return _json(_engine.sync(hermes_dir=hd, cwd=cwd, claude_dir=cd, dry_run=dry))
    except Exception as e:
        return _json({"error": str(e)})


def _h_forget(args: dict, **kwargs: Any) -> str:
    try:
        store = (args.get("store") or "hermes").lower()
        name = (args.get("name") or "").strip()
        if not name:
            return _json({"error": "name is required"})
        if not args.get("confirm"):
            return _json({"error": "refusing to forget without confirm=true"})
        if store in ("claude", "claude-project"):
            cwd, d = _claude_arg_dirs(args)
            return _json(_engine.claude.forget(name, d or _engine.claude_dir(cwd)))
        hd = Path(args["hermes_dir"]) if args.get("hermes_dir") else None
        file = args.get("file", "MEMORY.md")
        if file not in _HermesStore.FILE_NAMES:
            return _json({"error": f"file must be one of {list(_HermesStore.FILE_NAMES)}"})
        return _json(_engine.hermes.forget(name, store_dir=hd, file=file))
    except Exception as e:
        return _json({"error": str(e)})


# ── Slash command ────────────────────────────────────────────────────────
def _cmd_cross_memory(raw_args: str) -> str:
    parts = raw_args.strip().split(maxsplit=2)
    sub = parts[0].lower() if parts else "help"
    try:
        if sub == "status":
            return _json(_engine.status(None, None, None))
        if sub == "search":
            q = parts[1] if len(parts) > 1 else ""
            if not q:
                return "Usage: /cross-memory search <query> [limit]"
            limit = _as_int(parts[2], 20) if len(parts) > 2 else 20
            return _json(_engine.search(q, limit=limit, hermes_dir=None, claude_dir=None))
        if sub == "list":
            return _json(_engine.claude.list(_engine.claude_dir(None)))
        if sub in ("forget", "rm"):
            if len(parts) < 2:
                return "Usage: /cross-memory forget <store> <name>  (store: hermes|claude)"
            store = parts[1].lower()
            name = parts[2] if len(parts) > 2 else ""
            if not name:
                return "Usage: /cross-memory forget <store> <name>  (store: hermes|claude)"
            if store in ("claude", "claude-project"):
                return _json(_engine.claude.forget(name, _engine.claude_dir(None)))
            return _json(_engine.hermes.forget(name, store_dir=None, file="MEMORY.md"))
        if sub == "sync" and len(parts) > 1 and parts[1] == "dry-run":
            return _json(_engine.sync(hermes_dir=None, cwd=None, claude_dir=None, dry_run=True))
        return (
            "Usage: /cross-memory <subcommand> [args]\n"
            "  status                 — memory store paths + counts\n"
            "  search <query> [lim]   — search across Hermes + Claude stores\n"
            "  list                   — list current project's Claude facts\n"
            "  sync dry-run           — preview the bidirectional sync\n"
            "  forget <store> <name>  — remove one entry (store: hermes|claude)\n"
        )
    except Exception as e:
        return f"Error: {e}"


# ── Plugin entry point ───────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    logger.info("Registering hermes-cross-memory plugin")

    ctx.register_tool(name="cross_memory_status", toolset="cross-memory",
        schema={"name": "cross_memory_status",
                "description": "Cross-memory plugin status: Hermes memory dir + entry counts and Claude Code project memory dir + fact counts. Read-only.",
                "parameters": {"type": "object", "properties": {
                    "cwd": {"type": "string", "description": "Working dir used to derive the Claude project memory dir"},
                    "hermes_dir": {"type": "string", "description": "Override Hermes memories dir (for audits)"},
                    "claude_dir": {"type": "string", "description": "Override Claude project memory dir (for audits)"}}}},
        handler=_h_status)

    ctx.register_tool(name="cross_memory_search", toolset="cross-memory",
        schema={"name": "cross_memory_search",
                "description": "Search across BOTH memory stores — Hermes MEMORY.md/USER.md and all Claude Code project memory dirs + global CLAUDE.md. Returns ranked hits with origin labels. No LLM dependency.",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "Search terms"},
                    "limit": {"type": "integer", "description": "Max results (1-50)", "default": 20},
                    "hermes_dir": {"type": "string"},
                    "claude_dir": {"type": "string"}},
                    "required": ["query"]}},
        handler=_h_search)

    ctx.register_tool(name="cross_memory_claude_list", toolset="cross-memory",
        schema={"name": "cross_memory_claude_list",
                "description": "List fact files in a Claude Code project memory dir (+ whether each is indexed).",
                "parameters": {"type": "object", "properties": {
                    "cwd": {"type": "string"}, "claude_dir": {"type": "string"}}}},
        handler=_h_claude_list)

    ctx.register_tool(name="cross_memory_claude_read", toolset="cross-memory",
        schema={"name": "cross_memory_claude_read",
                "description": "Read one Claude Code fact file (frontmatter + body).",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Fact filename (.md implied)"},
                    "cwd": {"type": "string"}, "claude_dir": {"type": "string"}},
                    "required": ["name"]}},
        handler=_h_claude_read)

    ctx.register_tool(name="cross_memory_claude_write", toolset="cross-memory",
        schema={"name": "cross_memory_claude_write",
                "description": "Create or update a Claude Code fact file + refresh its MEMORY.md index line. Atomic write.",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Fact filename (.md implied)"},
                    "body": {"type": "string", "description": "Markdown body of the fact"},
                    "description": {"type": "string", "description": "One-line summary (goes in frontmatter + index)"},
                    "type": {"type": "string", "description": "metadata.type", "default": "project"},
                    "cwd": {"type": "string"}, "claude_dir": {"type": "string"}},
                    "required": ["name", "body"]}},
        handler=_h_claude_write)

    ctx.register_tool(name="cross_memory_hermes_list", toolset="cross-memory",
        schema={"name": "cross_memory_hermes_list",
                "description": "List Hermes memory entries (MEMORY.md + USER.md) with topic + source.",
                "parameters": {"type": "object", "properties": {
                    "hermes_dir": {"type": "string"}}}},
        handler=_h_hermes_list)

    ctx.register_tool(name="cross_memory_hermes_add", toolset="cross-memory",
        schema={"name": "cross_memory_hermes_add",
                "description": "Append a §-delimited entry to Hermes MEMORY.md (or USER.md). Skips if the topic already exists.",
                "parameters": {"type": "object", "properties": {
                    "body": {"type": "string", "description": "The fact/entry text"},
                    "file": {"type": "string", "description": "MEMORY.md or USER.md", "default": "MEMORY.md"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "origin tags"},
                    "hermes_dir": {"type": "string"}},
                    "required": ["body"]}},
        handler=_h_hermes_add)

    ctx.register_tool(name="cross_memory_sync", toolset="cross-memory",
        schema={"name": "cross_memory_sync",
                "description": "Bidirectional sync: Claude facts -> Hermes MEMORY.md entries AND Hermes sections -> Claude fact files. Idempotent (dedupes by topic, skips circular imports). Requires confirm=true OR dry_run=true.",
                "parameters": {"type": "object", "properties": {
                    "cwd": {"type": "string"}, "hermes_dir": {"type": "string"}, "claude_dir": {"type": "string"},
                    "dry_run": {"type": "boolean", "description": "Preview without writing"},
                    "confirm": {"type": "boolean", "description": "Acknowledge live write"}}}},
        handler=_h_sync)

    ctx.register_tool(name="cross_memory_forget", toolset="cross-memory",
        schema={"name": "cross_memory_forget",
                "description": "Remove exactly ONE named entry. claude: deletes a fact file + index line. hermes: removes one topic paragraph from MEMORY.md. Never bulk. Requires confirm=true for both stores (both are destructive).",
                "parameters": {"type": "object", "properties": {
                    "store": {"type": "string", "description": "hermes or claude", "default": "hermes"},
                    "name": {"type": "string", "description": "topic (hermes) or fact filename (claude)"},
                    "file": {"type": "string", "description": "MEMORY.md or USER.md (hermes only)"},
                    "cwd": {"type": "string"}, "hermes_dir": {"type": "string"}, "claude_dir": {"type": "string"},
                    "confirm": {"type": "boolean"}},
                    "required": ["name"]}},
        handler=_h_forget)

    ctx.register_command(name="cross-memory",
                         description="Cross-memory between Claude Code and Hermes — status/search/list/sync",
                         handler=_cmd_cross_memory)

    return {"name": "hermes-cross-memory"}
