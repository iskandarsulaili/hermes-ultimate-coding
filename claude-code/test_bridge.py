#!/usr/bin/env python3
"""
End-to-end test for hermes_mcp_bridge.

Spawns the bridge exactly as Claude Code does — as a subprocess speaking
newline-delimited JSON-RPC on stdio — and exercises the real protocol against
the real Hermes plugin registry.  Nothing here is mocked.

The most important assertion is protocol integrity: Hermes plugins log
verbosely and some libraries print to fd 1, so every byte the child writes to
stdout must still parse as a JSON-RPC frame.  If that ever regresses, Claude
Code drops the server with an opaque error, so it is asserted explicitly.

Usage:
    <hermes-venv>/bin/python claude-code/test_bridge.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "hermes_mcp_bridge.py")

PASS = "PASS"
FAIL = "FAIL"

_results: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    status = PASS if ok else FAIL
    line = f"[{status}] {name}"
    if detail and not ok:
        line += f"\n        {detail}"
    elif detail:
        line += f"  ({detail})"
    print(line, flush=True)
    return ok


class Bridge:
    """A live bridge subprocess."""

    def __init__(self, env_overrides: Optional[Dict[str, str]] = None):
        env = dict(os.environ)
        env.setdefault("HERMES_HOME", os.path.expanduser("~/.hermes"))
        env["HERMES_MCP_CALL_TIMEOUT"] = env.get("HERMES_MCP_CALL_TIMEOUT", "120")
        if env_overrides:
            env.update(env_overrides)
        self.proc = subprocess.Popen(
            [sys.executable, BRIDGE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self._next_id = 0
        self.raw_stdout_lines: List[str] = []

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 300.0) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

        deadline = time.time() + timeout
        assert self.proc.stdout is not None
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                raise RuntimeError(
                    "bridge closed stdout; stderr tail:\n" + self.stderr_tail()
                )
            line = line.rstrip("\n")
            if not line.strip():
                continue
            self.raw_stdout_lines.append(line)
            msg = json.loads(line)  # deliberately unguarded: see protocol-integrity test
            if msg.get("id") == rid:
                return msg
        raise TimeoutError(f"no response to {method} within {timeout}s")

    def stderr_tail(self, n: int = 40) -> str:
        try:
            self.proc.stderr.flush()
        except Exception:
            pass
        return ""

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:
            self.proc.kill()


def main() -> int:
    print("=" * 72)
    print("hermes_mcp_bridge — end-to-end MCP test")
    print("=" * 72)
    print(f"python : {sys.executable}")
    print(f"bridge : {BRIDGE}")
    print(f"HERMES_HOME: {os.environ.get('HERMES_HOME', '~/.hermes')}")
    print()

    b = Bridge()
    try:
        # ---- 4.1 handshake ------------------------------------------------
        resp = b.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "bridge-test", "version": "1.0"},
            },
        )
        result = resp.get("result") or {}
        check("initialize returns a result", "result" in resp, str(resp)[:200])
        check(
            "protocolVersion echoed",
            result.get("protocolVersion") == "2025-06-18",
            f"got {result.get('protocolVersion')}",
        )
        check(
            "declares tools capability",
            "tools" in (result.get("capabilities") or {}),
            str(result.get("capabilities")),
        )
        check(
            "serverInfo present",
            (result.get("serverInfo") or {}).get("name") == "hermes",
            str(result.get("serverInfo")),
        )
        instructions = result.get("instructions") or ""
        check("instructions non-empty", len(instructions) > 50, f"{len(instructions)} chars")

        b.notify("notifications/initialized")

        # unknown protocol version must still negotiate
        b2 = Bridge()
        r2 = b2.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
        check(
            "unknown protocolVersion falls back to a supported one",
            (r2.get("result") or {}).get("protocolVersion") in
            ("2025-06-18", "2025-03-26", "2024-11-05"),
            str((r2.get("result") or {}).get("protocolVersion")),
        )
        b2.close()

        # ---- 4.1 tools/list -----------------------------------------------
        resp = b.request("tools/list")
        tools = (resp.get("result") or {}).get("tools") or []
        check("tools/list returns tools", len(tools) > 0, f"{len(tools)} tools")

        names = {t["name"] for t in tools}
        for expected in ("semble_search", "lsp_diagnostics", "graphify_query",
                         "effect_run", "orchestra_status", "vault_search"):
            check(f"exposes {expected}", expected in names)

        bad_schema = [
            t["name"] for t in tools
            if not isinstance(t.get("inputSchema"), dict)
            or t["inputSchema"].get("type") != "object"
        ]
        check("every tool has an object inputSchema", not bad_schema, str(bad_schema[:5]))

        missing_desc = [t["name"] for t in tools if not (t.get("description") or "").strip()]
        check("every tool has a description", not missing_desc, str(missing_desc[:5]))

        # Claude Code namespaces MCP tools as mcp__<server>__<tool>; names must
        # be plain identifiers for that to round-trip.
        weird = [n for n in names if not n.replace("_", "").isalnum()]
        check("tool names are identifier-safe", not weird, str(weird[:5]))

        # ---- 4.2 live round-trip ------------------------------------------
        resp = b.request("tools/call", {"name": "semble_status", "arguments": {}})
        r = resp.get("result") or {}
        content = r.get("content") or []
        check("tools/call returns content blocks", len(content) > 0, str(r)[:200])
        check(
            "content block is well-formed",
            bool(content) and content[0].get("type") == "text"
            and isinstance(content[0].get("text"), str),
            str(content[:1])[:200],
        )
        if content:
            print(f"        semble_status -> {content[0].get('text', '')[:160]}")

        resp = b.request("tools/call", {"name": "lsp_servers", "arguments": {}})
        content = (resp.get("result") or {}).get("content") or []
        check("second live tool call succeeds", len(content) > 0)
        if content:
            print(f"        lsp_servers   -> {content[0].get('text', '')[:160]}")

        # ---- 4.3 error paths ----------------------------------------------
        resp = b.request("tools/call", {"name": "no_such_tool_xyz", "arguments": {}})
        r = resp.get("result") or {}
        check(
            "unknown tool -> isError, not a crash",
            r.get("isError") is True,
            str(r)[:200],
        )

        resp = b.request("tools/call", {"name": "semble_search", "arguments": {}})
        r = resp.get("result") or {}
        check(
            "missing required arg -> handled result, not a crash",
            "content" in r,
            str(r)[:200],
        )

        resp = b.request("tools/call", {})
        r = resp.get("result") or {}
        check("tools/call with no name -> isError", r.get("isError") is True, str(r)[:200])

        resp = b.request("nonexistent/method")
        check(
            "unknown method -> JSON-RPC error -32601",
            (resp.get("error") or {}).get("code") == -32601,
            str(resp)[:200],
        )

        resp = b.request("ping")
        check("ping answered", "result" in resp, str(resp)[:120])

        for probe in ("resources/list", "prompts/list"):
            resp = b.request(probe)
            check(f"{probe} answered (not METHOD_NOT_FOUND)", "result" in resp, str(resp)[:120])

        # malformed frame must not kill the server
        assert b.proc.stdin is not None
        b.proc.stdin.write("this is not json\n")
        b.proc.stdin.flush()
        resp = b.request("ping")
        check("survives a malformed frame", "result" in resp, str(resp)[:120])

        # ---- 4.4 protocol integrity ---------------------------------------
        non_json = []
        for line in b.raw_stdout_lines:
            try:
                json.loads(line)
            except ValueError:
                non_json.append(line[:120])
        check(
            "every stdout line is valid JSON (stdout quarantine holds)",
            not non_json,
            f"{len(non_json)} bad line(s): {non_json[:3]}",
        )
        check(
            "all frames carry jsonrpc 2.0",
            all(json.loads(l).get("jsonrpc") == "2.0" for l in b.raw_stdout_lines),
        )
        print(f"        verified {len(b.raw_stdout_lines)} stdout frames")

        # ---- scoping ------------------------------------------------------
        b3 = Bridge({"HERMES_MCP_TOOLSETS": "semble,lsp"})
        b3.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        t3 = (b3.request("tools/list").get("result") or {}).get("tools") or []
        n3 = {t["name"] for t in t3}
        check(
            "HERMES_MCP_TOOLSETS scopes the tool set",
            n3 and all(n.startswith(("semble_", "lsp_")) for n in n3),
            f"{len(n3)} tools: {sorted(n3)[:6]}",
        )
        b3.close()

        b4 = Bridge({"HERMES_MCP_EXCLUDE_TOOLS": "semble_search,lsp_hover"})
        b4.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        t4 = (b4.request("tools/list").get("result") or {}).get("tools") or []
        n4 = {t["name"] for t in t4}
        check(
            "HERMES_MCP_EXCLUDE_TOOLS removes named tools",
            "semble_search" not in n4 and "lsp_hover" not in n4 and len(n4) > 10,
            f"{len(n4)} tools remain",
        )
        b4.close()

        # ---- failure mode --------------------------------------------------
        b5 = Bridge({"HERMES_HOME": "/nonexistent/hermes/home/xyz"})
        r5 = b5.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        check(
            "missing HERMES_HOME still speaks MCP (diagnosable, not a silent exit)",
            "result" in r5 and "could not start" in
            ((r5.get("result") or {}).get("instructions") or "").lower(),
            str(r5)[:200],
        )
        t5 = (b5.request("tools/list").get("result") or {}).get("tools")
        check("broken bridge reports zero tools rather than crashing", t5 == [], str(t5)[:120])
        r5c = b5.request("tools/call", {"name": "semble_search", "arguments": {"query": "x"}})
        check(
            "broken bridge returns isError on call",
            ((r5c.get("result") or {}).get("isError")) is True,
            str(r5c)[:160],
        )
        b5.close()

    finally:
        b.close()

    print()
    print("=" * 72)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failed = [n for n, ok, _ in _results if not ok]
    print(f"{passed}/{total} checks passed")
    if failed:
        print("\nFAILED:")
        for n in failed:
            print(f"  - {n}")
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
