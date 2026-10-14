"""
hermes-rea — reverse engineering for Hermes, wrapping REA (Reverse Engineer Anything).

WHAT REA IS
  rea-agents (github.com/morluto/rea, MIT) is one Node/TypeScript codebase that ships:
    * a CLI  (127 commands in the published release, 153 in current main)
    * an MCP stdio server (`rea mcp`) advertising ~170 tools on the release, 207 on main
    * engine bridges (Ghidra Java bundle, Hopper, IDA, Rizin, GDB, pwndbg, pwntools,
      mitmproxy, apktool/JADX, Frida, LLDB, Cutter) under bridge/
    * a bundled agent skill (.agents/skills/reverse-engineer-anything)
  Analysis runs locally. Every result is an Evidence envelope: evidence_id, provider,
  authority, confidence, limitations, coverage, residual unknowns — plus comparison and
  export tools, and a session-level unknowns registry.

ARCHITECTURE OF THIS PLUGIN
  REA ships first-class Hermes support (its client registry writes ~/.hermes/config.yaml
  and ~/.hermes/skills, profile-aware). This plugin does NOT reimplement REA; it:
    1. resolves the best available REA (built main tree -> global CLI -> npx) and keeps a
       cached command manifest parsed from REA's own `--llms` / `--llms-full` output, so
       argument shapes are never guessed and never drift;
    2. exposes a bounded, task-shaped tool surface (device/APK/artifact/JS/process/native/
       evidence/composite) plus a validated generic executor for the remaining commands;
    3. manages the caller-supplied analysis engines (Ghidra, JADX headless MCP jar, apktool,
       tshark, binwalk/unblob, Rizin, Frida, pwntools, mitmproxy, wakaru, gdb, objdump,
       adb) in ONE env file, and reports per-engine readiness with exact remediation;
    4. persists every Evidence envelope under the Hermes state dir with retention, so
       findings outlive the session and can be compared/exported later;
    5. can register the REA MCP server for Hermes (wrapper script + `hermes mcp add`) and
       install the upstream skill, so deep sessions can use the full MCP surface.

ENGINE RESOLUTION (all caller-supplied, all optional — tools degrade with remediation)
  adb, objdump          -> no setup
  ghidra                -> GHIDRA_INSTALL_DIR
  jadx                  -> REA_JADX_MCP_JAR (headless MCP jar) + JDK 17+
  apktool               -> PATH or REA_APKTOOL_COMMAND
  rizin                 -> PATH (offline native provider)
  tshark/capinfos       -> PATH (pcap + packet captures)
  binwalk / unblob      -> PATH (firmware)
  sigrok-cli            -> PATH (logic recordings)
  frida                 -> PATH (live instrumentation)
  wabt (wasm2wat)       -> PATH (WASM)
  pwntools, mitmproxy   -> PATH (binary diagnostics, native capture)
  gdb / pwndbg          -> PATH (crash debugger context)
  wakaru                -> PATH (unminify/JS recovery)
  dotnet + ilspycmd     -> PATH (managed decompilation)
  Hopper / IDA / JEB    -> paid or closed; never installed by this pack

CONFIGURATION (env, no hardcoded tunables)
  HERMES_REA_CMD          explicit "cmd arg arg" override (highest priority)
  HERMES_REA_ROOT         path to a built REA tree (expects scripts/rea.mjs + dist/)
  HERMES_REA_PIN          npm version for the npx fallback (default: latest)
  HERMES_REA_TIMEOUT      default per-call timeout seconds (default 300)
  HERMES_REA_LONG_TIMEOUT timeout for heavy analyses (default 1800)
  HERMES_REA_MAX_BYTES    response cap before spill-to-disk (default 200000)
  HERMES_REA_STATE_DIR    state root (default <hermes home>/state/rea)
  HERMES_REA_ENV_FILE     engine env file (default <state>/engine_env.json)
  HERMES_REA_MANIFEST_TTL manifest cache TTL seconds (default 86400)
  HERMES_REA_EVIDENCE_RETENTION_DAYS  default 30
  HERMES_REA_GHIDRA_TIMEOUT_MS  Ghidra first-query allowance passed to REA
                                (default 900000; REA's own default is 330000)
  HERMES_REA_AUTO_INSTALL set 0 to disable JIT install of rea-agents
  HERMES_REA_ENGINE_INSTALL_SCRIPT  optional path to an engine bootstrap script

TOOLS
  rea_status rea_engines rea_commands rea_run rea_doctor rea_providers rea_capabilities
  rea_adb_devices rea_adb_inspect rea_adb_logcat rea_adb_screen rea_adb_bugreport
  rea_adb_app rea_adb_transfer rea_apk rea_js_analyze rea_js_compare rea_artifact
  rea_compare rea_crash rea_process_capture rea_native rea_evidence rea_firmware rea_web
  rea_sqlite rea_binary_meta rea_mobile_audit rea_verify_deploy rea_mcp_setup rea_mcp_status

COMMANDS
  /rea [status|engines|commands <q>|evidence|mcp]   /rea-doctor   /rea-engines
  /rea-setup   /rea-commands [query]

HONEST LIMITS
  * Packaged (Themida) targets are not decompilable by any of these engines — expect
    layout/strings/artifact evidence only.
  * Android *method decompilation* needs the caller-supplied JADX headless MCP jar; the
    plain jadx CLI is NOT that provider.
  * Commands added after the installed release require either a newer release or a built
    main tree (HERMES_REA_ROOT); the plugin reports this instead of pretending.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── _shared helpers (JIT deps) ────────────────────────────────────────────────
_shared_dir = str(Path(__file__).resolve().parent.parent)
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)

try:  # pragma: no cover - availability depends on the host
    from _shared.deps import DepSpec, ensure_deps
except Exception:  # pragma: no cover
    DepSpec = None  # type: ignore[assignment]
    ensure_deps = None  # type: ignore[assignment]


# ── small stdlib helpers ──────────────────────────────────────────────────────

def _to_int(raw: Any, default: int) -> int:
    """Coerce to int, never raising on model-supplied garbage."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _to_bool(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_str(key: str, default: str = "") -> str:
    try:
        return str(os.environ.get(key, default) or default)
    except Exception:
        return default


def _env_int(key: str, default: int) -> int:
    return _to_int(os.environ.get(key, str(default)), default)


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp + os.replace so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


# ── configuration ─────────────────────────────────────────────────────────────
def _hermes_home() -> Path:
    home = _env_str("HERMES_HOME")
    if home:
        return Path(home).expanduser()
    return Path.home() / ".hermes"


REA_CMD_OVERRIDE = _env_str("HERMES_REA_CMD")
REA_ROOT = _env_str("HERMES_REA_ROOT", str(Path.home() / "tools" / "rea-main"))
REA_PIN = _env_str("HERMES_REA_PIN", "latest")
REA_TIMEOUT = max(5, _env_int("HERMES_REA_TIMEOUT", 300))
REA_LONG_TIMEOUT = max(10, _env_int("HERMES_REA_LONG_TIMEOUT", 1800))
REA_MAX_BYTES = max(4000, _env_int("HERMES_REA_MAX_BYTES", 200_000))
REA_STATE_DIR = Path(_env_str("HERMES_REA_STATE_DIR", str(_hermes_home() / "state" / "rea")))
REA_ENV_FILE = Path(_env_str("HERMES_REA_ENV_FILE", str(REA_STATE_DIR / "engine_env.json")))
REA_MANIFEST_TTL = max(60, _env_int("HERMES_REA_MANIFEST_TTL", 86_400))
REA_EVIDENCE_RETENTION_DAYS = max(1, _env_int("HERMES_REA_EVIDENCE_RETENTION_DAYS", 30))
REA_AUTO_INSTALL = _env_str("HERMES_REA_AUTO_INSTALL", "1") not in {"0", "false", "no", "off"}
REA_ENGINE_INSTALL_SCRIPT = _env_str("HERMES_REA_ENGINE_INSTALL_SCRIPT")

REA_MANIFEST_FILE = REA_STATE_DIR / "command_manifest.json"
REA_EVIDENCE_DIR = REA_STATE_DIR / "evidence"
REA_SPILL_DIR = REA_STATE_DIR / "spill"
REA_MCP_WRAPPER = REA_STATE_DIR / "rea-mcp.sh"

REA_NODE_MIN_MAJOR = 22

# Engine env defaults discovered on this host; only used when the key is absent.
ENGINE_ENV_DEFAULTS: Dict[str, str] = {
    "GHIDRA_INSTALL_DIR": str(Path.home() / "tools" / "ghidra_12.1.4_PUBLIC"),
    "JAVA_HOME": str(Path.home() / "tools" / "jdk21"),
    "REA_JADX_MCP_JAR": str(Path.home() / "tools" / "rea-providers" / "jadx-headless-mcp-0.7.1-all.jar"),
    # ilspycmd's apphost cannot locate a runtime inside a user-local SDK, and the
    # current tool package targets net6.0; roll forward to whatever SDK is installed.
    "DOTNET_ROOT": str(Path.home() / "tools" / "dotnet"),
    "DOTNET_ROLL_FORWARD": "LatestMajor",
    # Ghidra allows 330s for startup+first query by default; a full REA bridge analysis of
    # a non-trivial binary exceeds that on this host, so give it a real budget.
    "REA_GHIDRA_STARTUP_TIMEOUT_MS": _env_str("HERMES_REA_GHIDRA_TIMEOUT_MS", "900000"),
    "PATH_EXTRA": ":".join([
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / "tools" / "jadx-1.5.6" / "bin"),
        str(Path.home() / "tools" / "dotnet"),
        str(Path.home() / ".dotnet" / "tools"),
    ]),
}

# (id, label, kind, probe-argv, remediation)
ENGINE_SPECS: List[Tuple[str, str, str, List[str], str]] = [
    ("adb", "Android Debug Bridge", "binary", ["adb", "version"], "install Android platform-tools and put adb on PATH"),
    ("objdump", "GNU objdump", "binary", ["objdump", "--version"], "apt install binutils"),
    ("ghidra", "Ghidra (native decompiler)", "env_dir", [], "download Ghidra 12.1.x and set GHIDRA_INSTALL_DIR"),
    ("rizin", "Rizin (offline native provider)", "binary", ["rizin", "-v"], "install Rizin and put rizin/rz-bin on PATH"),
    ("jadx_headless_mcp", "JADX headless MCP jar (APK decompile)", "env_file", [], "download jadx-headless-mcp 0.7.1 all.jar and set REA_JADX_MCP_JAR"),
    ("java", "Java runtime (17+; Ghidra/JADX need it)", "binary", ["java", "-version"], "install a JDK 17+ and set JAVA_HOME"),
    ("apktool", "apktool (APK resources)", "binary", ["apktool", "--version"], "apt install apktool or set REA_APKTOOL_COMMAND"),
    ("tshark", "TShark 4.x (pcap/packet captures)", "binary", ["tshark", "--version"], "apt install tshark"),
    ("capinfos", "capinfos (capture summaries)", "binary", ["capinfos", "-h"], "apt install tshark (ships capinfos)"),
    ("binwalk", "binwalk (firmware regions)", "binary", ["binwalk", "--help"], "apt install binwalk"),
    ("unblob", "unblob (firmware extraction)", "binary", ["unblob", "--version"], "pipx install unblob"),
    ("sigrok", "sigrok-cli (logic recordings)", "binary", ["sigrok-cli", "--version"], "apt install sigrok-cli"),
    ("frida", "Frida (live instrumentation)", "binary", ["frida", "--version"], "pipx install frida-tools"),
    ("wabt", "WABT (WASM artifacts)", "binary", ["wasm2wat", "--version"], "apt install wabt"),
    ("pwntools", "pwntools (ELF layout, crash notes)", "binary", ["pwn", "--help"], "pipx install pwntools (exposes the `pwn` CLI)"),
    ("mitmdump", "mitmproxy (native capture)", "binary", ["mitmdump", "--version"], "pipx install mitmproxy"),
    ("gdb", "GDB (crash debugger context)", "binary", ["gdb", "--version"], "apt install gdb"),
    ("wakaru", "Wakaru (JS source recovery)", "binary", ["wakaru", "--version"], "npm install -g wakaru"),
    ("dotnet", ".NET SDK (managed decompile)", "binary", ["dotnet", "--version"], "install the .NET SDK"),
    ("ilspycmd", "ilspycmd (IL decompiler)", "binary", ["ilspycmd", "--version"], "dotnet tool install -g ilspycmd"),
    ("node", "Node.js (REA runtime)", "node", [], "install Node 22.19+/24.11+/26+"),
]

_LOCK = threading.RLock()
_CACHED_MANIFEST: Optional[Dict[str, Any]] = None
_CACHED_ARGV: Optional[List[str]] = None


# ── engine environment (one file, used by the CLI, the MCP wrapper and Hermes) ──

