#!/usr/bin/env python3
"""Verification for hermes-cross-memory — runs entirely against TEMP dirs,
never the live ~/.hermes or ~/.claude stores."""
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

mod = importlib.import_module("hermes-cross-memory")
eng = mod._CrossEngine()

tmp = tempfile.mkdtemp(prefix="xmem-test-")
hermes_dir = Path(tmp) / "hermes" / "memories"
hermes_dir.mkdir(parents=True)
claude_root = Path(tmp) / "claude" / "projects"
claude_root.mkdir(parents=True)
claude_dir = claude_root / "hermes-ultimate-coding" / "memory"

fails = []
def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)

# Seed a Hermes memory entry (temp copy)
r = eng.hermes.add("cross-memory test fact: the quick brown fox jumps over the lazy dog",
                   store_dir=hermes_dir, file="MEMORY.md")
check("hermes add", r.get("status") == "added")
r2 = eng.hermes.add("cross-memory test fact: the quick brown fox jumps over the lazy dog",
                    store_dir=hermes_dir, file="MEMORY.md")
check("hermes add dedupes", r2.get("status") == "skipped")

# Claude write (temp copy)
cw = eng.claude.write("test-fact.md", "Another test fact about pgbouncer session drops",
                      memory_dir=claude_dir, description="test")
check("claude write", cw.get("status") == "written")
cl = eng.claude.list(claude_dir)
check("claude list has 1 fact + index", cl.get("count") == 1 and cl.get("index_lines") >= 1)
cr = eng.claude.read("test-fact.md", claude_dir)
check("claude read body", cr.get("body", "").startswith("Another test"))

# Cross search
s = eng.search("pgbouncer", limit=10, hermes_dir=hermes_dir, claude_dir=claude_dir)
check("search finds claude hit", any(h["origin"] == "claude-project" for h in s["results"]))
s2 = eng.search("brown fox", limit=10, hermes_dir=hermes_dir, claude_dir=claude_dir)
check("search finds hermes hit", any(h["origin"] == "hermes-memory" for h in s2["results"]))

# Bidirectional sync (dry-run then live, temp only)
dr = eng.sync(hermes_dir=hermes_dir, cwd=Path("/fake/cwd"), claude_dir=claude_dir, dry_run=True)
check("sync dry-run no write", dr.get("dry_run") is True)
live = eng.sync(hermes_dir=hermes_dir, cwd=Path("/fake/cwd"), claude_dir=claude_dir, dry_run=False)
check("sync wrote claude->hermes", len(live["claude->hermes"]) == 1)
check("sync wrote hermes->claude", len(live["hermes->claude"]) == 1)

# Idempotency + circular-import guard: second live sync adds nothing
live2 = eng.sync(hermes_dir=hermes_dir, cwd=Path("/fake/cwd"), claude_dir=claude_dir, dry_run=False)
check("sync idempotent", len(live2["claude->hermes"]) == 0 and len(live2["hermes->claude"]) == 0)

# After one full sync: MEMORY.md has the original + the claude import
hm = eng.hermes.read(hermes_dir, "MEMORY.md")
check("sync mirrored claude fact into hermes", len(hm["entries"]) == 2)

# Forget claude fact
fg = eng.claude.forget("test-fact.md", claude_dir)
check("claude forget", fg.get("status") == "forgot")
cl2 = eng.claude.list(claude_dir)
check("claude fact removed (1 remaining)", cl2.get("count") == 1)

# Forget hermes entry — remove the FIRST (original) entry, leave the tagged import
orig_body = next(e["body"] for e in eng.hermes.read(hermes_dir, "MEMORY.md")["entries"]
                  if not e["body"].startswith("[cross-memory:"))
fh = eng.hermes.forget(eng.hermes._topic(orig_body), store_dir=hermes_dir, file="MEMORY.md")
check("hermes forget removes", fh.get("status") == "forgot")
check("hermes forget leaves tagged import only", eng.hermes.read(hermes_dir, "MEMORY.md").get("count") == 1)

# Status
st = eng.status(Path("/fake/cwd"), hermes_dir, claude_dir)
check("status reports paths + counts", st["hermes"]["memory_entries"] >= 0 and st["claude"]["project"])

# Path safety: reject traversal + absolute fact names
safe = eng.claude.write("../../etc/passwd", "x", memory_dir=claude_dir)
check("claude write rejects traversal", "error" in safe)
safe2 = eng.claude.write("/abs/path.md", "x", memory_dir=claude_dir)
check("claude write rejects absolute", "error" in safe2)

# S11 regression: tags delivered as a STRING (MCP can) must not corrupt to per-char
import tempfile as _tf
_tmpd = _tf.mkdtemp(prefix="xmem-tags-")
_td = Path(_tmpd) / "memories"; _td.mkdir(parents=True)
_tags_res = eng.hermes.add("s11 fact body", store_dir=_td, file="MEMORY.md", tags="claude:myfact.md")
_tags_line = eng.hermes.read(_td, "MEMORY.md")["entries"][0]["body"]
check("S11 tags-as-string not per-char corrupted",
      _tags_line.startswith("[cross-memory: claude:myfact.md]") and "c, l, a" not in _tags_line)
shutil.rmtree(_tmpd, ignore_errors=True)

# S12 regression: an index filename must not be usable as a FACT name (clobber)
_res12 = eng.claude.write("MEMORY.md", "clobber", memory_dir=claude_dir, description="boom")
check("S12 MEMORY.md reserved as index (fact name rejected)", "error" in _res12)
_res12b = eng.claude.write("USER.md", "clobber", memory_dir=claude_dir, description="boom")
check("S12 USER.md reserved too", "error" in _res12b)

shutil.rmtree(tmp, ignore_errors=True)
if fails:
    print(f"\n{len(fails)} FAILURES:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
sys.exit(0)
