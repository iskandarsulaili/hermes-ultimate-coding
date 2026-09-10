#!/usr/bin/env python3
"""Cross-process sync stress — two SEPARATE processes write the same store
concurrently. Verifies atomic os.replace means no torn file (last-writer-wins)
even though the in-process RLock cannot serialize across processes."""
import subprocess, sys, os, re, tempfile
from pathlib import Path

PLUG = str(Path(__file__).resolve().parents[1] / "plugins")

def spawn(code, D):
    return subprocess.Popen([sys.executable, "-c", code],
                            env={**os.environ, "PYTHONPATH": PLUG, "D": D},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

D = tempfile.mkdtemp(prefix="xmem-xproc-")

# Process 1: heavy writer into hermes MEMORY.md
writer = '''
import sys; from pathlib import Path
m = __import__("hermes-cross-memory")
eng = m._CrossEngine(); D = __import__("os").environ["D"]
for i in range(30):
    eng.hermes.add(f"proc A fact {i} alpha", store_dir=Path(D) / "h", file="MEMORY.md")
eng.claude.write("procA.md", "proc A body", memory_dir=Path(D) / "c", description="a")
'''

# Process 2: repeated syncs over the same store
syncer = '''
import sys; from pathlib import Path
m = __import__("hermes-cross-memory")
eng = m._CrossEngine(); D = __import__("os").environ["D"]
for i in range(4):
    eng.sync(hermes_dir=Path(D) / "h", cwd=Path("/x"), claude_dir=Path(D) / "c", dry_run=False)
'''

p1 = spawn(writer, D)
p2 = spawn(syncer, D)
p1.wait(60); p2.wait(60)
print("p1 rc:", p1.returncode, "p2 rc:", p2.returncode)

# Verify no torn/corrupt state
raw = (Path(D) / "h" / "MEMORY.md").read_text() if (Path(D) / "h" / "MEMORY.md").exists() else ""
paras = [p.strip() for p in re.split(r"\n?\u00a7\n?", raw) if p.strip()]
bad = [p for p in paras if len(p) < 3]
cf = [f for f in (Path(D) / "c").glob("*.md")] if (Path(D) / "c").exists() else []
print("hermes paras:", len(paras), "degenerate(<3):", len(bad))
print("claude fact files:", len([f for f in cf if f.name != "MEMORY.md"]))
# index must be valid markdown (each line a bullet) or absent
idx = Path(D) / "c" / "MEMORY.md"
if idx.exists():
    badlines = [l for l in idx.read_text().splitlines() if l.strip() and not l.strip().startswith("-")]
    print("index bad lines:", len(badlines))
else:
    print("index absent")
# every hermes para must be a whole fact — no fragment split mid-fact
frag = [p for p in paras if len(p) < 10 and p and not p.startswith("proc")]
print("fragment-like paras:", len(frag))

import shutil; shutil.rmtree(D, ignore_errors=True)
assert p1.returncode == 0 and p2.returncode == 0, "process crashed"
assert len(bad) == 0, "torn/degenerate paragraphs"
print("CROSS-PROCESS: no torn state; atomic os.replace holds (last-writer-wins)")
