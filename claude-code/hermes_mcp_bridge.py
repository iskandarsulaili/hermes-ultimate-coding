#!/usr/bin/env python3
"""
hermes_mcp_bridge — expose the real Hermes plugin registry to Claude Code over MCP.

This is a *bridge*, not a reimplementation.  It boots the genuine
``hermes_cli.plugins.PluginManager`` against the user's real ``$HERMES_HOME``,
lets every plugin register through the real ``PluginContext``, and then serves
the resulting ``tools.registry`` over the Model Context Protocol on stdio.

Consequences of that design, all deliberate:

* There is exactly **one** implementation of every tool.  A fix in a plugin is
  live in Claude Code with no porting step.
* Plugin availability gates (``check_fn``), input schemas, async handlers,
  result contracts and error formats are the plugins' own — the bridge never
  reinterprets them.
* New plugins appear automatically.  Nothing here enumerates tools by name.

Transport
---------
MCP stdio: newline-delimited JSON-RPC 2.0 on stdin/stdout.

**stdout is the wire.**  Hermes plugins log, and some third-party libraries
they import print directly to file descriptor 1.  A single stray byte on
stdout desynchronizes the protocol and Claude Code drops the server.  So at
start-up the real stdout is duplicated to a private descriptor and fd 1 is
pointed at stderr.  Everything the process or any of its children writes to
"stdout" lands harmlessly in the Claude Code MCP log; only this module's
framer writes to the saved descriptor.

Configuration (environment)
---------------------------
``HERMES_HOME``               Hermes home.  Default ``~/.hermes``.
``HERMES_AGENT_DIR``          hermes-agent checkout.  Default ``$HERMES_HOME/hermes-agent``.
``HERMES_MCP_TOOLSETS``       Comma-separated allowlist of toolsets.  Default: all.
``HERMES_MCP_EXCLUDE_TOOLS``  Comma-separated denylist of tool names.
``HERMES_MCP_INCLUDE_BUILTINS``  ``1`` to also expose Hermes' own built-in tools.
                              Off by default: they duplicate Claude Code's
                              native Read/Write/Bash and add nothing.
``HERMES_MCP_DEBUG``          ``1`` for verbose stderr tracing.
``HERMES_MCP_CALL_TIMEOUT``   Per-call wall-clock budget in seconds (default 300).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from typing import Any, Dict, List, Optional, Tuple

SERVER_NAME = "hermes"
SERVER_VERSION = "1.0.0"

# MCP revisions this framer implements.  If the client asks for one of these we
# echo it back verbatim; otherwise we answer with our newest and let the client
# decide whether it can proceed.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# JSON-RPC 2.0 reserved codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# stdout quarantine — must run before anything imports Hermes
# ---------------------------------------------------------------------------

def _quarantine_stdout():
    """Move the real stdout out of reach and point fd 1 at stderr.

    Returns a text stream bound to the *original* stdout, which is the only
    thing permitted to carry protocol frames.

    The redirection is done at the file-descriptor level rather than by
    rebinding ``sys.stdout`` so that C extensions and subprocesses — which
    inherit fd 1 and never consult ``sys.stdout`` — are covered too.
    """
    real_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr  # anything using sys.stdout is caught as well
    return os.fdopen(real_fd, "w", encoding="utf-8", buffering=1, newline="\n")


_WIRE = _quarantine_stdout()
_WIRE_LOCK = threading.Lock()


def _isolate_process_group() -> bool:
    """Put the bridge in its own process group so its descendants can be reaped.

    Plugins spawn long-running helpers (``graphify extract``, language servers,
    browsers). Those helpers are supervised by the plugin thread that started
    them — a ``communicate(timeout=...)`` in the parent is what enforces their
    time budget. If the bridge exits while one is running, the child is
    reparented to init and that budget stops being enforced: it runs unbounded,
    burning CPU with nobody left to stop it.

    Owning a process group lets :func:`_reap_process_group` signal exactly our
    own descendants at shutdown, and nothing else. Set
    ``HERMES_MCP_NO_PROCESS_GROUP=1`` to opt out.
    """
    if os.environ.get("HERMES_MCP_NO_PROCESS_GROUP") == "1":
        return False
    if not hasattr(os, "setpgrp"):
        return False  # not POSIX
    try:
        os.setpgrp()
        return True
    except OSError as exc:
        log.debug("could not create process group: %s", exc)
        return False


def _group_members(pgid: int) -> List[int]:
    """PIDs in our process group, excluding this process. Empty on non-Linux."""
    out: List[int] = []
    me = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                fields = fh.read().rsplit(b")", 1)[-1].split()
            # after the comm field: state, ppid, pgrp, ...
            if len(fields) >= 3 and int(fields[2]) == pgid:
                out.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return out


def _reap_process_group(owns_group: bool) -> None:
    """Best-effort termination of processes we started. Never raises.

    Signals the group with SIGTERM (having first made this process immune, so
    ``killpg`` does not take the bridge down before it finishes), then SIGKILLs
    individual survivors. Self is always excluded.
    """
    if not owns_group:
        return
    import signal
    import time as _time

    try:
        pgid = os.getpgrp()
        if pgid != os.getpid():
            return  # not our own group; refuse to signal it
    except OSError:
        return

    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (OSError, ValueError):
        pass

    survivors = _group_members(pgid)
    if not survivors:
        return
    log.info("reaping %d child process(es) at shutdown", len(survivors))

    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass

    _time.sleep(1.5)

    for pid in _group_members(pgid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("HERMES_MCP_DEBUG") == "1" else logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s hermes-mcp %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hermes-mcp-bridge")


# ---------------------------------------------------------------------------
# Hermes bootstrap
# ---------------------------------------------------------------------------

class HermesRuntime:
    """Owns the loaded Hermes plugin registry."""

    def __init__(self) -> None:
        self.registry = None
        self.manager = None
        self.load_errors: List[Tuple[str, str]] = []
        self._exposed: Dict[str, str] = {}  # tool name -> toolset
        self._loaded = False
        self._lock = threading.Lock()

    # -- paths ------------------------------------------------------------

    @staticmethod
    def hermes_home() -> str:
        return os.path.expanduser(
            os.environ.get("HERMES_HOME") or os.path.join("~", ".hermes")
        )

    @classmethod
    def agent_dir(cls) -> str:
        explicit = os.environ.get("HERMES_AGENT_DIR")
        if explicit:
            return os.path.expanduser(explicit)
        return os.path.join(cls.hermes_home(), "hermes-agent")

    # -- loading ----------------------------------------------------------

    def load(self) -> None:
        """Import Hermes and run a real plugin discovery pass. Idempotent."""
        with self._lock:
            if self._loaded:
                return

            home = self.hermes_home()
            agent = self.agent_dir()

            if not os.path.isdir(home):
                raise RuntimeError(
                    f"HERMES_HOME does not exist: {home}\n"
                    "Install Hermes first (see https://github.com/NousResearch/hermes-agent), "
                    "or point HERMES_HOME at an existing install."
                )
            if not os.path.isdir(agent):
                raise RuntimeError(
                    f"hermes-agent not found at: {agent}\n"
                    "Set HERMES_AGENT_DIR to the hermes-agent checkout."
                )

            os.environ.setdefault("HERMES_HOME", home)
            if agent not in sys.path:
                sys.path.insert(0, agent)

            log.info("hermes home=%s agent=%s", home, agent)

            from hermes_cli.plugins import PluginManager  # noqa: E402
            from tools.registry import registry  # noqa: E402

            self.registry = registry
            self.manager = PluginManager()

            try:
                self.manager.discover_and_load()
            except Exception as exc:  # a bad plugin must not kill the bridge
                log.error("plugin discovery raised: %s", exc)
                log.debug("%s", traceback.format_exc())
                self.load_errors.append(("<discovery>", str(exc)))

            for key, loaded in getattr(self.manager, "_plugins", {}).items():
                err = getattr(loaded, "error", None)
                if err and getattr(loaded, "enabled", False) is False:
                    # "disabled via config" is a user choice, not a failure.
                    if "disabled" not in str(err).lower():
                        self.load_errors.append((key, str(err)))

            self._compute_exposed()
            self._loaded = True

            log.info(
                "loaded: %d plugins, %d tools exposed%s",
                len(getattr(self.manager, "_plugins", {})),
                len(self._exposed),
                f", {len(self.load_errors)} load error(s)" if self.load_errors else "",
            )

    # -- tool scoping -----------------------------------------------------

    @staticmethod
    def _csv_env(name: str) -> Optional[set]:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return None
        return {p.strip() for p in raw.split(",") if p.strip()}

    def _compute_exposed(self) -> None:
        """Decide which registry tools this bridge advertises.

        Default: every plugin-provided tool.  Hermes' own built-ins are held
        back because Claude Code already has first-class equivalents (Read,
        Write, Bash, Grep...) and shadowing them adds cost without capability.
        """
        assert self.registry is not None

        plugin_tools = set(getattr(self.manager, "_plugin_tool_names", set()) or set())

        if os.environ.get("HERMES_MCP_INCLUDE_BUILTINS") == "1":
            candidates = set(self.registry.get_all_tool_names())
        else:
            candidates = plugin_tools or set(self.registry.get_all_tool_names())

        toolset_allow = self._csv_env("HERMES_MCP_TOOLSETS")
        tool_deny = self._csv_env("HERMES_MCP_EXCLUDE_TOOLS") or set()

        exposed: Dict[str, str] = {}
        for name in sorted(candidates):
            if name in tool_deny:
                continue
            toolset = self.registry.get_toolset_for_tool(name) or ""
            if toolset_allow is not None and toolset not in toolset_allow:
                continue
            exposed[name] = toolset

        self._exposed = exposed

    # -- MCP surface ------------------------------------------------------

    def list_tools(self) -> List[dict]:
        """Registry schemas translated to MCP tool descriptors.

        ``get_definitions`` applies each tool's ``check_fn``, so a tool whose
        backing dependency is missing is simply absent rather than advertised
        and broken.
        """
        assert self.registry is not None
        defs = self.registry.get_definitions(set(self._exposed), quiet=True)

        tools: List[dict] = []
        for d in defs:
            # Hermes stores OpenAI function-call shape, either bare or wrapped
            # in {"type": "function", "function": {...}}.
            fn = d.get("function") if isinstance(d.get("function"), dict) else d
            name = fn.get("name")
            if not name:
                continue
            schema = fn.get("parameters")
            if not isinstance(schema, dict) or not schema:
                schema = {"type": "object", "properties": {}}
            # MCP requires an object-typed root schema.
            if schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}

            toolset = self._exposed.get(name, "")
            description = (fn.get("description") or "").strip()
            if toolset:
                description = f"[{toolset}] {description}" if description else f"[{toolset}]"

            tools.append(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": schema,
                }
            )
        return tools

    def call_tool(self, name: str, args: dict) -> Tuple[List[dict], bool]:
        """Dispatch through the real registry. Returns (content, is_error)."""
        assert self.registry is not None

        if name not in self._exposed:
            return ([_text(f"Unknown tool: {name}")], True)

        timeout = _float_env("HERMES_MCP_CALL_TIMEOUT", 300.0)

        # registry.dispatch already catches handler exceptions and bridges async
        # handlers; it runs on a worker thread purely so a wedged handler cannot
        # block the protocol loop.
        #
        # That worker is a bare daemon thread rather than a ThreadPoolExecutor
        # on purpose. `with ThreadPoolExecutor(...)` calls shutdown(wait=True)
        # on exit, so returning from inside the block after a timeout blocks
        # until the very handler we just gave up on finally returns — the
        # timeout would bound nothing, and one hung tool would wedge the whole
        # server. A daemon thread can simply be abandoned: the call returns on
        # schedule, and a stuck handler cannot hold up process exit either.
        box: Dict[str, Any] = {}
        done = threading.Event()

        def _invoke() -> None:
            try:
                box["result"] = self.registry.dispatch(name, args)
            except BaseException as exc:  # dispatch is defensive, but never trust it
                box["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(
            target=_invoke, name=f"hermes-tool-{name}", daemon=True
        )
        worker.start()

        if not done.wait(timeout=timeout):
            log.error("tool %s exceeded %.0fs budget; abandoning worker", name, timeout)
            return ([_text(f"Tool {name} timed out after {timeout:.0f}s")], True)

        if "error" in box:
            exc = box["error"]
            log.exception("dispatch of %s failed", name, exc_info=exc)
            return ([_text(f"Tool {name} failed: {type(exc).__name__}: {exc}")], True)

        return _result_to_mcp_content(box.get("result"))


# ---------------------------------------------------------------------------
# result translation
# ---------------------------------------------------------------------------

def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _float_env(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name) or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _looks_like_error(payload: str) -> bool:
    """Hermes signals tool failure as a JSON string carrying an "error" key."""
    s = payload.lstrip()
    if not s.startswith("{"):
        return False
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and "error" in obj


def _result_to_mcp_content(result: Any) -> Tuple[List[dict], bool]:
    """Map a Hermes tool result onto MCP content blocks.

    Hermes guarantees one of two shapes (``registry._normalize_handler_result``):
    a ``str``, or a dict with ``_multimodal: True`` and a ``content`` list.
    """
    if isinstance(result, str):
        return ([_text(result)], _looks_like_error(result))

    if isinstance(result, dict) and result.get("_multimodal") is True:
        blocks: List[dict] = []
        for item in result.get("content") or []:
            blocks.append(_translate_block(item))
        if not blocks:
            blocks = [_text("(empty result)")]
        return (blocks, False)

    # The registry contract says this is unreachable; handle it rather than
    # trust the invariant, since a contract violation upstream should surface
    # as a readable tool error instead of a bridge crash.
    return ([_text(json.dumps(result, ensure_ascii=False, default=str))], False)


def _translate_block(item: Any) -> dict:
    """Translate one Anthropic-style content block to an MCP content block."""
    if not isinstance(item, dict):
        return _text(str(item))

    kind = item.get("type")

    if kind == "text":
        return _text(str(item.get("text", "")))

    if kind == "image":
        source = item.get("source") or {}
        # Anthropic base64 image source
        if source.get("type") == "base64" and source.get("data"):
            return {
                "type": "image",
                "data": source["data"],
                "mimeType": source.get("media_type") or "image/png",
            }
        # Already-MCP-shaped image
        if item.get("data"):
            return {
                "type": "image",
                "data": item["data"],
                "mimeType": item.get("mimeType") or "image/png",
            }
        if source.get("type") == "url" and source.get("url"):
            return _text(f"[image] {source['url']}")

    if kind == "resource" and isinstance(item.get("resource"), dict):
        return item  # already an MCP embedded resource

    # Unknown block: preserve it losslessly as JSON rather than dropping it.
    try:
        return _text(json.dumps(item, ensure_ascii=False, default=str))
    except Exception:
        return _text(str(item))


# ---------------------------------------------------------------------------
# JSON-RPC framing
# ---------------------------------------------------------------------------

def _send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _WIRE_LOCK:
        _WIRE.write(line + "\n")
        _WIRE.flush()


def _reply(req_id: Any, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": req_id, "error": err})


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

class BridgeServer:
    def __init__(self) -> None:
        self.runtime = HermesRuntime()
        self.fatal: Optional[str] = None
        self._shutdown = False

    def preload(self) -> None:
        """Load Hermes eagerly so `initialize` reports honest capability.

        A failure here is recorded rather than raised: the server still speaks
        MCP so Claude Code can surface a real diagnostic instead of an opaque
        "server exited" message.
        """
        try:
            self.runtime.load()
        except Exception as exc:
            self.fatal = str(exc)
            log.error("hermes bootstrap failed: %s", exc)
            log.debug("%s", traceback.format_exc())

    # -- dispatch ---------------------------------------------------------

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if not method:
            if not is_notification:
                _error(req_id, INVALID_REQUEST, "missing method")
            return

        try:
            if method == "initialize":
                _reply(req_id, self._initialize(params))
            elif method in ("notifications/initialized", "initialized"):
                return  # notification: no reply
            elif method == "ping":
                _reply(req_id, {})
            elif method == "tools/list":
                _reply(req_id, {"tools": self._tools_list()})
            elif method == "tools/call":
                _reply(req_id, self._tools_call(params))
            elif method in ("resources/list", "prompts/list"):
                # Declared unsupported in capabilities, but some clients probe
                # anyway; an empty list is friendlier than METHOD_NOT_FOUND.
                key = "resources" if method.startswith("resources") else "prompts"
                _reply(req_id, {key: []})
            elif method == "shutdown":
                self._shutdown = True
                _reply(req_id, {})
            elif is_notification:
                log.debug("ignoring notification %s", method)
            else:
                _error(req_id, METHOD_NOT_FOUND, f"unknown method: {method}")
        except Exception as exc:
            log.exception("error handling %s", method)
            if not is_notification:
                _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    # -- methods ----------------------------------------------------------

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else PREFERRED_PROTOCOL_VERSION
        )

        instructions = self._instructions()

        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": instructions,
        }

    def _instructions(self) -> str:
        if self.fatal:
            return (
                "The Hermes bridge could not start, so no Hermes tools are available. "
                f"Reason: {self.fatal}"
            )
        rt = self.runtime
        by_toolset: Dict[str, int] = {}
        for toolset in rt._exposed.values():
            by_toolset[toolset] = by_toolset.get(toolset, 0) + 1
        summary = ", ".join(f"{k} ({v})" for k, v in sorted(by_toolset.items()))
        lines = [
            f"Hermes plugin tools, bridged live from {rt.hermes_home()}.",
            f"{len(rt._exposed)} tools across {len(by_toolset)} toolsets: {summary}.",
            "",
            "These complement Claude Code's built-ins rather than replacing them: "
            "use semble_* for semantic/concept code search and graphify_*/codegraph_* "
            "for structural relationships, then Claude Code's own Read/Grep for exact "
            "text and full file contents.",
        ]
        if rt.load_errors:
            lines.append("")
            lines.append("Plugins that failed to load:")
            for key, err in rt.load_errors:
                lines.append(f"  - {key}: {err}")
        return "\n".join(lines)

    def _tools_list(self) -> List[dict]:
        if self.fatal:
            return []
        try:
            return self.runtime.list_tools()
        except Exception:
            log.exception("tools/list failed")
            return []

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments")
        if not isinstance(args, dict):
            args = {}

        if self.fatal:
            return {
                "content": [_text(f"Hermes bridge unavailable: {self.fatal}")],
                "isError": True,
            }
        if not name or not isinstance(name, str):
            return {"content": [_text("tools/call requires a tool name")], "isError": True}

        content, is_error = self.runtime.call_tool(name, args)
        out: Dict[str, Any] = {"content": content}
        if is_error:
            out["isError"] = True
        return out

    # -- loop -------------------------------------------------------------

    def serve(self) -> int:
        log.info("hermes MCP bridge ready (pid %d)", os.getpid())
        stdin = sys.stdin
        while not self._shutdown:
            try:
                line = stdin.readline()
            except (KeyboardInterrupt, InterruptedError):
                break
            except Exception:
                log.exception("stdin read failed")
                break

            if line == "":
                log.info("stdin closed; exiting")
                break
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except ValueError as exc:
                log.error("malformed JSON frame: %s", exc)
                _error(None, PARSE_ERROR, f"parse error: {exc}")
                continue

            if isinstance(msg, list):
                for item in msg:  # JSON-RPC batch
                    if isinstance(item, dict):
                        self.handle(item)
                continue
            if not isinstance(msg, dict):
                _error(None, INVALID_REQUEST, "frame must be an object or array")
                continue

            self.handle(msg)
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    owns_group = _isolate_process_group()
    server = BridgeServer()

    try:
        if "--selftest" in argv:
            return _selftest(server)
        server.preload()
        return server.serve()
    finally:
        # Plugins spawn helpers whose time budget is enforced by the parent that
        # started them; leaving them orphaned means that budget is never applied.
        _reap_process_group(owns_group)


def _selftest(server: BridgeServer) -> int:
    """Load Hermes and print a human-readable inventory. Never speaks MCP."""
    server.preload()
    out = sys.stderr
    if server.fatal:
        print(f"FAIL: {server.fatal}", file=out)
        return 1

    rt = server.runtime
    tools = rt.list_tools()
    by: Dict[str, List[str]] = {}
    for t in tools:
        ts = rt._exposed.get(t["name"], "?")
        by.setdefault(ts, []).append(t["name"])

    print(f"hermes home : {rt.hermes_home()}", file=out)
    print(f"agent dir   : {rt.agent_dir()}", file=out)
    print(f"plugins     : {len(getattr(rt.manager, '_plugins', {}))}", file=out)
    print(f"tools listed: {len(tools)}", file=out)
    print("", file=out)
    for ts in sorted(by):
        print(f"  {ts:<22} {len(by[ts]):>3}  {', '.join(sorted(by[ts]))}", file=out)
    if rt.load_errors:
        print("", file=out)
        print("load errors:", file=out)
        for key, err in rt.load_errors:
            print(f"  {key}: {err}", file=out)

    # Fail only on problems this bridge is responsible for: no tools at all, or
    # a hermes-* plugin that did not load.  Unrelated third-party plugins in the
    # user's Hermes install are reported above but do not fail the selftest.
    ours = [k for k, _ in rt.load_errors if str(k).startswith("hermes-")]
    if not tools:
        print("\nFAIL: no tools exposed", file=out)
        return 1
    if ours:
        print(f"\nFAIL: {len(ours)} hermes plugin(s) failed to load", file=out)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