def _load_engine_env(create: bool = True) -> Dict[str, str]:
    """Return the engine env dict, creating it from discovered defaults on first use.

    An existing file is MERGED with any default key it lacks (a new engine added by a
    later plugin version must not stay invisible), but an operator-set value is never
    overwritten.
    """
    env: Dict[str, str] = {}
    try:
        if REA_ENV_FILE.is_file():
            loaded = json.loads(REA_ENV_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                env = {str(k): str(v) for k, v in loaded.items()}
    except Exception as exc:
        logger.warning("rea: engine env unreadable (%s) — using defaults", exc)
    if not create:
        return env

    def _usable(key: str, value: str) -> bool:
        if not value:
            return False
        if key == "PATH_EXTRA":
            return any(Path(p).exists() for p in value.split(":") if p)
        if key.endswith("_DIR") or key.endswith("_ROOT") or key == "REA_JADX_MCP_JAR":
            return Path(value).exists()
        return True

    added: Dict[str, str] = {}
    for key, value in ENGINE_ENV_DEFAULTS.items():
        if env.get(key):
            continue
        if key == "PATH_EXTRA":
            present = [p for p in value.split(":") if p and Path(p).exists()]
            if present:
                added[key] = ":".join(present)
            continue
        if _usable(key, value):
            added[key] = value
    # An explicit HERMES_REA_* override always wins over the persisted default, so a
    # per-run raise of the Ghidra allowance is not silently ignored by the file.
    if env.get("REA_GHIDRA_STARTUP_TIMEOUT_MS") and _env_str("HERMES_REA_GHIDRA_TIMEOUT_MS"):
        env["REA_GHIDRA_STARTUP_TIMEOUT_MS"] = _env_str("HERMES_REA_GHIDRA_TIMEOUT_MS")
    if (not env and added) or (added and REA_ENV_FILE.is_file()):
        env.update(added)
        try:
            _atomic_write(REA_ENV_FILE, json.dumps(env, indent=1, sort_keys=True))
        except Exception as exc:  # pragma: no cover
            logger.warning("rea: cannot write engine env: %s", exc)
    elif not REA_ENV_FILE.is_file():
        env.update({k: v for k, v in ENGINE_ENV_DEFAULTS.items() if _usable(k, v)})
        try:
            _atomic_write(REA_ENV_FILE, json.dumps(env, indent=1, sort_keys=True))
        except Exception as exc:  # pragma: no cover
            logger.warning("rea: cannot write engine env: %s", exc)
    return env


def _engine_subprocess_env() -> Dict[str, str]:
    """Hermes process env + engine vars + PATH additions.

    Precedence is deliberate: an explicit ambient variable (e.g. REA_GHIDRA_STARTUP_TIMEOUT_MS
    set by an operator for one run) WINS over the persisted engine-env file. The file is the
    fallback store, not an override — the opposite order silently ignored per-run overrides.
    """
    env = dict(os.environ)
    cfg = _load_engine_env()
    for key, value in cfg.items():
        if key == "PATH_EXTRA" or not value:
            continue
        if key in os.environ:  # explicit caller value wins
            continue
        env[key] = value
    extra_path = cfg.get("PATH_EXTRA", "")
    if extra_path:
        env["PATH"] = extra_path + ":" + env.get("PATH", "")
    return env


# ── REA resolution ────────────────────────────────────────────────────────────

def _node_exe() -> str:
    node = _which("node")
    if node:
        return node
    fallback = Path.home() / ".hermes" / "tools"
    if fallback.is_dir():
        for child in sorted(fallback.glob("node-*/bin/node"), reverse=True):
            if child.is_file():
                return str(child)
    return "node"


def _node_version_error() -> Optional[str]:
    try:
        done = subprocess.run([_node_exe(), "--version"], capture_output=True, text=True, timeout=15)
        raw = (done.stdout or "").strip().lstrip("v")
        major = _to_int(raw.split(".")[0], 0)
    except Exception as exc:
        return f"Node.js not runnable ({exc}) — install Node 22.19+/24.11+/26+"
    if major == 0:
        return "Node.js version unrecognised — install Node 22.19+/24.11+/26+"
    if major < REA_NODE_MIN_MAJOR and major not in (20,):
        return f"Node {major} is below REA's minimum (22.19/24.11/26+)"
    return None


def _candidate_argvs() -> List[Tuple[str, List[str]]]:
    out: List[Tuple[str, List[str]]] = []
    if REA_CMD_OVERRIDE:
        out.append(("env HERMES_REA_CMD", shlex.split(REA_CMD_OVERRIDE)))
    root = Path(REA_ROOT).expanduser()
    entry = root / "scripts" / "rea.mjs"
    if entry.is_file():
        out.append(("built main tree", [_node_exe(), str(entry)]))
    cli = _which("rea")
    if cli:
        out.append(("global rea CLI", [cli]))
    npm = _which("npm") or _which("npx")
    if npm:
        npx = _which("npx") or str(Path(npm).with_name("npx"))
        out.append((f"npx rea-agents@{REA_PIN}", [npx, "-y", f"rea-agents@{REA_PIN}"]))
    return out


def _rea_argv() -> List[str]:
    """Resolve the REA launcher once per process (first candidate that answers --version)."""
    global _CACHED_ARGV
    with _LOCK:
        if _CACHED_ARGV is not None:
            return _CACHED_ARGV
        for label, argv in _candidate_argvs():
            try:
                done = subprocess.run(argv + ["--version"], capture_output=True, text=True,
                                      timeout=180, env=_engine_subprocess_env())
                if done.returncode == 0 and (done.stdout or done.stderr).strip():
                    logger.info("rea: using %s", label)
                    _CACHED_ARGV = argv
                    return argv
            except Exception as exc:
                logger.debug("rea: candidate %s failed: %s", label, exc)
        # last resort: global name (errors surface to the caller with remediation)
        _CACHED_ARGV = ["rea"]
        return _CACHED_ARGV


def _launcher_label() -> str:
    argv = _rea_argv()
    if REA_CMD_OVERRIDE and argv[:1] == shlex.split(REA_CMD_OVERRIDE)[:1]:
        return "HERMES_REA_CMD override"
    if len(argv) >= 2 and str(argv[-1]).endswith("rea.mjs"):
        return f"built tree {REA_ROOT}"
    if len(argv) >= 2 and argv[0].endswith("npx"):
        return f"npx rea-agents@{REA_PIN}"
    return " ".join(argv)


# ── process execution ─────────────────────────────────────────────────────────

def _run(argv: List[str], timeout: int, stdin_text: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=_engine_subprocess_env(), start_new_session=True,
                                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL)
    except FileNotFoundError as exc:
        return 127, "", f"launcher not found: {exc}"
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
        return proc.returncode or 0, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            proc.kill()
        try:
            out, err = proc.communicate(timeout=20)
        except Exception:
            out, err = "", ""
        return 124, out or "", (err or "") + f"\n[timeout after {timeout}s]"


def _spill(text: str) -> Tuple[str, Optional[str]]:
    """Cap response size; return (text, spill_path)."""
    if len(text.encode("utf-8", "replace")) <= REA_MAX_BYTES:
        return text, None
    REA_SPILL_DIR.mkdir(parents=True, exist_ok=True)
    path = REA_SPILL_DIR / f"rea-{int(time.time()*1000)}.txt"
    try:
        _atomic_write(path, text)
    except Exception as exc:
        logger.warning("rea: spill write failed: %s", exc)
        return text[: REA_MAX_BYTES // 2], None
    head = text[: REA_MAX_BYTES // 2]
    return head + f"\n\n[... truncated, full output at {path} ...]", str(path)


def _parse_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start: end + 1])
        except Exception:
            return None
    return None


def _exec(cmd: str, pos: Optional[List[Any]] = None, opts: Optional[List[Any]] = None,
          timeout: Optional[int] = None, json_out: bool = True,
          inline_json: Optional[str] = None) -> Dict[str, Any]:
    """Run one REA command. Returns a normalised dict — never raises."""
    pos = pos or []
    opts = opts or []
    argv = list(_rea_argv()) + [cmd]
    if inline_json is not None:
        argv.append(inline_json)
    else:
        argv.extend(str(p) for p in pos if p not in (None, ""))
    for flag, value in zip(opts[0::2], opts[1::2]):
        if value is None or value is False:
            continue
        argv.append(str(flag))
        if value is not True:
            argv.append(str(value))
    if json_out:
        argv += ["--format", "json"]
    code, out, err = _run(argv, timeout or REA_TIMEOUT)
    payload = _parse_json(out) if json_out else None
    result: Dict[str, Any] = {
        "ok": code == 0,
        "command": cmd,
        "argv": argv[1:],
        "exit_code": code,
        "stderr": err.strip()[:2000] if err.strip() else "",
    }
    if payload is not None:
        result["result"] = payload
        if isinstance(payload, dict):
            ev = payload.get("evidence_id")
            if ev:
                result["evidence_id"] = ev
    else:
        text, spill = _spill(out.strip() or err.strip())
        result["output"] = text
        if spill:
            result["spill_path"] = spill
    _persist_evidence(payload, cmd)
    return result


# ── command manifest (parsed from REA's own --llms output) ────────────────────

def _parse_manifest(llms: str, llms_full: str) -> Dict[str, Any]:
    cmds: Dict[str, Any] = {}
    for usage, desc in re.findall(r"^\| `rea ([^`]+)` \| (.+?) \|$", llms or "", re.M):
        parts = usage.split()
        if not parts:
            continue
        cmds[parts[0]] = {"positional": parts[1:], "description": desc.strip()}
    for block in re.split(r"^# rea ", llms_full or "", flags=re.M)[1:]:
        head = block.splitlines()[0].strip() if block.strip() else ""
        name = head.split(" ")[0] if head else ""
        if not name or name not in cmds:
            continue
        args = re.findall(r"^\| `([^`]+)` \| `[^`]+` \| (yes|no) \|", block, re.M)
        cmds[name]["args"] = [{"name": a, "required": r == "yes"} for a, r in args]
        cmds[name]["options"] = re.findall(r"^\| `(--[^`]+)` \|", block, re.M)
    return cmds


def _manifest(force: bool = False) -> Dict[str, Any]:
    """Cached {command: {positional, args, options, description}} from REA itself."""
    global _CACHED_MANIFEST
    with _LOCK:
        if _CACHED_MANIFEST and not force:
            return _CACHED_MANIFEST
        if not force and REA_MANIFEST_FILE.is_file():
            age = time.time() - REA_MANIFEST_FILE.stat().st_mtime
            if age < REA_MANIFEST_TTL:
                try:
                    data = json.loads(REA_MANIFEST_FILE.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("commands"):
                        _CACHED_MANIFEST = data
                        return data
                except Exception:
                    pass
        argv = list(_rea_argv())
        _, llms, _ = _run(argv + ["--llms"], min(REA_TIMEOUT, 300))
        _, full, _ = _run(argv + ["--llms-full"], min(REA_TIMEOUT, 300))
        data = {
            "generated_at": int(time.time()),
            "launcher": _launcher_label(),
            "commands": _parse_manifest(llms, full),
        }
        if data["commands"]:
            try:
                _atomic_write(REA_MANIFEST_FILE, json.dumps(data, indent=1, sort_keys=True))
            except Exception as exc:
                logger.warning("rea: manifest cache write failed: %s", exc)
        _CACHED_MANIFEST = data
        return data


def _command_known(name: str) -> Optional[Dict[str, Any]]:
    return (_manifest().get("commands") or {}).get(name)


# ── evidence persistence ──────────────────────────────────────────────────────

def _persist_evidence(payload: Any, label: str) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    ev = payload.get("evidence_id")
    if not ev:
        return None
    try:
        REA_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(label))[:60]
        path = REA_EVIDENCE_DIR / f"{ev}.{safe}.json"
        if not path.exists():
            _atomic_write(path, json.dumps(payload, indent=1, sort_keys=False))
        _prune_evidence()
        return str(path)
    except Exception as exc:
        logger.warning("rea: evidence persist failed: %s", exc)
        return None


def _prune_evidence() -> int:
    cutoff = time.time() - REA_EVIDENCE_RETENTION_DAYS * 86_400
    removed = 0
    if not REA_EVIDENCE_DIR.is_dir():
        return 0
    for item in REA_EVIDENCE_DIR.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except Exception:
            continue
    return removed


def _list_evidence(limit: int = 40) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not REA_EVIDENCE_DIR.is_dir():
        return rows
    files = sorted(REA_EVIDENCE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[: max(1, min(limit, 500))]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        norm = data.get("normalized_result") or {}
        rows.append({
            "evidence_id": data.get("evidence_id"),
            "operation": data.get("operation"),
            "provider": (data.get("provider") or {}).get("id") if isinstance(data.get("provider"), dict) else None,
            "confidence": data.get("confidence"),
            "limitations": (data.get("limitations") or [])[:2],
            "keys": list(norm.keys())[:8] if isinstance(norm, dict) else [],
            "path": str(path),
            "mtime": int(path.stat().st_mtime),
        })
    return rows


# ── readiness ─────────────────────────────────────────────────────────────────

def _engine_report() -> List[Dict[str, Any]]:
    env = _load_engine_env()
    rows: List[Dict[str, Any]] = []
    for eid, label, kind, probe, remediation in ENGINE_SPECS:
        ok, detail = False, ""
        try:
            if kind == "binary":
                path = _which(probe[0])
                ok = bool(path)
                detail = path or "not on PATH"
            elif kind == "python_module":
                name = {"pwntools": "pwn"}.get(eid, eid)
                code = f"import {name}" if name.isidentifier() else "import importlib; importlib.import_module('pwn')"
                done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
                ok = done.returncode == 0
                detail = "importable" if ok else "not importable by the running interpreter"
            elif kind == "env_dir":
                value = env.get("GHIDRA_INSTALL_DIR", "")
                headless = Path(value) / "support" / "analyzeHeadless" if value else None
                ok = bool(value) and headless is not None and headless.is_file()
                detail = value or "GHIDRA_INSTALL_DIR unset"
            elif kind == "env_file":
                value = env.get("REA_JADX_MCP_JAR", "")
                ok = bool(value) and Path(value).is_file()
                detail = value or "REA_JADX_MCP_JAR unset"
            elif kind == "node":
                err = _node_version_error()
                ok = err is None
                detail = "ok" if ok else err
        except Exception as exc:
            detail = f"probe failed: {exc}"
        rows.append({
            "engine": eid, "label": label, "ready": ok,
            "detail": detail, "remediation": "" if ok else remediation,
        })
    return rows


def _readiness() -> Dict[str, Any]:
    node_err = _node_version_error()
    manifest = _manifest() if not node_err else {"commands": {}}
    engines = _engine_report()
    ready = [r["engine"] for r in engines if r["ready"]]
    missing = [(r["engine"], r["remediation"]) for r in engines if not r["ready"]]
    return {
        "plugin": "hermes-rea",
        "launcher": _launcher_label(),
        "launcher_argv": _rea_argv(),
        "node": "ok" if not node_err else node_err,
        "commands_discovered": len(manifest.get("commands") or {}),
        "manifest_age_s": int(time.time() - (REA_MANIFEST_FILE.stat().st_mtime if REA_MANIFEST_FILE.exists() else 0)),
        "engines_ready": ready,
        "engines_missing": [{"engine": e, "remediation": r} for e, r in missing],
        "evidence_dir": str(REA_EVIDENCE_DIR),
        "evidence_count": len(list(REA_EVIDENCE_DIR.glob('*.json'))) if REA_EVIDENCE_DIR.is_dir() else 0,
        "engine_env_file": str(REA_ENV_FILE),
        "mcp_registration": _mcp_registration_state(),
    }


def _canonical_mcp_env() -> Dict[str, str]:
    """Env Hermes must pass to the REA MCP server (engine vars + a deterministic PATH).

    Hermes gives a stdio MCP server a FILTERED environment, so the engine variables and
    the directories holding the engine binaries must be stated explicitly here.
    """
    cfg = _load_engine_env()
    env: Dict[str, str] = {k: v for k, v in cfg.items() if k != "PATH_EXTRA" and v}
    extra = cfg.get("PATH_EXTRA", "")
    base = "/usr/local/bin:/usr/bin:/bin"
    env["PATH"] = f"{extra}:{base}" if extra else base
    return env


def _tune_mcp_entry(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Ensure the registered rea entry carries the engine env and long timeouts.

    Deliberately TEXT surgery + CLI validation rather than ``hermes_cli.config.save_config``:
    that round-trip (strip_defaults + merge_existing) mis-placed keys added to an entry
    another writer had created, appending them into the file's trailing comment block —
    a silently corrupt config. Here the file is edited deterministically, validated with
    `hermes mcp list` + `hermes config check`, and rolled back if either fails.
    """
    cfg_path = _hermes_home() / "config.yaml"
    result: Dict[str, Any] = {"config": str(cfg_path), "changed": False}
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {**result, "error": f"cannot read config: {exc}"}
    if not re.search(r"^mcp_servers:", text, re.M):
        return {**result, "error": "no mcp_servers section — run rea_mcp_setup with apply=true first"}
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^  rea:", ln)), None)
    if start is None:
        return {**result, "error": "mcp_servers.rea entry not found"}
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith("    ")):
        end += 1
    block = lines[start:end]
    want = env or _canonical_mcp_env()

    def _has(key: str) -> bool:
        return any(re.match(rf"^    {re.escape(key)}\s*:", ln) for ln in block)

    changed = False
    defaults_wanted = {"lazy": "true", "connect_timeout": "120", "timeout": "900"}
    for key, value in defaults_wanted.items():
        if not _has(key):
            block.append(f"    {key}: {value}")
            changed = True
    if not _has("env"):
        block.append("    env:")
        for key, value in want.items():
            block.append(f"      {key}: {value}")
        changed = True
    else:
        env_at = next(i for i, ln in enumerate(block) if re.match(r"^    env:\s*$", ln))
        tail = env_at + 1
        while tail < len(block) and block[tail].startswith("      "):
            tail += 1
        existing = {re.match(r"^      ([^:]+):", ln).group(1).strip(): ln for ln in block[env_at + 1: tail]
                    if re.match(r"^      ([^:]+):", ln)}
        for key, value in want.items():
            line = f"      {key}: {value}"
            if key not in existing:
                block.insert(env_at + 1, line)
                changed = True
            elif existing[key].strip() != line.strip():
                block[block.index(existing[key])] = line
                changed = True
    if not changed:
        return {**result, "verified": True, "env_keys": sorted(want)}
    backup = cfg_path.with_name(cfg_path.name + f".bak-tune-{int(time.time())}")
    backup.write_text(text, encoding="utf-8")
    new_lines = lines[:start] + block + lines[end:]
    cfg_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    hermes = _which("hermes")
    if hermes:
        ok = True
        for args in (["mcp", "list"], ["config", "check"]):
            code, out, err = _run([hermes] + args, 240)
            blob = (out or "") + (err or "")
            if code != 0 or any(tok in blob for tok in ("Traceback", "ScannerError", "duplicate key")):
                ok = False
                result["failed_check"] = " ".join(args)
                break
        if not ok:
            cfg_path.write_text(text, encoding="utf-8")
            return {**result, "rolled_back": str(backup), "error": result.pop("failed_check", "validation failed")}
    result.update({"changed": True, "verified": True, "env_keys": sorted(want), "backup": str(backup)})
    return result


def _mcp_registration_state() -> Dict[str, Any]:
    """Whether ~/.hermes/config.yaml carries an mcp_servers.rea entry (text scan, no YAML dep)."""
    cfg = _hermes_home() / "config.yaml"
    state: Dict[str, Any] = {"config": str(cfg), "registered": False}
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        state["error"] = str(exc)
        return state
    if not re.search(r"^mcp_servers:", text, re.M):
        return state
    block = text.split("mcp_servers:", 1)[1]
    block = re.split(r"^\S", block, flags=re.M)[0] if re.search(r"^\S", block, flags=re.M) else block
    if re.search(r"^[ \t]+rea:", block, re.M):
        state["registered"] = True
        m = re.search(r"^[ \t]+rea:\n((?:[ \t]+.*\n)+)", block, re.M)
        if m:
            body = m.group(1)
            cmd = re.search(r"command:\s*(.+)", body)
            args = re.search(r"args:\s*(.+)", body)
            state["command"] = (cmd.group(1).strip() if cmd else "")
            state["args"] = (args.group(1).strip() if args else "")
    if not REA_MCP_WRAPPER.is_file():
        state["wrapper"] = "missing"
    else:
        state["wrapper"] = str(REA_MCP_WRAPPER)
    return state


# ── helpers used by handlers ──────────────────────────────────────────────────

_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-]{2,64}$")


def _require(args: Dict[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = args.get(name)
        if value in (None, "", []):
            return f"{name} is required"
    return None


def _check_serial(raw: Any) -> Optional[str]:
    serial = str(raw or "").strip()
    if not serial:
        return "serial is required (see rea_adb_devices)"
    if not _SERIAL_RE.match(serial):
        return "serial contains unexpected characters"
    return None


def _check_path(raw: Any, must_exist: bool = True) -> Tuple[Optional[str], Optional[str]]:
    path = str(raw or "").strip()
    if not path:
        return None, "path is required"
    expanded = str(Path(path).expanduser())
    if not os.path.isabs(expanded):
        return None, "path must be absolute"
    if must_exist and not Path(expanded).exists():
        return None, f"path does not exist: {expanded}"
    return expanded, None


def _ok(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=1, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, indent=1, default=str)


def _write_spec(name: str, spec: Any) -> str:
    """Persist an input-JSON spec and return its absolute path (REA accepts a path)."""
    REA_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = REA_STATE_DIR / f"spec-{re.sub(r'[^a-z0-9-]', '-', name.lower())}-{int(time.time()*1000)}.json"
    _atomic_write(path, json.dumps(spec, indent=1))
    return str(path)


def _out_dir(args: Dict[str, Any], kind: str, key: str = "output_directory",
             unique: bool = True) -> Tuple[Optional[str], Optional[str]]:
    """Resolve an output directory for commands that REQUIRE one.

    REA rejects capture-adb-screen / collect-adb-bugreport / pull-adb-file /
    pull-adb-package without `--output-directory` (VALIDATION_ERROR), and a second run
    into the SAME directory fails too: REA refuses to overwrite an existing artifact and
    reports it as "Analysis returned an unreadable result". So a fresh timestamped
    subdirectory is used per call (unless the caller opts out), which also keeps capture
    provenance clean; the resolved path is always returned in the tool result.
    """
    raw = str(args.get(key) or "").strip()
    if raw:
        base = str(Path(raw).expanduser())
        if not os.path.isabs(base):
            return None, f"{key} must be an absolute path"
    else:
        base = str(REA_STATE_DIR / "artifacts" / re.sub(r"[^a-z0-9-]", "-", kind.lower()))
    path = os.path.join(base, time.strftime("%Y%m%dT%H%M%S", time.gmtime())) if unique else base
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return None, f"cannot create {key} {path}: {exc}"
    return path, None


def _spec_arg(args: Dict[str, Any], tool: str) -> Tuple[Optional[str], Optional[str]]:
    """Accept either `spec` (object or inline JSON) or `spec_path`."""
    spec = args.get("spec")
    if isinstance(spec, dict):
        return _write_spec(tool, spec), None
    if isinstance(spec, str) and spec.strip():
        stripped = spec.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except Exception as exc:
                return None, f"spec is not valid JSON: {exc}"
            return _write_spec(tool, parsed), None
        path, err = _check_path(stripped)
        return path, err
    path = args.get("spec_path")
    if path:
        return _check_path(path)
    return None, "spec (JSON object/string) or spec_path is required"


def _require_command(cmd: str) -> Optional[str]:
    """None when the command exists in REA's manifest, else a remediation string."""
    known = _command_known(cmd)
    if known:
        return None
    total = len((_manifest().get("commands") or {}))
    return (f"REA command '{cmd}' is not in the installed build's manifest "
            f"({total} commands via {_launcher_label()}). Build REA main "
            f"(HERMES_REA_ROOT) or update the release; rea_commands lists what is available.")


# ══════════════════════════════════════════════════════════════════════════════
# TOOL HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

def _h_status(args: Dict[str, Any], **_: Any) -> str:
    try:
        refresh = _to_bool(args.get("refresh"))
        if refresh:
            _manifest(force=True)
        return _ok(_readiness())
    except Exception as exc:
        return _err(f"status failed: {exc}")


def _h_engines(args: Dict[str, Any], **_: Any) -> str:
    try:
        rows = _engine_report()
        missing = [r for r in rows if not r["ready"]]
        return _ok({
            "ready_count": len(rows) - len(missing),
            "total": len(rows),
            "engines": rows,
            "install_script": REA_ENGINE_INSTALL_SCRIPT or None,
            "how_to_build_main": ("python3 -c \"...\" not needed — set HERMES_REA_ROOT to a tree "
                                  "with scripts/rea.mjs + dist/ after `npm ci && npm run compile`"),
        })
    except Exception as exc:
        return _err(f"engine report failed: {exc}")


def _h_commands(args: Dict[str, Any], **_: Any) -> str:
    try:
        query = str(args.get("query") or "").strip().lower()
        limit = max(1, min(_to_int(args.get("limit", 40), 40), 200))
        cmds = _manifest().get("commands") or {}
        rows = []
        for name, meta in sorted(cmds.items()):
            blob = (name + " " + (meta.get("description") or "")).lower()
            if query and query not in blob:
                continue
            rows.append({
                "command": name,
                "usage": " ".join(["rea", name] + list(meta.get("positional") or [])),
                "args": meta.get("args") or [],
                "options": (meta.get("options") or [])[:16],
                "description": meta.get("description") or "",
            })
            if len(rows) >= limit:
                break
        return _ok({"total_commands": len(cmds), "matched": len(rows), "commands": rows})
    except Exception as exc:
        return _err(f"manifest read failed: {exc}")


def _h_run(args: Dict[str, Any], **_: Any) -> str:
    """Validated generic executor for any discovered REA command."""
    try:
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return _err("command is required (see rea_commands)")
        missing = _require_command(cmd)
        if missing:
            return _err(missing)
        pos = args.get("args") or args.get("positional") or []
        if isinstance(pos, str):
            pos = shlex.split(pos)
        if not isinstance(pos, list):
            return _err("args must be a list of positional strings or a shell-style string")
        opts: List[Any] = []
        options = args.get("options")
        if isinstance(options, dict):
            for flag, value in options.items():
                opts += [flag if flag.startswith("-") else f"--{flag}", value]
        elif isinstance(options, list):
            for item in options:
                if isinstance(item, dict):
                    for flag, value in item.items():
                        opts += [flag, value]
                else:
                    opts.append(item)
        elif isinstance(options, str):
            toks = shlex.split(options)
            if len(toks) % 2 == 0:
                opts = toks
            else:
                return _err("options string must be an even number of tokens (--flag value)")
        timeout = max(5, min(_to_int(args.get("timeout", REA_TIMEOUT), REA_TIMEOUT), REA_LONG_TIMEOUT))
        return _ok(_exec(cmd, pos, opts, timeout))
    except Exception as exc:
        return _err(f"run failed: {exc}")


def _h_doctor(args: Dict[str, Any], **_: Any) -> str:
    try:
        opts: List[Any] = []
        client = args.get("client")
        if client:
            opts += ["--client", str(client)]
        target = args.get("target")
        if target:
            path, err = _check_path(target)
            if err:
                return _err(err)
            opts += ["--target", path]
        return _ok(_exec("doctor", [], opts, REA_TIMEOUT))
    except Exception as exc:
        return _err(f"doctor failed: {exc}")


def _h_providers(args: Dict[str, Any], **_: Any) -> str:
    try:
        return _ok(_exec("providers"))
    except Exception as exc:
        return _err(f"providers failed: {exc}")


def _h_capabilities(args: Dict[str, Any], **_: Any) -> str:
    try:
        return _ok(_exec("capabilities", timeout=min(REA_TIMEOUT, 600)))
    except Exception as exc:
        return _err(f"capabilities failed: {exc}")


# ── ADB ───────────────────────────────────────────────────────────────────────

def _h_adb_devices(args: Dict[str, Any], **_: Any) -> str:
    return _ok(_exec("list-adb-devices"))


def _h_adb_inspect(args: Dict[str, Any], **_: Any) -> str:
    try:
        kind = str(args.get("kind") or "device").strip().lower()
        err = _check_serial(args.get("serial"))
        if err:
            return _err(err)
        serial = str(args["serial"]).strip()
        table = {
            "device": ("inspect-adb-device", [serial]),
            "display": ("inspect-adb-display", [serial]),
            "window": ("inspect-adb-window", [serial]),
            "client": ("inspect-adb-client", []),
            "packages": ("list-adb-packages", [serial]),
            "processes": ("list-adb-processes", [serial]),
            "services": ("list-adb-services", [serial]),
            "features": ("list-adb-features", [serial]),
            "directory": ("list-adb-directory", [serial, args.get("device_path")]),
            "setting": ("read-adb-setting", [serial, args.get("namespace"), args.get("key")]),
        }
        if kind == "package":
            package = str(args.get("package") or "").strip()
            if not package:
                return _err("package is required for kind=package")
            return _ok(_exec("inspect-adb-package", [serial, package]))
        entry = table.get(kind)
        if not entry:
            return _err(f"unknown kind '{kind}' (device|display|window|client|package|packages|"
                        f"processes|services|features|directory|setting)")
        cmd, pos = entry
        if None in pos:
            return _err(f"kind={kind} needs the matching extra parameter")
        opts: List[Any] = []
        if kind == "packages" and args.get("scope"):
            opts += ["--scope", args["scope"]]
        return _ok(_exec(cmd, pos, opts))
    except Exception as exc:
        return _err(f"adb inspect failed: {exc}")


def _h_adb_logcat(args: Dict[str, Any], **_: Any) -> str:
    try:
        err = _check_serial(args.get("serial"))
        if err:
            return _err(err)
        serial = str(args["serial"]).strip()
        opts: List[Any] = []
        if args.get("count") not in (None, ""):
            opts += ["--count", max(1, min(_to_int(args.get("count"), 200), 20_000))]
        if args.get("buffer"):
            opts += ["--buffer", str(args["buffer"])]
        if args.get("pid") not in (None, ""):
            opts += ["--pid", max(1, _to_int(args["pid"], 0))]
        return _ok(_exec("read-adb-logcat", [serial], opts))
    except Exception as exc:
        return _err(f"logcat failed: {exc}")


def _h_adb_screen(args: Dict[str, Any], **_: Any) -> str:
    try:
        err = _check_serial(args.get("serial"))
        if err:
            return _err(err)
        out_dir, derr = _out_dir(args, "screen")
        if derr:
            return _err(derr)
        return _ok(_exec("capture-adb-screen", [str(args["serial"]).strip()],
                         ["--output-directory", out_dir]))
    except Exception as exc:
        return _err(f"screen capture failed: {exc}")


def _h_adb_bugreport(args: Dict[str, Any], **_: Any) -> str:
    try:
        err = _check_serial(args.get("serial"))
        if err:
            return _err(err)
        out_dir, derr = _out_dir(args, "bugreport")
        if derr:
            return _err(derr)
        return _ok(_exec("collect-adb-bugreport", [str(args["serial"]).strip()],
                         ["--output-directory", out_dir], REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"bugreport failed: {exc}")


def _h_adb_app(args: Dict[str, Any], **_: Any) -> str:
    try:
        action = str(args.get("action") or "").strip().lower()
        err = _check_serial(args.get("serial"))
        if err:
            return _err(err)
        serial = str(args["serial"]).strip()
        package = str(args.get("package") or "").strip()
        if action in {"start", "stop", "uninstall", "install", "resolve", "pull"} and not package and action != "install":
            return _err(f"package is required for action={action}")
        opts: List[Any] = []
        if action == "resolve":
            return _ok(_exec("resolve-adb-packages", [serial, package]))
        if action == "start":
            return _ok(_exec("start-adb-app", [serial, package]))
        if action == "stop":
            return _ok(_exec("stop-adb-app", [serial, package]))
        if action == "uninstall":
            return _ok(_exec("uninstall-adb-package", [serial, package]))
        if action == "install":
            apk, perr = _check_path(args.get("apk_path") or args.get("apk"))
            if perr:
                return _err(perr)
            if _to_bool(args.get("replace")):
                opts += ["--replace", True]
            return _ok(_exec("install-adb-package", [serial, apk], opts, REA_LONG_TIMEOUT))
        if action == "pull":
            out_dir, derr = _out_dir(args, "apk-pull")
            if derr:
                return _err(derr)
            opts += ["--output-directory", out_dir]
            return _ok(_exec("pull-adb-package", [serial, package], opts, REA_LONG_TIMEOUT))
        if action == "activity":
            intent = str(args.get("intent") or args.get("action_value") or "").strip()
            if not intent:
                return _err("intent (ACTION) is required for action=activity")
            if args.get("data_uri"):
                opts += ["--data-uri", str(args["data_uri"])]
            if args.get("component"):
                opts += ["--component", str(args["component"])]
            for item in (args.get("extras") or []):
                if isinstance(item, str):
                    opts += ["--extra", item]
            return _ok(_exec("start-adb-activity", [serial, intent], opts))
        return _err("unknown action (packages|resolve|start|stop|install|uninstall|activity|pull)")
    except Exception as exc:
        return _err(f"adb app action failed: {exc}")


def _h_adb_transfer(args: Dict[str, Any], **_: Any) -> str:
    try:
        direction = str(args.get("direction") or "pull").strip().lower()
        err = _check_serial(args.get("serial"))
        if err:
            return _err(err)
        serial = str(args["serial"]).strip()
        opts: List[Any] = []
        if direction == "pull":
            remote = str(args.get("remote") or args.get("device_path") or "").strip()
            if not remote:
                return _err("remote device path is required")
            out_dir, derr = _out_dir(args, "adb-pull")
            if derr:
                return _err(derr)
            opts += ["--output-directory", out_dir]
            return _ok(_exec("pull-adb-file", [serial, remote], opts, REA_LONG_TIMEOUT))
        if direction == "push":
            local, perr = _check_path(args.get("local") or args.get("local_path"))
            if perr:
                return _err(perr)
            remote = str(args.get("remote") or args.get("device_path") or "").strip()
            if not remote:
                return _err("remote device path is required")
            if _to_bool(args.get("overwrite")):
                opts += ["--overwrite", True]
            return _ok(_exec("push-adb-file", [serial, local, remote], opts, REA_LONG_TIMEOUT))
        return _err("direction must be pull or push")
    except Exception as exc:
        return _err(f"adb transfer failed: {exc}")


# ── APK ───────────────────────────────────────────────────────────────────────

def _h_apk(args: Dict[str, Any], **_: Any) -> str:
    try:
        apk, err = _check_path(args.get("apk") or args.get("path"))
        if err:
            return _err(err)
        action = str(args.get("action") or "package").strip().lower()
        if action == "package":
            return _ok(_exec("inspect-android-package", [apk], timeout=REA_LONG_TIMEOUT))
        if action == "classes":
            query = str(args.get("query") or "").strip()
            return _ok(_exec("search-android-classes", [apk, query], timeout=REA_LONG_TIMEOUT))
        if action == "class":
            cls = str(args.get("class") or args.get("class_name") or "").strip()
            if not cls:
                return _err("class is required for action=class")
            return _ok(_exec("inspect-android-class", [apk, cls], timeout=REA_LONG_TIMEOUT))
        if action == "method":
            cls = str(args.get("class") or args.get("class_name") or "").strip()
            meth = str(args.get("method") or args.get("method_name") or "").strip()
            if not cls or not meth:
                return _err("class and method are required for action=method")
            opts: List[Any] = []
            if args.get("overload_index") not in (None, ""):
                opts += ["--overloadIndex", max(0, _to_int(args.get("overload_index"), 0))]
            return _ok(_exec("inspect-android-method", [apk, cls, meth], opts, REA_LONG_TIMEOUT))
        if action == "refs":
            cls = str(args.get("class") or args.get("class_name") or "").strip()
            if not cls:
                return _err("class is required for action=refs")
            opts = []
            if args.get("method"):
                opts += ["--methodName", str(args["method"])]
            if args.get("overload_index") not in (None, ""):
                opts += ["--overloadIndex", max(0, _to_int(args.get("overload_index"), 0))]
            return _ok(_exec("trace-android-references", [apk, cls], opts, REA_LONG_TIMEOUT))
        if action == "resources":
            opts = []
            if args.get("locale"):
                opts += ["--locale", str(args["locale"])]
            if args.get("strings"):
                opts += ["--strings", str(args["strings"])]
            return _ok(_exec("decode-android-resources", [apk], opts, REA_LONG_TIMEOUT))
        if action == "graph":
            spec, serr = _spec_arg(args, "android-graph")
            if serr:
                return _err(serr + " (action=graph needs the workflow JSON REA documents for "
                                   "project-android-application-graph)")
            return _ok(_exec("project-android-application-graph", [], [], REA_LONG_TIMEOUT, inline_json=spec))
        return _err("unknown action (package|classes|class|method|refs|resources|graph)")
    except Exception as exc:
        return _err(f"apk analysis failed: {exc}")


# ── JavaScript / Electron ─────────────────────────────────────────────────────

def _h_js_analyze(args: Dict[str, Any], **_: Any) -> str:
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        opts: List[Any] = []
        sources = args.get("source_paths")
        if isinstance(sources, str):
            sources = [s for s in re.split(r"[,\n]", sources) if s.strip()]
        for item in (sources or []):
            opts += ["--sourcePath", str(item)]
        if args.get("max_heap_mb") not in (None, ""):
            opts += ["--maxHeapMb", max(64, _to_int(args.get("max_heap_mb"), 1024))]
        if args.get("analysis_timeout_ms") not in (None, ""):
            opts += ["--analysisTimeoutMs", max(1000, _to_int(args.get("analysis_timeout_ms"), 300_000))]
        if args.get("integrity_policy"):
            opts += ["--integrityPolicy", str(args["integrity_policy"])]
        return _ok(_exec("analyze-javascript-application", [path], opts, REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"js analysis failed: {exc}")


def _h_js_compare(args: Dict[str, Any], **_: Any) -> str:
    try:
        action = str(args.get("action") or "source-to-bundle").strip().lower()
        spec, err = _spec_arg(args, f"js-{action}")
        if err:
            return _err(err)
        table = {
            "source-to-bundle": "compare-source-to-bundle",
            "versions": "compare-application-versions",
            "export-shapes": "compare-javascript-export-shapes",
            "trace": "trace-javascript-semantics",
            "reconcile-runtime": "reconcile-javascript-runtime",
            "feature": "trace-application-feature",
        }
        cmd = table.get(action)
        if not cmd:
            return _err(f"unknown action '{action}' ({'|'.join(sorted(table))})")
        return _ok(_exec(cmd, [], [], REA_LONG_TIMEOUT, inline_json=spec))
    except Exception as exc:
        return _err(f"js compare failed: {exc}")


def _h_js_recover(args: Dict[str, Any], **_: Any) -> str:
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        out = str(args.get("output_directory") or "").strip()
        if not out:
            return _err("output_directory is required (REA requires an absent directory)")
        out = str(Path(out).expanduser())
        Path(out).mkdir(parents=True, exist_ok=True)
        opts: List[Any] = []
        for extra in (args.get("additional_paths") or []):
            opts += ["--additionalPath", str(extra)]
        if args.get("extraction_mode"):
            opts += ["--extractionMode", str(args["extraction_mode"])]
        if args.get("rewrite_level"):
            opts += ["--rewriteLevel", str(args["rewrite_level"])]
        return _ok(_exec("recover-javascript-sources", [path, out], opts, REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"js recovery failed: {exc}")


# ── artifacts / binaries / native ─────────────────────────────────────────────

def _h_artifact(args: Dict[str, Any], **_: Any) -> str:
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        action = str(args.get("action") or "inspect").strip().lower()
        opts: List[Any] = []
        if args.get("integrity_policy"):
            opts += ["--integrityPolicy", str(args["integrity_policy"])]
        if action == "inspect":
            return _ok(_exec("inspect-artifact", [path], opts, REA_LONG_TIMEOUT))
        if action == "extract":
            return _ok(_exec("extract-artifact", [path], opts, REA_LONG_TIMEOUT))
        if action == "pe-resources":
            return _ok(_exec("inspect-pe-resources", [path], [], REA_LONG_TIMEOUT))
        if action == "layout":
            return _ok(_exec("inspect-binary-layout", [path], [], REA_LONG_TIMEOUT))
        if action == "managed":
            return _ok(_exec("inspect-managed-artifact", [path], [], REA_LONG_TIMEOUT))
        if action == "managed-members":
            return _ok(_exec("inspect-managed-members", [path], [], REA_LONG_TIMEOUT))
        if action == "dylib":
            return _ok(_exec("trace-dylib-resolution", [path], [], REA_LONG_TIMEOUT))
        if action == "plist":
            return _ok(_exec("inspect-plist", [path], [], REA_LONG_TIMEOUT))
        if action == "view":
            known = _command_known("inspect-analysis-view")
            if not known:
                return _err(_require_command("inspect-analysis-view") or "unavailable")
            return _ok(_exec("inspect-analysis-view", [], [], REA_LONG_TIMEOUT, inline_json=path))
        return _err("unknown action (inspect|extract|pe-resources|layout|managed|managed-members|dylib|plist|view)")
    except Exception as exc:
        return _err(f"artifact failed: {exc}")


def _h_compare(args: Dict[str, Any], **_: Any) -> str:
    try:
        kind = str(args.get("kind") or "artifacts").strip().lower()
        left, lerr = _check_path(args.get("left"))
        if lerr:
            return _err(lerr)
        right, rerr = _check_path(args.get("right"))
        if rerr:
            return _err(rerr)
        table = {
            "artifacts": "compare",
            "bundles": "compare-bundles",
            "processes": "compare-process-captures",
            "application-versions": "compare-application-versions",
            "export-shapes": "compare-javascript-export-shapes",
            "managed-members": "compare-managed-members",
            "wasm": "compare-wasm-interfaces",
            "web-captures": "compare-web-captures",
            "screenshots": "compare-web-screenshots",
        }
        cmd = table.get(kind)
        if not cmd:
            return _err(f"unknown kind '{kind}' ({'|'.join(sorted(table))})")
        opts: List[Any] = []
        if args.get("trace_spec"):
            spec_path, err = _check_path(args["trace_spec"])
            if err:
                return _err(err)
            return _ok(_exec(cmd, [left, right, spec_path], opts, REA_LONG_TIMEOUT))
        return _ok(_exec(cmd, [left, right], opts, REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"compare failed: {exc}")


def _h_crash(args: Dict[str, Any], **_: Any) -> str:
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        opts: List[Any] = []
        if _to_bool(args.get("debugger_context")):
            opts += ["--debuggerContext", True]
        return _ok(_exec("inspect-recorded-crash", [path], opts, REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"crash inspection failed: {exc}")


def _h_native(args: Dict[str, Any], **_: Any) -> str:
    """Provider-aware native operations; degrades explicitly when no provider is ready."""
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        action = str(args.get("action") or "overview").strip().lower()
        opts: List[Any] = []
        if args.get("provider"):
            opts += ["--provider", str(args["provider"])]
        if args.get("snapshot"):
            opts += ["--snapshot", str(args["snapshot"])]
        selector = str(args.get("selector") or args.get("name") or "").strip()
        mapping = {
            "overview": ("analyze", [path]),
            "inspect": ("inspect", [path]),
            "decompile": ("decompile", [path, selector]),
            "function": ("function", [path, selector]),
            "instructions": ("instructions", [path, selector]),
            "xrefs": ("xrefs", [path, selector]),
            "search": ("search", [path, selector]),
            "trace": ("trace", [path, selector]),
            "api": ("inspect-native-api", [path, selector]),
            "strings": ("search", [path, selector]),
        }
        entry = mapping.get(action)
        if not entry:
            return _err(f"unknown action ({'|'.join(sorted(mapping))})")
        cmd, pos = entry
        if None in pos or (action != "overview" and action != "inspect" and not selector):
            return _err(f"action={action} needs 'selector' (function/procedure name or literal)")
        missing = _require_command(cmd)
        if missing:
            return _err(missing)
        engines = {r["engine"]: r for r in _engine_report()}
        if not any(engines.get(e, {}).get("ready") for e in ("ghidra", "rizin", "objdump")):
            return _err(
                "no native analysis provider is ready: set GHIDRA_INSTALL_DIR (Ghidra 12.1.x), "
                "or install Rizin/objdump. Static artifact tools (rea_artifact) need no engine.",
                engines_needed=["ghidra", "rizin", "objdump"])
        return _ok(_exec(cmd, pos, opts, REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"native action failed: {exc}")


# ── process capture ───────────────────────────────────────────────────────────

def _h_process_capture(args: Dict[str, Any], **_: Any) -> str:
    try:
        spec = args.get("spec")
        if not isinstance(spec, dict):
            executable = str(args.get("executable") or "").strip()
            if not executable:
                return _err("executable is required (or pass a full `spec` object)")
            argv = args.get("arguments")
            if isinstance(argv, str):
                argv = shlex.split(argv)
            if not isinstance(argv, list):
                argv = []
            spec = {"executable": executable, "arguments": [str(a) for a in argv]}
            timeout_ms = args.get("timeout_ms")
            if timeout_ms not in (None, ""):
                spec["timeout_ms"] = max(100, _to_int(timeout_ms, 5000))
            cwd = args.get("working_directory")
            if cwd:
                path, err = _check_path(cwd)
                if err:
                    return _err(err)
                spec["working_directory"] = path
            observe = args.get("filesystem_observation_paths")
            if isinstance(observe, list) and observe:
                spec["filesystem_observation_paths"] = [str(p) for p in observe]
            limits = args.get("limits")
            if isinstance(limits, dict):
                spec["limits"] = limits
        if "executable" not in spec:
            return _err("spec must set 'executable'")
        spec_path = _write_spec("process", spec)
        return _ok(_exec("capture-process", [spec_path], [], REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"process capture failed: {exc}")


# ── evidence ──────────────────────────────────────────────────────────────────

def _h_evidence(args: Dict[str, Any], **_: Any) -> str:
    try:
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            rows = _list_evidence(_to_int(args.get("limit", 40), 40))
            return _ok({"count": len(rows), "evidence_dir": str(REA_EVIDENCE_DIR),
                        "retention_days": REA_EVIDENCE_RETENTION_DAYS, "items": rows})
        if action == "get":
            ev = str(args.get("evidence_id") or "").strip()
            if not ev:
                return _err("evidence_id is required")
            if not REA_EVIDENCE_DIR.is_dir():
                return _err("no evidence store yet")
            hits = sorted(REA_EVIDENCE_DIR.glob(f"{ev}*.json"))
            if not hits:
                return _err(f"no stored evidence for {ev}")
            text, spill = _spill(hits[0].read_text(encoding="utf-8"))
            return text + (f"\n\n[full at {spill}]" if spill else "")
        if action == "export":
            source = str(args.get("source") or "").strip()
            if not source:
                return _err("source is required (REA evidence source reference)")
            out = str(args.get("output") or "").strip()
            if not out:
                return _err("output path is required")
            out = str(Path(out).expanduser())
            opts: List[Any] = []
            if _to_bool(args.get("overwrite")):
                opts += ["--overwrite", True]
            return _ok(_exec("evidence-export", [source, out], opts, REA_LONG_TIMEOUT))
        if action == "import":
            path, err = _check_path(args.get("path") or args.get("bundle"))
            if err:
                return _err(err)
            return _ok(_exec("evidence-import", [path], [], REA_LONG_TIMEOUT))
        if action == "inspect":
            path, err = _check_path(args.get("path") or args.get("bundle"))
            if err:
                return _err(err)
            return _ok(_exec("inspect-evidence-bundle", [], [], REA_LONG_TIMEOUT,
                             inline_json=_write_spec("evidence-bundle", {"bundle": path})))
        if action == "prune":
            removed = _prune_evidence()
            return _ok({"removed": removed, "retention_days": REA_EVIDENCE_RETENTION_DAYS})
        if action == "unknowns":
            spec, err = _spec_arg(args, "unknowns")
            if err:
                return _err(err)
            return _ok(_exec("list_unknowns" if _command_known("list_unknowns") else "audit-residual-unknowns",
                             [], [], REA_LONG_TIMEOUT, inline_json=spec))
        return _err("unknown action (list|get|export|import|inspect|prune|unknowns)")
    except Exception as exc:
        return _err(f"evidence action failed: {exc}")


# ── firmware / sqlite / binary metadata / web ────────────────────────────────

def _h_firmware(args: Dict[str, Any], **_: Any) -> str:
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        action = str(args.get("action") or "regions").strip().lower()
        if action == "regions":
            return _ok(_exec("inspect-firmware-regions", [path], [], REA_LONG_TIMEOUT))
        if action == "extract":
            out = str(args.get("output_directory") or "").strip()
            if not out:
                return _err("output_directory is required")
            out = str(Path(out).expanduser())
            Path(out).mkdir(parents=True, exist_ok=True)
            opts: List[Any] = []
            if args.get("offset") not in (None, ""):
                opts += ["--offset", _to_int(args.get("offset"), 0)]
            if args.get("length") not in (None, ""):
                opts += ["--length", _to_int(args.get("length"), 0)]
            if args.get("max_depth") not in (None, ""):
                opts += ["--maxDepth", max(1, _to_int(args.get("max_depth"), 3))]
            return _ok(_exec("extract-firmware", [path, out], opts, REA_LONG_TIMEOUT))
        return _err("unknown action (regions|extract)")
    except Exception as exc:
        return _err(f"firmware action failed: {exc}")


def _h_sqlite(args: Dict[str, Any], **_: Any) -> str:
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        missing = _require_command("inspect-sqlite-database")
        if missing:
            return _err(missing + " (SQLite inspection is main-branch only on 6.4.0)")
        opts: List[Any] = []
        if args.get("table"):
            opts += ["--table", str(args["table"])]
            if args.get("row_limit") not in (None, ""):
                # REA rejects row_limit without an explicit table selection.
                opts += ["--rowLimit", max(0, _to_int(args.get("row_limit"), 20))]
        if args.get("column"):
            opts += ["--column", str(args["column"])]
        if args.get("schema_scope"):
            opts += ["--schemaScope", str(args["schema_scope"])]
        return _ok(_exec("inspect-sqlite-database", [path], opts, REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"sqlite inspection failed: {exc}")


def _h_binary_meta(args: Dict[str, Any], **_: Any) -> str:
    try:
        path, err = _check_path(args.get("path"))
        if err:
            return _err(err)
        kind = str(args.get("kind") or "auto").strip().lower()
        if kind == "auto":
            head = b""
            try:
                with open(path, "rb") as fh:
                    head = fh.read(4096)
            except Exception as exc:
                return _err(f"cannot read file: {exc}")
            if b"Go build ID" in head or b"Go\x00\x00" in head:
                kind = "go"
            elif b".dep-v0" in head or b"rustc" in head:
                kind = "rust"
            else:
                kind = "rust"  # rust/go inspectors both accept any ELF/PE and report null metadata
        if kind == "go":
            cmd = "inspect-go-binary"
        elif kind == "rust":
            cmd = "inspect-rust-binary"
        elif kind == "objdump":
            operation = str(args.get("operation") or "headers")
            missing = _require_command("inspect-with-objdump")
            if missing:
                return _err(missing)
            opts = ["--followDebugLinks", True] if _to_bool(args.get("follow_debug_links")) else []
            return _ok(_exec("inspect-with-objdump", [path, operation], opts, REA_LONG_TIMEOUT))
        elif kind == "rizin":
            command = str(args.get("command") or "iIj").strip()
            missing = _require_command("execute-rizin-command")
            if missing:
                return _err(missing)
            return _ok(_exec("execute-rizin-command", [path, command], [], REA_LONG_TIMEOUT))
        elif kind == "strings":
            return _ok(_exec("search", [path, str(args.get("query") or "")], [], REA_LONG_TIMEOUT))
        else:
            return _err("kind must be auto|go|rust|objdump|rizin|strings")
        missing = _require_command(cmd)
        if missing:
            return _err(missing)
        return _ok(_exec(cmd, [path], [], REA_LONG_TIMEOUT))
    except Exception as exc:
        return _err(f"binary metadata failed: {exc}")


def _h_web(args: Dict[str, Any], **_: Any) -> str:
    try:
        action = str(args.get("action") or "targets").strip().lower()
        endpoint = str(args.get("endpoint") or "").strip()
        if not endpoint:
            return _err("endpoint is required (a literal-port loopback CDP endpoint, "
                        "e.g. http://127.0.0.1:9222)")
        if action in {"targets", "electron-targets", "runtime-targets"}:
            cmd = {"targets": "list-browser-targets", "electron-targets": "list-electron-targets",
                   "runtime-targets": "list-javascript-runtime-targets"}[action]
            opts: List[Any] = []
            if args.get("allowed_origins"):
                origins = args["allowed_origins"]
                if isinstance(origins, str):
                    origins = [o for o in origins.split(",") if o.strip()]
                for origin in origins:
                    opts += ["--allowedOrigins", str(origin)]
            return _ok(_exec(cmd, [endpoint], opts, REA_LONG_TIMEOUT))
        target = str(args.get("target_id") or "").strip()
        if not target:
            return _err("target_id is required (call rea_web action=targets first)")
        opts = []
        if action == "page":
            if args.get("observation_ms") not in (None, ""):
                opts += ["--observationMs", max(50, _to_int(args.get("observation_ms"), 1500))]
            for flag, key in (("--includeAccessibilityText", "include_accessibility_text"),
                              ("--includeConsoleText", "include_console_text"),
                              ("--includeScriptSources", "include_script_sources"),
                              ("--includeStorageKeys", "include_storage_keys")):
                if _to_bool(args.get(key)):
                    opts += [flag, True]
            return _ok(_exec("inspect-web-page", [endpoint, target], opts, REA_LONG_TIMEOUT))
        if action == "session":
            opts = []
            if args.get("observation_ms") not in (None, ""):
                opts += ["--observationMs", max(50, _to_int(args.get("observation_ms"), 1500))]
            return _ok(_exec("observe-web-session", [endpoint, target], opts, REA_LONG_TIMEOUT))
        if action == "screenshot":
            return _ok(_exec("capture-web-screenshot", [endpoint, target], [], REA_LONG_TIMEOUT))
        if action == "bundle":
            opts = []
            if _to_bool(args.get("fetch_source_maps")):
                opts += ["--fetchSourceMaps", True]
            return _ok(_exec("analyze-web-bundle", [endpoint, target], opts, REA_LONG_TIMEOUT))
        if action == "electron":
            return _ok(_exec("inspect-electron-page", [endpoint, target], [], REA_LONG_TIMEOUT))
        if action == "runtime":
            return _ok(_exec("observe-javascript-runtime", [endpoint, target], [], REA_LONG_TIMEOUT))
        if action == "network":
            capture = str(args.get("capture_path") or "").strip()
            fmt = str(args.get("format") or "har").strip()
            if not capture:
                return _err("capture_path is required for action=network")
            path, err = _check_path(capture)
            if err:
                return _err(err)
            opts = []
            if _to_bool(args.get("record")):
                opts += ["--record", True]
            return _ok(_exec("inspect-web-network-capture", [path, fmt], opts, REA_LONG_TIMEOUT))
        return _err("unknown action (targets|page|session|screenshot|bundle|electron|runtime|network|"
                    "electron-targets|runtime-targets)")
    except Exception as exc:
        return _err(f"web action failed: {exc}")


# ── composites ────────────────────────────────────────────────────────────────

def _h_mobile_audit(args: Dict[str, Any], **_: Any) -> str:
    """One evidence-chained device -> package -> logcat -> screen audit."""
    try:
        err = _check_serial(args.get("serial"))
        if err:
            return _err(err)
        serial = str(args["serial"]).strip()
        package = str(args.get("package") or "").strip()
        steps: Dict[str, Any] = {}
        steps["device"] = _exec("inspect-adb-device", [serial])
        steps["display"] = _exec("inspect-adb-display", [serial])
        if package:
            steps["package"] = _exec("inspect-adb-package", [serial, package])
            steps["processes"] = _exec("list-adb-processes", [serial])
        opts: List[Any] = []
        if args.get("logcat_count") not in (None, ""):
            opts += ["--count", max(1, min(_to_int(args.get("logcat_count"), 300), 20_000))]
        if args.get("buffer"):
            opts += ["--buffer", str(args["buffer"])]
        steps["logcat"] = _exec("read-adb-logcat", [serial], opts)
        screen_dir, derr = _out_dir(args, "mobile-audit-screen")
        if derr:
            return _err(derr)
        screen_opts: List[Any] = ["--output-directory", screen_dir]
        steps["screen"] = _exec("capture-adb-screen", [serial], screen_opts)
        if _to_bool(args.get("bugreport")):
            report_dir, rerr = _out_dir(args, "mobile-audit-bugreport")
            if rerr:
                return _err(rerr)
            steps["bugreport"] = _exec("collect-adb-bugreport", [serial],
                                       ["--output-directory", report_dir], REA_LONG_TIMEOUT)
        evidence = sorted({v.get("evidence_id") for v in steps.values() if isinstance(v, dict) and v.get("evidence_id")})
        failures = [k for k, v in steps.items() if isinstance(v, dict) and not v.get("ok")]
        return _ok({
            "serial": serial, "package": package or None,
            "evidence_ids": evidence,
            "failed_steps": failures,
            "screen_directory": screen_dir,
            "steps": steps,
            "stored_evidence": _list_evidence(len(evidence) or 5),
        })
    except Exception as exc:
        return _err(f"mobile audit failed: {exc}")


def _h_verify_deploy(args: Dict[str, Any], **_: Any) -> str:
    """Compare what shipped against the source/build inputs, with digests."""
    try:
        artifacts = args.get("artifacts") or []
        if isinstance(artifacts, str):
            artifacts = [a for a in artifacts.split(",") if a.strip()]
        digests: List[Dict[str, Any]] = []
        for item in artifacts:
            path, perr = _check_path(item)
            if perr:
                digests.append({"path": item, "error": perr})
                continue
            import hashlib
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            digests.append({"path": path, "size": Path(path).stat().st_size,
                            "sha256": digest.hexdigest()})
        out: Dict[str, Any] = {"digests": digests}
        bundle = args.get("bundle")
        source = args.get("source")
        if bundle and source:
            spec = {"source": str(Path(str(source)).expanduser()),
                    "bundle": str(Path(str(bundle)).expanduser())}
            if isinstance(args.get("extra"), dict):
                spec.update(args["extra"])
            out["source_to_bundle"] = _exec(
                "compare-source-to-bundle", [], [], REA_LONG_TIMEOUT,
                inline_json=_write_spec("verify-deploy", spec))
        elif bundle:
            out["bundle_inventory"] = _exec("inspect-artifact", [str(Path(str(bundle)).expanduser())], [],
                                            REA_LONG_TIMEOUT)
        return _ok(out)
    except Exception as exc:
        return _err(f"verify deploy failed: {exc}")


# ── MCP registration ─────────────────────────────────────────────────────────

def _write_mcp_wrapper() -> Path:
    """Wrapper that injects engine env then execs REA's MCP server."""
    argv = _rea_argv()
    env = _load_engine_env()
    export_lines = [f'export {k}="{v}"' for k, v in env.items() if k != "PATH_EXTRA" and v]
    if env.get("PATH_EXTRA"):
        export_lines.append(f'export PATH="{env["PATH_EXTRA"]}:$PATH"')
    quoted = " ".join(shlex.quote(a) for a in argv)
    body = "#!/bin/sh\n# generated by hermes-rea — injects the analysis-engine environment\n"
    body += "\n".join(export_lines) + ("\n" if export_lines else "")
    body += f'exec {quoted} mcp "$@"\n'
    _atomic_write(REA_MCP_WRAPPER, body)
    REA_MCP_WRAPPER.chmod(0o755)
    return REA_MCP_WRAPPER


def _h_mcp_setup(args: Dict[str, Any], **_: Any) -> str:
    """Register REA with Hermes (wrapper + `hermes mcp add`) and install its skill."""
    try:
        apply = _to_bool(args.get("apply"))
        install_skill = _to_bool(args.get("install_skill"), True)
        wrapper = _write_mcp_wrapper()
        plan: Dict[str, Any] = {
            "wrapper": str(wrapper),
            "launcher": _launcher_label(),
            "commands": [" ".join(_rea_argv()) + " setup --client hermes",
                         f"hermes mcp add rea --command {wrapper}"],
        }
        if not apply:
            plan["dry_run"] = True
            plan["note"] = "pass apply=true to execute (config.yaml gets a .rea.backup first via rea setup)"
            return _ok(plan)
        results: List[Dict[str, Any]] = []
        if install_skill:
            # --yes: non-interactive approval of user-owned setup actions (config entry + skill).
            code, out, err = _run(list(_rea_argv()) + ["setup", "--yes", "--client", "hermes"], 600)
            results.append({"step": "rea setup --yes --client hermes", "exit_code": code,
                            "stdout": out.strip()[-1500:], "stderr": err.strip()[-800:]})
        hermes = _which("hermes")
        if hermes:
            # hermes mcp add asks "Enable all N tools?" — answer it so the call cannot hang.
            code, out, err = _run([hermes, "mcp", "add", "rea", "--command", str(wrapper)], 300,
                                  stdin_text="Y\n")
            results.append({"step": f"hermes mcp add rea --command {wrapper}", "exit_code": code,
                            "stdout": out.strip()[-1200:], "stderr": err.strip()[-800:]})
        else:
            results.append({"step": "hermes mcp add", "exit_code": 127,
                            "stderr": "hermes CLI not on PATH — add the entry manually:\n"
                                      f"  mcp_servers:\n    rea:\n      command: {wrapper}\n      enabled: true"})
        code, out, err = _run([hermes or "hermes", "mcp", "test", "rea"], 300) if hermes else (127, "", "no hermes CLI")
        results.append({"step": "hermes mcp test rea", "exit_code": code,
                        "stdout": out.strip()[-1500:], "stderr": err.strip()[-600:]})
        plan["applied"] = results
        if _to_bool(args.get("tune"), True):
            plan["tuned"] = _tune_mcp_entry()
        plan["registration"] = _mcp_registration_state()
        plan["restart_required"] = "Hermes discovers MCP tools at startup — restart to load rea's tools"
        return _ok(plan)
    except Exception as exc:
        return _err(f"mcp setup failed: {exc}")


def _h_mcp_status(args: Dict[str, Any], **_: Any) -> str:
    try:
        state = _mcp_registration_state()
        state["full_surface_available"] = bool((_manifest().get("commands") or {}).get("inspect-sqlite-database"))
        state["mcp_tool_hint"] = ("the MCP server advertises the full surface "
                                  "(~207 tools on main, ~170 on the 6.4.0 release)")
        return _ok(state)
    except Exception as exc:
        return _err(f"mcp status failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

def _d(text: str) -> Dict[str, str]:
    return {"type": "string", "description": text}


def _simple_tool(name: str, cmd: str, description: str, params: Dict[str, Any],
                 build: Callable[[Dict[str, Any]], Tuple[List[Any], List[Any]]],
                 timeout: Optional[int] = None) -> Dict[str, Any]:
    """Declare a one-command tool; the handler validates the command exists first."""

    def _handler(args: Dict[str, Any], **_: Any) -> str:
        try:
            missing = _require_command(cmd)
            if missing:
                return _err(missing)
            pos, opts = build(args)
            return _ok(_exec(cmd, pos, opts, timeout))
        except Exception as exc:
            return _err(f"{name} failed: {exc}")

    return {"name": name, "cmd": cmd, "description": description,
            "params": params, "handler": _handler}


# tools that map 1:1 onto a single verified REA command
def _tool_table() -> List[Dict[str, Any]]:
    serial = _d("Device serial exactly as `adb devices` reports it (see rea_adb_devices)")
    return [
        _simple_tool("rea_adb_packages", "list-adb-packages",
                     "List installed Android packages on one device with their base APK paths (ADB).",
                     {"serial": serial, "scope": _d("Optional package scope filter")},
                     lambda a: ([a.get("serial")],
                                ["--scope", a["scope"]] if a.get("scope") else [])),
        _simple_tool("rea_adb_processes", "list-adb-processes",
                     "List processes on the connected Android device exactly as ps -A reports them.",
                     {"serial": serial}, lambda a: ([a.get("serial")], [])),
        _simple_tool("rea_adb_services", "list-adb-services",
                     "List binder services and interfaces on the connected Android device.",
                     {"serial": serial}, lambda a: ([a.get("serial")], [])),
        _simple_tool("rea_adb_features", "list-adb-features",
                     "List the device's declared hardware and software features.",
                     {"serial": serial}, lambda a: ([a.get("serial")], [])),
        _simple_tool("rea_adb_directory", "list-adb-directory",
                     "List one device directory with kinds, permissions and sizes.",
                     {"serial": serial, "device_path": _d("Absolute path on the device, e.g. /sdcard/Download")},
                     lambda a: ([a.get("serial"), a.get("device_path")], [])),
        _simple_tool("rea_adb_setting", "read-adb-setting",
                     "Read one Android settings value from the system, secure or global namespace.",
                     {"serial": serial, "namespace": _d("system | secure | global"),
                      "key": _d("Setting key, e.g. screen_brightness")},
                     lambda a: ([a.get("serial"), a.get("namespace"), a.get("key")], [])),
        _simple_tool("rea_frida_devices", "list-frida-devices",
                     "List Frida devices and processes available for live instrumentation.",
                     {}, lambda a: ([], [])),
        _simple_tool("rea_dart_flutter", "identify-flutter-build",
                     "Identify a Flutter application build (Dart snapshot hash, per-ABI digests, engine provenance).",
                     {"path": _d("Absolute path to an APK or Flutter artifact")},
                     lambda a: ([a.get("path")], []), REA_LONG_TIMEOUT),
        _simple_tool("rea_dart_aot", "inspect-dart-aot",
                     "Inspect a Dart AOT snapshot inside a Flutter application artifact.",
                     {"path": _d("Absolute path to an APK or libapp.so"),
                      "abi": _d("Optional ABI selector (arm64-v8a, armeabi-v7a, x86_64)")},
                     lambda a: ([a.get("path")], ["--abi", a["abi"]] if a.get("abi") else []), REA_LONG_TIMEOUT),
        _simple_tool("rea_wasm", "inspect-wasm-artifact",
                     "Validate a WASM artifact and return its sections plus exact import/export WAT forms.",
                     {"path": _d("Absolute path to a .wasm / .wat artifact")},
                     lambda a: ([a.get("path")], []), REA_LONG_TIMEOUT),
        _simple_tool("rea_evm", "inspect-evm-interface",
                     "Infer interface candidates (selectors, offsets, mutability) from local EVM bytecode.",
                     {"path": _d("Absolute path to raw/hex EVM bytecode carrier"),
                      "encoding": _d("Bytecode encoding, e.g. hex")},
                     lambda a: ([a.get("path"), a.get("encoding")], []), REA_LONG_TIMEOUT),
        _simple_tool("rea_pcap", "inspect-pcap-capture",
                     "Summarise a pcap: endpoints, DNS, TLS SNI, protocol distribution, HTTP requests.",
                     {"path": _d("Absolute path to a .pcap/.pcapng capture")},
                     lambda a: ([a.get("path")], []), REA_LONG_TIMEOUT),
        _simple_tool("rea_packets", "inspect-packet-capture",
                     "Inspect recorded packets: selected fields, repeated values, filters and decode-as rules.",
                     {"path": _d("Absolute path to a capture file"),
                      "fields": _d("Optional comma-separated field selection"),
                      "display_filter": _d("Optional tshark display filter expression"),
                      "packet_limit": _d("Optional cap on inspected packets")},
                     lambda a: ([a.get("path")],
                                [k for pair in (
                                    ("--fields", a["fields"]) if a.get("fields") else None,
                                    ("--display_filter", a["display_filter"]) if a.get("display_filter") else None,
                                    ("--packet_limit", max(1, _to_int(a.get("packet_limit"), 100))) if a.get("packet_limit") not in (None, "") else None,
                                ) if pair for k in pair]), REA_LONG_TIMEOUT),
        _simple_tool("rea_signals", "decode-signal-recording",
                     "Decode a recorded logic-signal capture into decoder annotations, sample ranges and diagnostics.",
                     {"path": _d("Absolute path to a sigrok-compatible recording"),
                      "decoders": _d("Optional comma-separated sigrok decoder stack, e.g. uart:baudrate=115200")},
                     lambda a: ([a.get("path")],
                                ["--decoders", a["decoders"]] if a.get("decoders") else []), REA_LONG_TIMEOUT),
        _simple_tool("rea_gdb", "debug-with-gdb",
                     "Run a bounded interactive GDB session command against a target under the caller's permissions.",
                     {"command": _d("GDB command line to execute"),
                      "inferior_tty": _d("Optional inferior TTY path")},
                     lambda a: ([a.get("command")],
                                ["--inferiorTty", a["inferior_tty"]] if a.get("inferior_tty") else []), REA_LONG_TIMEOUT),
        _simple_tool("rea_rizin", "execute-rizin-command",
                     "Execute one Rizin command against a binary and return its output as evidence.",
                     {"path": _d("Absolute path to a binary"),
                      "command": _d("Rizin command, e.g. iIj or afl")},
                     lambda a: ([a.get("path"), a.get("command")], []), REA_LONG_TIMEOUT),
        _simple_tool("rea_objdump", "inspect-with-objdump",
                     "Inspect an ELF with caller-supplied GNU objdump (offline layout, symbols, relocations).",
                     {"path": _d("Absolute path to an ELF binary"),
                      "operation": _d("objdump operation, e.g. headers | symbols | disassemble"),
                      "follow_debug_links": _d("Follow debug links when resolving separate debug files")},
                     lambda a: ([a.get("path"), a.get("operation")],
                                ["--followDebugLinks", True] if _to_bool(a.get("follow_debug_links")) else []),
                     REA_LONG_TIMEOUT),
        _simple_tool("rea_frida_instrument", "instrument-with-frida",
                     "Instrument one target with Frida (attach/spawn) and return bounded observations.",
                     {"mode": _d("Frida mode, e.g. attach | spawn | list"),
                      "target": _d("Target process name/PID or spawn target"),
                      "script_file": _d("Optional JS script file to load"),
                      "device_id": _d("Optional Frida device id"),
                      "duration_ms": _d("Optional bounded run duration in milliseconds")},
                     lambda a: ([a.get("mode"), a.get("target")],
                                [k for pair in (
                                    ("--scriptFile", a["script_file"]) if a.get("script_file") else None,
                                    ("--deviceId", a["device_id"]) if a.get("device_id") else None,
                                    ("--durationMs", max(100, _to_int(a.get("duration_ms"), 5000))) if a.get("duration_ms") not in (None, "") else None,
                                ) if pair for k in pair]), REA_LONG_TIMEOUT),
        _simple_tool("rea_screenshot_compare", "compare-web-screenshots",
                     "Compare two self-verifying screenshot artifacts (visual regression with channel threshold).",
                     {"left": _d("Absolute path to the before JSON artifact"),
                      "right": _d("Absolute path to the after JSON artifact"),
                      "channel_threshold": _d("Optional per-channel difference threshold")},
                     lambda a: ([a.get("left"), a.get("right")],
                                ["--channelThreshold", a["channel_threshold"]] if a.get("channel_threshold") else []),
                     REA_LONG_TIMEOUT),
        _simple_tool("rea_process_compare", "compare-process-captures",
                     "Compare two saved process-capture evidence files (terminal, exit, filesystem, process).",
                     {"left": _d("Absolute path to the authority capture JSON"),
                      "right": _d("Absolute path to the reconstruction capture JSON"),
                      "trace_spec": _d("Optional trace-specification JSON for independent scheduling")},
                     lambda a: ([a.get("left"), a.get("right")] + ([a["trace_spec"]] if a.get("trace_spec") else []), []),
                     REA_LONG_TIMEOUT),
        _simple_tool("rea_apktool_client", "inspect-apktool-client",
                     "Check the caller-supplied apktool launcher readiness without decoding anything.",
                     {"command": _d("Optional absolute apktool launcher path (else PATH is used)")},
                     lambda a: ([], []), REA_LONG_TIMEOUT),
        _simple_tool("rea_jadx_client", "inspect-android-package",
                     "Probe APK/JADX readiness by inspecting an APK's manifest declarations.",
                     {"path": _d("Absolute path to an APK")},
                     lambda a: ([a.get("path")], []), REA_LONG_TIMEOUT),
    ]


def register(ctx) -> Dict[str, Any]:
    """Register hermes-rea tools and commands."""
    toolset = "rea"
    count = 0

    def _add(name: str, description: str, params: Dict[str, Any], handler: Callable[..., str]) -> None:
        nonlocal count
        ctx.register_tool(
            name=name,
            toolset=toolset,
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": params}},
            handler=handler,
        )
        count += 1

    # core / discovery
    _add("rea_status",
         "REA readiness for this host: resolved launcher, discovered command count, engine readiness, "
         "evidence store, and whether the REA MCP server is registered with Hermes. Call this first when "
         "any reverse-engineering task starts, or when another rea_* tool reports a missing engine.",
         {"refresh": _d("Re-parse REA's command manifest before answering (bool)")}, _h_status)
    _add("rea_engines",
         "Per-engine readiness for REA with the exact remediation for anything missing "
         "(GHIDRA_INSTALL_DIR, REA_JADX_MCP_JAR, apktool, tshark, binwalk, unblob, rizin, frida, wabt, "
         "pwntools, mitmdump, gdb, wakaru, dotnet/ilspycmd, adb, objdump, node).",
         {}, _h_engines)
    _add("rea_commands",
         "Search REA's own command manifest (parsed from `rea --llms`): exact usage, required arguments and "
         "options for every available command. Use before rea_run so argument shapes are never guessed.",
         {"query": _d("Substring to match against command names and descriptions"),
          "limit": _d("Max rows (default 40)")}, _h_commands)
    _add("rea_run",
         "Execute any command from REA's manifest with explicit positional args and options. Validated against "
         "the installed build; returns REA's own diagnostics when a shape is wrong. Use for commands without a "
         "dedicated rea_* tool.",
         {"command": _d("REA command name, e.g. inspect-managed-members"),
          "args": _d("Positional arguments in order (list or shell-style string)"),
          "options": _d("Options as a map {--flag: value}, a list, or an even-token string"),
          "timeout": _d("Optional timeout in seconds")}, _h_run)
    _add("rea_doctor",
         "Run REA's own doctor: client registrations, provider availability, skill identity, and catalog identity.",
         {"client": _d("Optional client id to scope the doctor (e.g. hermes)"),
          "target": _d("Optional app path to check")}, _h_doctor)
    _add("rea_providers",
         "List configured analysis providers and their availability as REA sees them.",
         {}, _h_providers)
    _add("rea_capabilities",
         "List provider capabilities and side effects (which operations mutate target/session/filesystem).",
         {}, _h_capabilities)

    # Android device
    _add("rea_adb_devices",
         "List attached Android devices/emulators with serial, state, transport and inferred kind. "
         "Start here for any real-device verification; every other adb tool needs the serial.",
         {}, _h_adb_devices)
    _add("rea_adb_inspect",
         "Inspect one Android device or package: build identity, display, current window, adb client, installed "
         "packages, processes, binder services, features, directory listings, settings.",
         {"serial": _d("Device serial"),
          "kind": _d("device|display|window|client|package|packages|processes|services|features|directory|setting"),
          "package": _d("Package name (kind=package)"),
          "device_path": _d("Device path (kind=directory)"),
          "namespace": _d("Settings namespace (kind=setting): system|secure|global"),
          "key": _d("Settings key (kind=setting)"),
          "scope": _d("Package scope filter (kind=packages)")}, _h_adb_inspect)
    _add("rea_adb_logcat",
         "Read a bounded device log dump (per buffer, optionally by pid) — the on-device evidence for a player-"
         "visible bug or crash.",
         {"serial": _d("Device serial"),
          "count": _d("Max lines (default 200)"),
          "buffer": _d("Log buffer, e.g. main|system|crash"),
          "pid": _d("Optional pid to filter")}, _h_adb_logcat)
    _add("rea_adb_screen",
         "Capture one screen frame from the device as a digested PNG artifact (pair with vision analysis to "
         "actually read what rendered).",
         {"serial": _d("Device serial"),
          "output_directory": _d("Optional output directory for the PNG "
                                 "(default: <hermes state>/rea/artifacts/screen)")}, _h_adb_screen)
    _add("rea_adb_bugreport",
         "Collect and digest a device-generated bugreport archive (heavy; bounded and evidence-backed).",
         {"serial": _d("Device serial"),
          "output_directory": _d("Optional output directory "
                                 "(default: <hermes state>/rea/artifacts/bugreport)")}, _h_adb_bugreport)
    _add("rea_adb_app",
         "Android app lifecycle and queries: resolve packages, start/stop apps, start an intent/activity, "
         "install/uninstall an APK, pull the installed APK set.",
         {"serial": _d("Device serial"),
          "action": _d("packages|resolve|start|stop|install|uninstall|activity|pull"),
          "package": _d("Package name (or substring for resolve)"),
          "apk_path": _d("APK path (action=install)"),
          "replace": _d("Re-install keeping app data (action=install)"),
          "intent": _d("Intent action (action=activity)"),
          "data_uri": _d("Optional intent data URI"),
          "component": _d("Optional intent component"),
          "extras": _d("Optional list of --extra values"),
          "output_directory": _d("Output directory (action=pull)")}, _h_adb_app)
    _add("rea_adb_transfer",
         "Move files between this host and the device: pull a device file, push a local file, with digests.",
         {"serial": _d("Device serial"),
          "direction": _d("pull|push"),
          "remote": _d("Device path"),
          "local": _d("Local absolute path (direction=push, or a file to push)"),
          "output_directory": _d("Output directory (direction=pull)"),
          "overwrite": _d("Allow overwriting an existing device target (direction=push)")}, _h_adb_transfer)

    # APK + JS
    _add("rea_apk",
         "Static Android APK analysis: manifest/declarations, class search, class members, method decompilation, "
         "incoming references, decoded resources/strings/locales, application graph. Method decompilation needs "
         "the caller-supplied JADX headless MCP jar (see rea_engines).",
         {"apk": _d("Absolute path to the APK"),
          "action": _d("package|classes|class|method|refs|resources|graph"),
          "query": _d("Class-name query (action=classes; empty lists all)"),
          "class": _d("Class name (class|method|refs)"),
          "method": _d("Method name (action=method) or method filter (action=refs)"),
          "overload_index": _d("Overload index when a method is ambiguous"),
          "locale": _d("Resource locale filter (action=resources)"),
          "strings": _d("String-table filter (action=resources)"),
          "spec": _d("Workflow JSON for action=graph (REA's project-android-application-graph input)"),
          "spec_path": _d("Path to that workflow JSON (alternative to spec)")}, _h_apk)
    _add("rea_js_analyze",
         "Statically reconstruct a local JavaScript/Electron application (ASAR or extracted tree): modules, "
         "imports, source maps, routes, IPC and native add-on relationships. Bound big trees with source_paths.",
         {"path": _d("Absolute path to an ASAR or extracted app directory"),
          "source_paths": _d("Optional exact inventoried paths to analyse (list)"),
          "max_heap_mb": _d("Analyzer V8 old-space heap MiB (default 1024)"),
          "analysis_timeout_ms": _d("Per-source deadline ms (default 300000)"),
          "integrity_policy": _d("fail|warn|ignore when declared integrity mismatches")}, _h_js_analyze)
    _add("rea_js_compare",
         "Compare JavaScript application evidence: committed source vs shipped bundle, two app versions, export "
         "shapes, semantic traces, static-vs-runtime reconciliation. Pass `spec` (object) or spec_path.",
         {"action": _d("source-to-bundle|versions|export-shapes|trace|reconcile-runtime|feature"),
          "spec": _d("Workflow JSON object (or inline JSON string)"),
          "spec_path": _d("Path to a workflow JSON file (alternative to spec)")}, _h_js_compare)
    _add("rea_js_recover",
         "Recover readable JavaScript modules from minified/bundled input with provenance (requires caller-"
         "supplied wakaru).",
         {"path": _d("Absolute path to the bundled/minified input"),
          "output_directory": _d("Absent output directory for recovered sources"),
          "additional_paths": _d("Optional related runtime/shared/lazy files"),
          "extraction_mode": _d("Optional extraction mode"),
          "rewrite_level": _d("Optional rewrite level")}, _h_js_recover)

    # artifacts / native / process / evidence
    _add("rea_artifact",
         "Inspect one artifact with evidence: inventory + digests (zip/asar/package), extract contents, PE "
         "resources, linked binary layout, managed/.NET identity and members, dylib resolution, plists, or a "
         "selected view of previously completed evidence.",
         {"path": _d("Absolute path to the artifact"),
          "action": _d("inspect|extract|pe-resources|layout|managed|managed-members|dylib|plist|view"),
          "integrity_policy": _d("fail|warn|ignore")}, _h_artifact)
    _add("rea_compare",
         "Evidence-grade comparison of two inputs: artifact inventories, bundles, process captures, application "
         "versions, export shapes, managed members, WASM interfaces, web captures, screenshots.",
         {"kind": _d("artifacts|bundles|processes|application-versions|export-shapes|managed-members|wasm|"
                     "web-captures|screenshots"),
          "left": _d("Absolute path to the first/before input"),
          "right": _d("Absolute path to the second/after input"),
          "trace_spec": _d("Optional trace-specification JSON (kind=processes)")}, _h_compare)
    _add("rea_crash",
         "Inspect a recorded Linux crash (core file): every thread's registers and signals, raw notes, original "
         "file ranges, and optional debugger mapping candidates.",
         {"path": _d("Absolute path to a core dump"),
          "debugger_context": _d("Add mapping candidates via gdb/pwndbg when available")}, _h_crash)
    _add("rea_native",
         "Native binary operations through the best available provider (Ghidra -> Rizin -> objdump): overview, "
         "decompile, function dossier, instructions, xrefs, string/procedure search, feature trace, native API "
         "reconstruction. Packed (Themida) targets are explicitly not decompilable — expect layout/strings only.",
         {"path": _d("Absolute path to the binary"),
          "action": _d("overview|inspect|decompile|function|instructions|xrefs|search|trace|api|strings"),
          "selector": _d("Function/procedure name or literal feature to trace"),
          "provider": _d("Optional provider id to force (ghidra|rizin|objdump|ida|hopper)"),
          "snapshot": _d("Optional analysis snapshot path")}, _h_native)
    _add("rea_process_capture",
         "Capture one bounded process scenario (terminal output, interactions, exit status, filesystem "
         "observations) — the way to PROVE a CLI/script behaves, not assume it.",
         {"executable": _d("Executable to run (or pass a full spec instead)"),
          "arguments": _d("Arguments (list or shell-style string)"),
          "working_directory": _d("Optional absolute working directory"),
          "timeout_ms": _d("Scenario timeout ms (default 5000)"),
          "filesystem_observation_paths": _d("Optional paths whose files are digested before/after"),
          "limits": _d("Optional limits object {output_bytes, file_bytes}"),
          "spec": _d("Full scenario JSON object (overrides the fields above)")}, _h_process_capture)
    _add("rea_evidence",
         "Work with the plugin's persisted Evidence store: list what this host has analysed, read one envelope, "
         "export/import canonical evidence bundles, inspect a bundle's discovery metadata, prune, or audit "
         "residual unknowns.",
         {"action": _d("list|get|export|import|inspect|prune|unknowns"),
          "evidence_id": _d("Evidence id (action=get)"),
          "source": _d("Evidence source reference (action=export)"),
          "output": _d("Output path (action=export)"),
          "overwrite": _d("Allow overwriting the output (action=export)"),
          "path": _d("Bundle/evidence path (action=import|inspect)"),
          "spec": _d("Workflow JSON (action=unknowns)"),
          "limit": _d("Max rows (action=list)")}, _h_evidence)
    _add("rea_firmware",
         "Firmware analysis (binwalk/unblob): signature candidates without extraction, or bounded extraction "
         "with verified files and provenance.",
         {"path": _d("Absolute path to a firmware image"),
          "action": _d("regions|extract"),
          "output_directory": _d("Output directory (action=extract)"),
          "offset": _d("Optional byte offset"), "length": _d("Optional byte length"),
          "max_depth": _d("Optional extraction depth")}, _h_firmware)
    _add("rea_sqlite",
         "Inspect a SQLite database snapshot (private copy; schema, indexes, triggers, views, selected rows) — "
         "ideal for verifying delivered .sqlite artifacts. Requires a build that ships SQLite inspection.",
         {"path": _d("Absolute path to the database (a snapshot, not a live file)"),
          "table": _d("Optional table to expand with sample rows"),
          "column": _d("Optional column selection"),
          "schema_scope": _d("Optional scope (database for the complete schema)"),
          "row_limit": _d("Optional sample row limit (default 20)")}, _h_sqlite)
    _add("rea_binary_meta",
         "Build/dependency metadata and offline native inspection: Go build settings and modules, Rust "
         "cargo-auditable crates, objdump operations, Rizin commands, or string/procedure search.",
         {"path": _d("Absolute path to an ELF/PE/Mach-O"),
          "kind": _d("auto|go|rust|objdump|rizin|strings"),
          "operation": _d("objdump operation (kind=objdump)"),
          "command": _d("Rizin command (kind=rizin)"),
          "query": _d("Search literal (kind=strings)"),
          "follow_debug_links": _d("Follow debug links (kind=objdump)")}, _h_binary_meta)
    _add("rea_web",
         "Browser/Electron observation through an existing loopback CDP endpoint: list targets, passively inspect "
         "a page (DOM/accessibility/console/scripts), observe a session, screenshot, analyse a bundle, inspect "
         "retained HAR/mitmproxy records.",
         {"action": _d("targets|page|session|screenshot|bundle|electron|runtime|network|electron-targets|"
                       "runtime-targets"),
          "endpoint": _d("Literal-port CDP endpoint, e.g. http://127.0.0.1:9222"),
          "target_id": _d("Target id from action=targets"),
          "allowed_origins": _d("Optional origin filter (list or comma string)"),
          "observation_ms": _d("Observation window ms"),
          "include_accessibility_text": _d("Include accessibility text"),
          "include_console_text": _d("Include console text"),
          "include_script_sources": _d("Include script sources"),
          "include_storage_keys": _d("Include storage key names"),
          "fetch_source_maps": _d("Fetch source maps (action=bundle)"),
          "capture_path": _d("Capture file path (action=network)"),
          "format": _d("Capture format (action=network): har|saz"),
          "record": _d("Record mode flag (action=network)")}, _h_web)

    # composites + MCP
    _add("rea_mobile_audit",
         "One evidence-chained real-device audit: device identity, display, package info, processes, logcat, "
         "screen capture (and optional bugreport) with every evidence id collected and stored. This is the "
         "default proof step for 'verify it on the real phone' work.",
         {"serial": _d("Device serial"),
          "package": _d("Optional package under test"),
          "logcat_count": _d("Max log lines (default 300)"),
          "buffer": _d("Log buffer (e.g. crash)"),
          "output_directory": _d("Optional BASE directory for the screen PNG; each run writes a fresh "
                                 "timestamped subdirectory (REA refuses to overwrite a capture)"),
          "bugreport": _d("Also collect a full bugreport (slow)")}, _h_mobile_audit)
    _add("rea_verify_deploy",
         "Prove what shipped: sha256 + size for each artifact, plus an inventory or source-vs-bundle comparison "
         "of the deployed bundle.",
         {"artifacts": _d("List (or comma string) of absolute artifact paths to digest"),
          "bundle": _d("Optional deployed bundle path to inventory/compare"),
          "source": _d("Optional source tree to compare against the bundle"),
          "extra": _d("Optional extra workflow fields merged into the comparison spec")}, _h_verify_deploy)
    _add("rea_mcp_setup",
         "Register the REA MCP server with Hermes (wrapper script that injects the engine env, then "
         "`hermes mcp add`) and install/refresh REA's upstream skill. Pass apply=true to execute; a config "
         "backup is taken by REA's own setup.",
         {"apply": _d("Execute the registration (false = print the plan only)"),
          "install_skill": _d("Also run `rea setup --client hermes` to install the skill (default true)"),
          "tune": _d("Also ensure the config entry carries the engine env + long timeouts "
                     "(default true; validated with `hermes mcp list` / `hermes config check`, "
                     "rolled back on failure)")},
         _h_mcp_setup)
    _add("rea_mcp_status",
         "Report whether ~/.hermes/config.yaml carries the REA MCP registration, the wrapper path, and whether "
         "the full (main-branch) command surface is available.",
         {}, _h_mcp_status)

    # declarative single-command tools
    for entry in _tool_table():
        _add(entry["name"], entry["description"], entry["params"], entry["handler"])

    # commands
    ctx.register_command(name="/rea", description="REA: status, engines, command manifest, evidence, MCP setup",
                         handler=_cmd_rea, args_hint="[status|engines|commands <q>|evidence|mcp]")
    ctx.register_command(name="/rea-doctor", description="Run REA's own doctor",
                         handler=_cmd_rea_doctor)
    ctx.register_command(name="/rea-engines", description="REA analysis-engine readiness + remediation",
                         handler=_cmd_rea_engines)
    ctx.register_command(name="/rea-setup", description="Register REA's MCP server with Hermes + install its skill",
                         handler=_cmd_rea_setup)
    ctx.register_command(name="/rea-commands", description="Search REA's command manifest",
                         handler=_cmd_rea_commands, args_hint="[query]")

    logger.info(
        "hermes-rea: %d tools registered — evidence-backed reverse engineering (native, Android/APK/ADB, "
        "JS/Electron, artifacts, process capture, firmware, pcap). launcher=%s, engines ready=%d/%d",
        count, _launcher_label(), sum(1 for r in _engine_report() if r["ready"]), len(ENGINE_SPECS))
    return {"name": "hermes-rea", "version": "1.0.0", "tools": count, "toolset": toolset}


# ── slash commands ────────────────────────────────────────────────────────────

def _cmd_rea(args: str, ctx: Any = None) -> str:
    try:
        parts = shlex.split(args or "")
        sub = parts[0].lower() if parts else "status"
        if sub in {"status", "st"}:
            return _h_status({})
        if sub in {"engines", "eng", "providers"}:
            return _h_engines({})
        if sub in {"commands", "cmds"}:
            return _h_commands({"query": " ".join(parts[1:]), "limit": 60})
        if sub in {"evidence", "ev"}:
            return _h_evidence({"action": "list", "limit": 20})
        if sub in {"mcp", "mcp-status"}:
            return _h_mcp_status({})
        if sub in {"mcp-setup", "setup-mcp"}:
            return _h_mcp_setup({"apply": False})
        if sub in {"doctor", "doc"}:
            return _h_doctor({"client": "hermes"})
        return ("REA\n"
                "  /rea status            readiness, launcher, engines, evidence store\n"
                "  /rea engines           per-engine readiness + remediation\n"
                "  /rea commands <query>  search REA's command manifest\n"
                "  /rea evidence          stored evidence ids\n"
                "  /rea mcp               MCP registration state\n"
                "  /rea mcp-setup         plan the MCP registration\n"
                "  /rea doctor            REA's own doctor\n"
                "tools: rea_status rea_run rea_adb_* rea_apk rea_js_* rea_artifact rea_compare rea_native "
                "rea_crash rea_process_capture rea_evidence rea_sqlite rea_firmware rea_web rea_mobile_audit "
                "rea_verify_deploy")
    except Exception as exc:
        return f"rea: {exc}"


def _cmd_rea_doctor(args: str, ctx: Any = None) -> str:
    return _h_doctor({"client": (args or "hermes").strip().split()[0] if args.strip() else "hermes"})


def _cmd_rea_engines(args: str, ctx: Any = None) -> str:
    return _h_engines({})


def _cmd_rea_setup(args: str, ctx: Any = None) -> str:
    apply = any(tok in {"apply", "--apply", "-y", "yes"} for tok in shlex.split(args or ""))
    return _h_mcp_setup({"apply": apply, "install_skill": True})


def _cmd_rea_commands(args: str, ctx: Any = None) -> str:
    return _h_commands({"query": (args or "").strip(), "limit": 60})


# ── JIT dependency (module level, before any REA use) ─────────────────────────

if ensure_deps is not None and DepSpec is not None and REA_AUTO_INSTALL:
    try:
        _REA_DEPS = [
            DepSpec(
                "rea-agents",
                # Probe the way the plugin uses it: a launcher that answers --version.
                # Never probe an npm global with require() (fails on a working install).
                [(_which("rea") or (_which("npx") or "npx")), "--version"]
                if not Path(REA_ROOT).expanduser().joinpath("scripts", "rea.mjs").is_file()
                else [_node_exe(), str(Path(REA_ROOT).expanduser() / "scripts" / "rea.mjs"), "--version"],
                install=[_which("npm") or "npm", "install", "-g", f"rea-agents@{REA_PIN}"],
                purpose="reverse-engineering CLI + MCP server (REA)",
            )
        ]
        ensure_deps("hermes-rea", _REA_DEPS, ask=False)
    except Exception as exc:  # pragma: no cover
        logger.warning("hermes-rea: dependency bootstrap skipped (%s)", exc)
