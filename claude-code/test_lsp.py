#!/usr/bin/env python3
"""
End-to-end regression test for hermes-lsp, driven through the MCP bridge.

Written after finding that the LSP request path had never worked:

* ``_check_server_available`` ran ``which`` and returned True unconditionally,
  ignoring the exit status — so every one of the 49 languages reported as
  installed, and ``get_client_for_file`` happily tried to spawn binaries that
  were not there.
* Servers installed by the plugin's own ``npm install -g`` live in the
  Hermes-managed prefix, which is not necessarily on the host process's PATH.
* The writer sent bare ``json.dumps(...) + "\\n"``. LSP requires
  ``Content-Length`` framing, and the same plugin's reader already expected it.
* That str was written to a pipe opened with ``text=False``, raising
  "a bytes-like object is required, not 'str'" on every request, starting with
  ``initialize``.

The failure was silent in the worst possible way: ``lsp_diagnostics`` returned
``success: true`` with zero diagnostics, which reads as "this file is clean"
rather than "nothing checked this file".

The load-bearing assertion here is therefore not "the call succeeded" but
"a known-bad file actually produced the expected diagnostic".

Usage:
    <hermes-venv>/bin/python claude-code/test_lsp.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LAUNCH = os.path.join(HERE, "launch.sh")

_results: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


class Bridge:
    def __init__(self, cwd: str):
        self.proc = subprocess.Popen(
            [LAUNCH], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=cwd,
        )
        self._id = 0

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 180.0) -> dict:
        self._id += 1
        rid = self._id
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n"
        )
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError("bridge closed stdout")
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg
        raise TimeoutError(method)

    def call(self, name: str, args: dict) -> Dict[str, Any]:
        r = self.request("tools/call", {"name": name, "arguments": args})
        return json.loads(r["result"]["content"][0]["text"])

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


BROKEN = "def f():\n    return undefined_name_xyz + 1\n"
CLEAN = "def f() -> int:\n    return 1 + 1\n"


def main() -> int:
    print("=" * 72)
    print("hermes-lsp — end-to-end regression test")
    print("=" * 72)

    # A real project root: the server needs one, and /tmp is not a project.
    workdir = tempfile.mkdtemp(prefix="hermes-lsp-test-", dir=REPO)
    os.makedirs(os.path.join(workdir, ".git"), exist_ok=True)  # root marker
    broken = os.path.join(workdir, "broken_module.py")
    clean = os.path.join(workdir, "clean_module.py")
    with open(broken, "w") as fh:
        fh.write(BROKEN)
    with open(clean, "w") as fh:
        fh.write(CLEAN)

    b = Bridge(cwd=workdir)
    try:
        b.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

        # --- availability must be honest -------------------------------
        servers = b.call("lsp_servers", {})
        available = servers.get("available", [])
        unavailable = servers.get("unavailable", [])
        names = {s.get("language") for s in available}

        # The regression this guards: _check_server_available used to return
        # True unconditionally, so every language landed in "available" and
        # "unavailable" was empty. On any real machine some servers are absent.
        check(
            "lsp_servers reports genuinely-missing servers as unavailable",
            len(unavailable) > 0,
            f"{len(available)} available, {len(unavailable)} not installed",
        )
        if not names:
            print("\nNo language servers installed — skipping the analysis checks.")
            return 0 if all(ok for _, ok in _results) else 1

        if "python" not in names:
            print("\nNo Python server installed — skipping the analysis checks.")
            return 0 if all(ok for _, ok in _results) else 1

        # --- the load-bearing assertion --------------------------------
        t0 = time.time()
        diag = b.call("lsp_diagnostics", {"filepath": broken})
        elapsed = time.time() - t0

        check("lsp_diagnostics reports analyzed=True", diag.get("analyzed") is True, str(diag)[:120])
        errs = diag.get("errors", [])
        check(
            "a real error is detected in a known-bad file",
            diag.get("summary", {}).get("errors", 0) >= 1,
            f"{diag.get('summary')} in {elapsed:.1f}s",
        )
        check(
            "the diagnostic names the undefined symbol",
            any("undefined_name_xyz" in (e.get("message") or "") for e in errs),
            (errs[0].get("message") if errs else "no errors returned"),
        )

        # A clean file must be reported clean AND marked analyzed, so that
        # "no problems" is distinguishable from "never checked".
        cd = b.call("lsp_diagnostics", {"filepath": clean})
        check("a clean file is analyzed", cd.get("analyzed") is True, str(cd.get("summary")))
        check("a clean file reports no errors", cd.get("summary", {}).get("errors", 0) == 0)

        # --- request/response path (all of these need correct framing) --
        hov = b.call("lsp_hover", {"filepath": broken, "line": 1, "character": 12})
        check("lsp_hover completes a request round-trip", hov.get("success") is True)

        comp = b.call("lsp_completions", {"filepath": clean, "line": 1, "character": 11})
        check("lsp_completions completes a request round-trip", comp.get("success") is True)

        # --- refresh path must not burn the whole timeout ----------------
        t0 = time.time()
        r = b.call("lsp_diagnostics", {"filepath": broken, "content": BROKEN})
        refresh_elapsed = time.time() - t0
        check(
            "refresh returns promptly (waits on a new publication, not a count change)",
            refresh_elapsed < 4.0 and r.get("summary", {}).get("errors", 0) >= 1,
            f"{refresh_elapsed:.1f}s",
        )

        # editing the file to be clean must be reflected
        t0 = time.time()
        r2 = b.call("lsp_diagnostics", {"filepath": broken, "content": CLEAN})
        check(
            "fixing the content clears the diagnostic",
            r2.get("summary", {}).get("errors", 0) == 0,
            f"{r2.get('summary')} in {time.time()-t0:.1f}s",
        )
    finally:
        b.close()
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for _, ok in _results if ok)
    print()
    print("=" * 72)
    print(f"{passed}/{len(_results)} checks passed")
    print("=" * 72)
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
