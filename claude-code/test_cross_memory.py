#!/usr/bin/env python3
"""Verification for hermes-cross-memory — runs entirely against TEMP dirs,
never the live ~/.hermes or ~/.claude stores."""
import importlib
import os
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

# S13 circular-import guard across a real CLI-form sync (temp dirs)
_tmpC = Path(tempfile.mkdtemp(prefix="xmem-circ-"))
_hdC = _tmpC / "h"; _hdC.mkdir(parents=True)
_cdC = _tmpC / "c"; _cdC.mkdir(parents=True)
eng.claude.write("myfact.md", "core learning about pgbouncer", memory_dir=_cdC, description="db learn")
_r1 = eng.sync(hermes_dir=_hdC, cwd=Path("/f"), claude_dir=_cdC, dry_run=False)
_r2 = eng.sync(hermes_dir=_hdC, cwd=Path("/f"), claude_dir=_cdC, dry_run=False)
_cfC = [f["name"] for f in eng.claude.list(_cdC)["facts"]]
check("S13 circular-import guard: 2nd sync idempotent", len(_r2["claude->hermes"])==0 and len(_r2["hermes->claude"])==0)
check("S13 no fact duplicated back to claude", len(_cfC)==1 and _cfC[0]=="myfact.md")
shutil.rmtree(_tmpC, ignore_errors=True)

# S14 cross-process worst-case: two SEPARATE processes (writer + syncer) share a
# store. The RLock is thread-only; this proves atomic os.replace bounds the race
# to last-writer-wins (a lost update), never a torn/corrupt file.
import subprocess as _sp
_d = Path(tempfile.mkdtemp(prefix="xmem-xproc-"))
_writer = (
    "import sys;from pathlib import Path;m=__import__('hermes-cross-memory');"
    "e=m._CrossEngine();D=__import__('os').environ['D'];"
    "[(e.hermes.add(f'procA{i} alpha',store_dir=Path(D)/'h',file='MEMORY.md')) for i in range(20)]"
)
_syncer = (
    "import sys;from pathlib import Path;m=__import__('hermes-cross-memory');"
    "e=m._CrossEngine();D=__import__('os').environ['D'];"
    "[e.sync(hermes_dir=Path(D)/'h',cwd=Path('/x'),claude_dir=Path(D)/'c',dry_run=False) for _ in range(4)]"
)
_env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "plugins"), "D": str(_d)}
_p1 = _sp.Popen([sys.executable, "-c", _writer], env=_env)
_p2 = _sp.Popen([sys.executable, "-c", _syncer], env=_env)
_p1.wait(60); _p2.wait(60)
_raw = (_d / "h" / "MEMORY.md").read_text() if (_d / "h" / "MEMORY.md").exists() else ""
import re as _re
_paras = [p.strip() for p in _re.split(r"\n?\u00a7\n?", _raw) if p.strip()]
_degen = [p for p in _paras if 0 < len(p) < 3]
check("S14 cross-process: no torn/degenerate paragraphs", _p1.returncode == 0 and _p2.returncode == 0 and len(_degen) == 0)
_xidx = _d / "c" / "MEMORY.md"
_xbad = 0
if _xidx.exists():
    _xbad = len([l for l in _xidx.read_text().splitlines() if l.strip() and not l.strip().startswith("-")])
check("S14 cross-process: index not corrupted", _xbad == 0)
shutil.rmtree(_d, ignore_errors=True)

# S15 single-side-forget coherence: forgetting a fact on ONE side does not
# propagate (each side is authoritative for its origin) AND re-sync must not
# zombie-resurrect it into Claude (tagged imports are never mirrored back; the
# 'already mirrored' content-dedupe also guards the untagged case).
_tmpD = Path(tempfile.mkdtemp(prefix="xmem-forget-"))
_hdD = _tmpD / "h"; _hdD.mkdir(parents=True)
_cdD = _tmpD / "c"; _cdD.mkdir(parents=True)
eng.claude.write("stale.md", "old learning that changed", memory_dir=_cdD, description="v1")
eng.sync(hermes_dir=_hdD, cwd=Path("/x"), claude_dir=_cdD, dry_run=False)
eng.claude.forget("stale.md", _cdD)  # delete from claude only
eng.sync(hermes_dir=_hdD, cwd=Path("/x"), claude_dir=_cdD, dry_run=False)  # re-sync
check("S15 claude-side forget not resurrected by re-sync",
      eng.claude.list(_cdD)["count"] == 0)
# reverse: forget from hermes, claude keeps it, re-sync does not re-add (content dedupe)
eng2 = mod._CrossEngine()
_hdR = _tmpD / "h2"; _hdR.mkdir(parents=True)
_cdR = _tmpD / "c2"; _cdR.mkdir(parents=True)
eng2.hermes.add("untagged hermes-only fact", store_dir=_hdR, file="MEMORY.md")
eng2.sync(hermes_dir=_hdR, cwd=Path("/x"), claude_dir=_cdR, dry_run=False)
eng2.hermes.forget(eng2.hermes._topic("untagged hermes-only fact"), store_dir=_hdR, file="MEMORY.md")
eng2.sync(hermes_dir=_hdR, cwd=Path("/x"), claude_dir=_cdR, dry_run=False)
check("S15 hermes forget, claude keeps + no re-add on re-sync",
      eng2.claude.list(_cdR)["count"] == 1)
shutil.rmtree(_tmpD, ignore_errors=True)

shutil.rmtree(tmp, ignore_errors=True)
if fails:
    print(f"\n{len(fails)} FAILURES:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
sys.exit(0)
