"""
hermes-dsh — DeepSeek Harness integration for Hermes.

Drives the published DeepSeek Harness CLI (``@deepseek-ai/dsh``) as an
independent, event-sourced agent executor and introspects its durable SQLite
session store from Hermes.

Capabilities surfaced (each maps to a real deepseek-harness feature, verified
against the upstream source):

  * ``dsh_run``            — run a one-shot headless task through dsh's `headless`
                             bundle (its own agent loop, its own persisted
                             event-sourced Agent) and return the final answer
                             plus the new event-sourced session id.
  * ``dsh_sessions``       — list the durable session store (SQLite schema v15:
                             `sessions` + `events`, append-only, seq-monotonic).
  * ``dsh_session_events`` — read ONE session's raw durable event log
                             (seq/type/time/data) — the replay-grade source of
                             truth dsh derives model history from.
  * ``dsh_session_export`` — dump a session's full raw event log as JSONL for
                             replay / fork / audit.
  * ``dsh_lineage``        — traverse session fork genealogy from
                             `sessions.parent_session` / `origin` / `delegation_depth`.
  * ``dsh_status``         — health: node version, dsh bin resolution, DSH_HOME,
                             DeepSeek key presence, install state.
  * ``dsh_bootstrap``      — ensure dsh is installed (idempotent, self-healing
                             managed `npm install`).

Integration decision (verified 2026-08-17): Node >= 22.19, DEEPSEEK_API_KEY
present, but pnpm is broken and `deepseek-harness-sdk` is not on PyPI — so the
published npm CLI plus on-disk read-only SQLite introspection is the maximum
practical, production-usable utilization surface.

Self-contained (stdlib only): survives Hermes updates. Machine-agnostic:
honors $DSH_BIN and $DSH_HOME overrides; no hardcoded machine paths.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time as _time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── config ──────────────────────────────────────────────────────────────
_DSH_NPM_VERSION = "0.1.0-rc.6"          # pinned published package
_NODE_MIN_MAJOR = 22                      # engines: ^22.19 || >=24
_DEFAULT_RUN_TIMEOUT = 300                # dsh_run default, clamped [10,1800]
_MAX_RUN_TIMEOUT = 1800
_DEFAULT_EVENT_LIMIT = 120                # dsh_session_events default
_MAX_EVENT_LIMIT = 2000
_DEFAULT_EXPORT_LIMIT = 500               # dsh_session_export default
_MAX_EXPORT_LIMIT = 10000
_NPM_INSTALL_TIMEOUT = 1200               # bootstrap npm install
_VERSION_PROBE_TIMEOUT = 60               # `dsh --version` after install


def _env_int(key: str, default: int) -> int:
    """Garbage-safe env int. Never raises (crash-on-import guard)."""
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _to_int(raw: Any, default: int) -> int:
    """Coerce an arg to int, falling back to default on garbage. Never raises."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# module-level tunables (all env-overridable, all safe via _env_int)
_RUN_TIMEOUT_DEFAULT = _env_int("DSH_RUN_TIMEOUT", _DEFAULT_RUN_TIMEOUT)
_STATE_ROOT = Path(os.environ.get("DSH_STATE_ROOT", str(Path.home() / ".hermes" / "dsh")))
_DSH_BIN_OVERRIDE = os.environ.get("DSH_BIN", "").strip()


def _default_dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))


def _managed_bin() -> Path:
    """The dsh launcher JS entry under the plugin-managed install."""
    return _STATE_ROOT / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"


def _install_marker() -> Path:
    return _STATE_ROOT / ".installed"


# ── bootstrap / resolution ─────────────────────────────────────────────
# RLock (reentrant): guards bootstrap + the launch window of dsh_run.
# _active_runs counts in-flight dsh subprocesses — a force-bootstrap must
# NOT rmtree node_modules under a running dsh (it would fail "Cannot find
# module" mid-flight); it skips the clean and reinstalls over the old tree.
_boot_lock: threading.RLock = threading.RLock()
_active_runs: int = 0


def _node_version() -> Tuple[Optional[int], Optional[str]]:
    """Return (major, full_version) or (None, error-string)."""
    try:
        r = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=15
        )
    except FileNotFoundError:
        return None, "Node.js not found — required >=22 for DeepSeek Harness"
    except subprocess.TimeoutExpired:
        return None, "node --version timed out"
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "").strip()[:200]
    ver = (r.stdout or "").strip().lstrip("v")
    try:
        return int(ver.split(".")[0]), ver
    except (ValueError, IndexError):
        return None, f"unparseable node version {ver!r}"


def _node_ok() -> Tuple[bool, str]:
    major, detail = _node_version()
    if major is None:
        return False, detail or "unknown node error"
    if major < _NODE_MIN_MAJOR:
        return False, f"Node {major} < {_NODE_MIN_MAJOR} (>=22.19 required)"
    return True, f"{major}.x (node {detail})"


def _read_hermes_env() -> Dict[str, str]:
    """Best-effort parse ~/.hermes/.env into a dict (never raises)."""
    out: Dict[str, str] = {}
    env = Path(os.environ.get("HERMES_ENV_PATH", str(Path.home() / ".hermes" / ".env")))
    try:
        text = env.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _resolve_llm_env() -> Dict[str, str]:
    """Effective LLM env for dsh subprocesses.

    Precedence: DSH_API_KEY/DSH_BASE_URL/DSH_MODEL (os.environ) →
    DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL (os.environ) → ~/.hermes/.env.
    DSH_* overrides let a deployment point dsh at a DeepSeek-compatible
    gateway (e.g. a local omniRoute/LiteLLM) without touching Hermes' own
    DeepSeek credentials.
    """
    file_env = _read_hermes_env()
    env: Dict[str, str] = {}
    key = (os.environ.get("DSH_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
           or file_env.get("DEEPSEEK_API_KEY") or file_env.get("DSH_API_KEY"))
    if key:
        env["DEEPSEEK_API_KEY"] = key
    base = (os.environ.get("DSH_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL")
            or file_env.get("DEEPSEEK_BASE_URL") or file_env.get("DSH_BASE_URL"))
    if base:
        env["DEEPSEEK_BASE_URL"] = base
    return env


def _resolve_model() -> Optional[str]:
    """Effective model override (DSH_MODEL env or .env), else None."""
    file_env = _read_hermes_env()
    return (os.environ.get("DSH_MODEL") or file_env.get("DSH_MODEL") or "").strip() or None


def _resolve_disable_proxy() -> bool:
    """True iff the SSE proxy must NOT be started (DSH_DISABLE_PROXY=1).

    Mirrors _resolve_model: honored from os.environ OR the .env file, so a
    deployment can disable the proxy (e.g. against a spec-compliant gateway)
    from its env file without touching the process environment.
    """
    file_env = _read_hermes_env()
    val = os.environ.get("DSH_DISABLE_PROXY") or file_env.get("DSH_DISABLE_PROXY") or ""
    return val.strip().lower() in ("1", "true", "yes", "on")


def _model_patch_file(model: str) -> Optional[Path]:
    """Write a cordis `--patch` overlay replacing agent-default-model's model.

    deepseek-harness composes its boot tree from patch layers; a row targeted
    by id replaces that row's whole config. dsh's base bundle already uses
    provider `deepseek-official`, so only the model id needs replacing (e.g.
    `combo/deepseek-v4-flash` on a gateway that does not route bare ids).

    ALSO caps the llm-deepseek provider's output budget: dsh's default
    maxTokens is 256_000, and gateways mishandle that shape (reasoning burns
    the whole budget → "EMPTY_RESPONSE: completed response with no content").
    A sane cap (DSH_MAX_TOKENS, default 4096) makes the gateway return real
    content. Returns the patch path; caller removes it after the run.
    """
    import tempfile
    try:
        fd, name = tempfile.mkstemp(prefix="dsh-model-", suffix=".yml")
        # Clamp to a value the provider schema accepts (min 1): a user-set
        # DSH_MAX_TOKENS of 0 or negative would produce an invalid patch and
        # a cryptic dsh config error instead of a clean run.
        max_tokens = max(1, min(_env_int("DSH_MAX_TOKENS", 4096), 1_000_000))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("- id: agent-default-model\n"
                    "  config:\n"
                    "    provider: deepseek-official\n"
                    f"    model: {model}\n"
                    "- id: llm-deepseek\n"
                    "  config:\n"
                    f"    maxTokens: {max_tokens}\n")
        return Path(name)
    except OSError as e:
        logger.warning("hermes-dsh: cannot write model patch: %s", e)
        return None


def _resolve_bin() -> Tuple[Optional[List[str]], Dict[str, Any]]:
    """Resolve the dsh launcher command list, without installing.

    Priority: $DSH_BIN override → plugin-managed install → None.
    """
    if _DSH_BIN_OVERRIDE:
        p = Path(_DSH_BIN_OVERRIDE)
        if p.exists() and os.access(p, os.X_OK):
            return [str(p)], {"source": "DSH_BIN", "bin": str(p)}
        return None, {"source": "DSH_BIN", "error": f"DSH_BIN not executable: {p}"}

    ok, node_detail = _node_ok()
    if not ok:
        return None, {"source": "node", "error": node_detail}

    managed = _managed_bin()
    if managed.exists():
        return ["node", str(managed)], {"source": "managed", "bin": str(managed)}
    return None, {"source": "managed", "error": "dsh not installed"}


def _probe_installed(managed: Path) -> bool:
    """True iff the managed dsh launcher actually boots (--version succeeds).

    A stale marker + truncated/garbage bin must NOT be treated as installed.
    Uses the same version-probe shape as the bootstrap handler.
    """
    try:
        r = subprocess.run(
            ["node", str(managed), "--version"],
            capture_output=True, text=True, timeout=_VERSION_PROBE_TIMEOUT,
        )
        return r.returncode == 0 and bool((r.stdout or r.stderr or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _do_install(force: bool = False) -> Tuple[Optional[List[str]], Dict[str, Any]]:
    """Idempotent, self-healing managed npm install of @deepseek-ai/dsh."""
    with _boot_lock:
        try:
            _STATE_ROOT.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return None, {"error": f"cannot create state root {_STATE_ROOT}: {e}"}

        managed = _managed_bin()
        needs_reinstall = False
        if not force and managed.exists() and _install_marker().exists():
            # Idempotency must PROVE the existing install boots — a truncated
            # or garbage bin with a stale marker would otherwise be handed
            # back as "already installed" and fail cryptically at run time.
            if _probe_installed(managed):
                return ["node", str(managed)], {"source": "managed",
                                                "bin": str(managed),
                                                "already": True}
            logger.warning("hermes-dsh: existing install fails to boot "
                           "(%s) — reinstalling", managed)
            needs_reinstall = True

        ok, node_detail = _node_ok()
        if not ok:
            return None, {"error": node_detail}
        if not os.environ.get("DEEPSEEK_API_KEY"):
            logger.warning("dsh bootstrap: DEEPSEEK_API_KEY not set in os.environ")

        # A broken/partial install must be cleaned BEFORE reinstall: npm sees
        # the package as already present and does nothing, leaving the corrupt
        # bin in place. Remove just the package + marker (never the whole root
        # — it may hold user data). SKIP the rmtree while dsh runs are
        # in-flight (a running dsh subprocess would fail "Cannot find module"
        # mid-flight); npm install over the old tree still repairs it.
        if (force or needs_reinstall) and _active_runs <= 0:
            try:
                pkgdir = _STATE_ROOT / "node_modules" / "@deepseek-ai" / "dsh"
                if pkgdir.exists():
                    import shutil
                    shutil.rmtree(str(pkgdir))
                marker = _install_marker()
                if marker.exists():
                    marker.unlink()
            except OSError as e:
                return None, {"error": f"clean failed: {e}"}
        elif force or needs_reinstall:
            logger.warning("hermes-dsh: %d dsh run(s) in flight — skipping "
                           "node_modules rmtree; npm will repair in place",
                           _active_runs)

        # Give the state dir its own package.json so `npm install` installs
        # HERE and does NOT climb to the nearest ancestor project (the user's
        # home/app package.json) — otherwise npm pollutes that project. This is
        # the machine-agnostic guard.
        try:
            pkg = _STATE_ROOT / "package.json"
            if not pkg.exists():
                pkg.write_text(json.dumps({
                    "name": "hermes-dsh-runtime",
                    "private": True,
                    "version": "0.0.0",
                }), encoding="utf-8")
        except OSError as e:
            return None, {"error": f"cannot write state package.json: {e}"}

        cmd = ["npm", "install", f"@deepseek-ai/dsh@{_DSH_NPM_VERSION}",
               "--no-audit", "--no-fund", "--no-save"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=_NPM_INSTALL_TIMEOUT, cwd=str(_STATE_ROOT))
        except FileNotFoundError:
            return None, {"error": "npm not found on PATH — install Node.js with npm"}
        except subprocess.TimeoutExpired:
            return None, {"error": f"npm install exceeded {_NPM_INSTALL_TIMEOUT}s timeout"}

        if r.returncode != 0 or not (_STATE_ROOT / "node_modules").exists():
            tail = (r.stderr or r.stdout or "").strip()[-800:]
            return None, {"error": f"npm install failed (exit {r.returncode}): {tail}"}
        if not _managed_bin().exists():
            tail = (r.stderr or r.stdout or "").strip()[-800:]
            return None, {"error": f"dsh launcher bin missing after install: {tail}"}

        # Self-heal: a leftover marker from a partial run is fine to refresh.
        try:
            _install_marker().write_text(_DSH_NPM_VERSION, encoding="utf-8")
        except OSError:
            pass
        logger.info("hermes-dsh: installed @deepseek-ai/dsh@%s into %s",
                    _DSH_NPM_VERSION, _STATE_ROOT)
        return ["node", str(_managed_bin())], {"source": "managed",
                                               "bin": str(_managed_bin()),
                                               "installed": True}


def _build_env(dsh_home: Path) -> Dict[str, str]:
    """Subprocess env: os.environ + resolved LLM creds + DSH_HOME.

    If the effective DeepSeek base URL is a local gateway (the omniRoute /
    LiteLLM class of servers that append keepalive SSE events), start the SSE
    normalization proxy and point dsh at it so streams always terminate with a
    clean `[DONE]` (fixes dsh STREAM_CLOSED).
    """
    env = dict(os.environ)
    llm = _resolve_llm_env()
    env.update(llm)
    base = llm.get("DEEPSEEK_BASE_URL", "")
    if base and not _resolve_disable_proxy():
        if "127.0.0.1" in base or "localhost" in base or "192.168." in base:
            proxy_base, err = _ensure_proxy(base)
            if err is None and proxy_base:
                env["DEEPSEEK_BASE_URL"] = proxy_base
            elif err:
                logger.warning("hermes-dsh: SSE proxy unavailable (%s) — "
                               "running without it; dsh may see STREAM_CLOSED",
                               err)
    env["DSH_HOME"] = str(dsh_home)
    return env


# ── session-store introspection (JSONL+zstd default, SQLite fallback) ─
def _find_session_store(dsh_home: Path) -> Optional[Dict[str, Any]]:
    """Locate a dsh durable session store.

    The dsh-base bundle persists with the JSONL backend by default
    (`$DSH_HOME/sessions/<workspace-slug>/session-<uuid>/session.jsonl[.zstd]`,
    zstd frames: first frame = header line, then one event JSON per line). A
    deployment may configure the SQLite backend instead (`*.db` with a
    `sessions` table). Return a store descriptor or None.
    """
    if dsh_home.exists():
        root = dsh_home / "sessions"
        if root.is_dir():
            artifacts = sorted(root.glob("*/session-*/session.jsonl*"))
            if artifacts:
                return {"kind": "jsonl", "root": str(root)}
        db = _find_sqlite_db(dsh_home)
        if db is not None:
            return {"kind": "sqlite", "path": str(db)}
    return None


def _find_sqlite_db(dsh_home: Path) -> Optional[Path]:
    """Locate a SQLite session-persistence store (schema v15, `sessions`)."""
    if not dsh_home.is_dir():
        return None
    candidates: List[Path] = []
    try:
        for p in dsh_home.iterdir():
            if p.is_file() and p.suffix == ".db":
                candidates.append(p)
    except OSError:
        return None
    for child in [dsh_home / "data", dsh_home / "store"]:
        if child.is_dir():
            for p in child.iterdir():
                if p.is_file() and p.suffix == ".db":
                    candidates.append(p)
    for db in candidates:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions'"
                ).fetchone()
                if row and row[0] == 1:
                    return db
            finally:
                con.close()
        except sqlite3.Error:
            continue
    return None


def _store_or_error(dsh_home: Path) -> Any:
    """Return a store descriptor or a dict error (never null error keys)."""
    store = _find_session_store(dsh_home)
    if store is None:
        return {"message": "No dsh session store found under "
                           f"{dsh_home} — run dsh_run once to create one"}
    return store


def _store_session_ids(store: Dict[str, Any]) -> set:
    """All stored session ids (either backend)."""
    if store["kind"] == "jsonl":
        ids = set()
        for path in _jsonl_artifacts(store):
            lines = _decode_session_log(path)
            if not lines:
                continue
            header = _parse_session_header(lines[0])
            if header and header.get("id"):
                ids.add(header["id"])
        return ids
    try:
        con = sqlite3.connect(f"file:{store['path']}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT id FROM sessions").fetchall()
            return {str(r[0]) for r in rows}
        finally:
            con.close()
    except sqlite3.Error:
        return set()


def _store_session_list(store: Dict[str, Any], limit: int = 100) -> List[Dict[str, Any]]:
    """Unified newest-first session listing for either backend."""
    if store["kind"] == "jsonl":
        return _jsonl_session_rows(store, limit)
    try:
        con = sqlite3.connect(f"file:{store['path']}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                """
                SELECT s.id, s.version, s.created_at, s.parent_session,
                       s.origin, s.delegation_depth, s.agent_preset, s.revision,
                       s.cwd,
                       (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id) AS events,
                       (SELECT MAX(e.seq) FROM events e WHERE e.session_id=s.id) AS last_seq
                FROM sessions s ORDER BY s.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "id": r[0], "version": r[1], "created_at": r[2],
                    "parent_session": r[3], "origin": r[4],
                    "delegation_depth": r[5], "agent_preset": r[6],
                    "revision": r[7], "cwd": r[8],
                    "event_count": r[9], "last_seq": r[10],
                }
                for r in rows
            ]
        finally:
            con.close()
    except sqlite3.Error as e:
        return []


_ZSTD_LIB_IMPORTED = None  # lazily imported python-zstandard module, or None


def _zstd_lib_decode(data: bytes) -> Optional[str]:
    """Decode zstd bytes via python-zstandard if importable (CLI fallback).

    dsh writes MULTI-FRAME zstd (one frame per event), so a single-frame
    `decompress()` truncates; `stream_reader` handles the frame sequence.
    """
    global _ZSTD_LIB_IMPORTED
    try:
        if _ZSTD_LIB_IMPORTED is None:
            import zstandard
            _ZSTD_LIB_IMPORTED = zstandard
        if _ZSTD_LIB_IMPORTED is not None:
            dctx = _ZSTD_LIB_IMPORTED.ZstdDecompressor()
            with dctx.stream_reader(
                    __import__("io").BytesIO(data),
                    read_across_frames=True) as r:
                return r.read(256 * 1024 * 1024).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        _ZSTD_LIB_IMPORTED = False
    return None


def _decode_session_log(path: Path) -> List[str]:
    """Return the decoded lines of a session log (zstd or plain JSONL).

    zstd frames are decoded via the `zstd` CLI when present, falling back to
    python-zstandard if importable — session introspection must not silently
    depend on a system binary. Plain `.jsonl` and corrupt/missing files return
    [] gracefully.
    """
    try:
        if path.name.endswith(".zstd"):
            raw = None
            try:
                raw = path.read_bytes()
            except OSError:
                return []
            text = None
            if raw:
                text = _zstd_lib_decode(raw)
            if text is None:
                try:
                    r = subprocess.run(["zstd", "-d", "-c", str(path)],
                                       capture_output=True, text=True, timeout=60)
                    if r.returncode == 0:
                        text = r.stdout
                except (OSError, subprocess.TimeoutExpired):
                    text = None
            if text is None:
                return []
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()


def _parse_session_header(line: str) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("type") != "session":
        return None
    return d


def _jsonl_artifacts(store: Dict[str, Any]) -> List[Path]:
    return sorted(Path(store["root"]).glob("*/session-*/session.jsonl*"))


def _jsonl_session_rows(store: Dict[str, Any], limit: int = 100) -> List[Dict[str, Any]]:
    """List JSONL sessions newest-first with header metadata + event count."""
    rows: List[Dict[str, Any]] = []
    for path in _jsonl_artifacts(store):
        lines = _decode_session_log(path)
        if not lines:
            continue
        header = _parse_session_header(lines[0])
        if header is None:
            continue
        sid = header.get("id") or path.parent.name
        rows.append({
            "id": sid,
            "version": header.get("version"),
            "created_at": header.get("createdAt"),
            "cwd": header.get("cwd"),
            "parent_session": header.get("parentSession"),
            "seed_length": header.get("seedLength"),
            "origin": header.get("origin"),
            "delegation_depth": header.get("delegationDepth"),
            "agent_preset": header.get("agentPreset"),
            "event_count": max(0, len(lines) - 1),
            "last_seq": max(0, len(lines) - 2) if len(lines) > 1 else 0,
            "path": str(path),
        })
    rows.sort(key=lambda r: (r.get("created_at") or 0), reverse=True)
    return rows[:limit]


def _jsonl_session_events(store: Dict[str, Any], session_id: str,
                          limit: int, offset: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (header, events[seq,type,time,data]) for one JSONL session."""
    for path in _jsonl_artifacts(store):
        if path.parent.name != session_id:
            continue
        lines = _decode_session_log(path)
        if not lines:
            return None, []
        header = _parse_session_header(lines[0])
        events: List[Dict[str, Any]] = []
        for line in lines[1:]:
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(ev, dict):
                continue
            events.append({
                "seq": ev.get("seq"),
                "type": ev.get("type"),
                "time": ev.get("time"),
                "data": _trunc(ev.get("data"), 600),
            })
        return header, events[offset:offset + limit]
    return None, []


def _jsonl_session_export(store: Dict[str, Any], session_id: str,
                          max_events: int, full: bool) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Return (header, exported JSONL lines) for one JSONL session."""
    for path in _jsonl_artifacts(store):
        if path.parent.name != session_id:
            continue
        lines = _decode_session_log(path)
        if not lines:
            return None, []
        header = _parse_session_header(lines[0])
        out: List[str] = []
        for line in lines[1:max_events + 1]:
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                out.append(line)
                continue
            rec: Dict[str, Any] = {"seq": ev.get("seq"), "type": ev.get("type"),
                                   "time": ev.get("time")}
            rec["data"] = ev.get("data") if full else _trunc(ev.get("data"), 400)
            if ev.get("sourceEventSeqs"):
                rec["source_event_seqs"] = ev["sourceEventSeqs"]
            out.append(json.dumps(rec, default=str))
        return header, out
    return None, []


def _jsonl_lineage(store: Dict[str, Any], session_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (ancestors, children) for a JSONL session via header parentSession."""
    headers: Dict[str, Dict[str, Any]] = {}
    for path in _jsonl_artifacts(store):
        lines = _decode_session_log(path)
        if not lines:
            continue
        header = _parse_session_header(lines[0])
        if header is None or not header.get("id"):
            continue
        headers[header["id"]] = header
    ancestors: List[Dict[str, Any]] = []
    seen = {session_id}
    cur = session_id
    while True:
        h = headers.get(cur)
        if h is None or not h.get("parentSession"):
            break
        pid = h["parentSession"]
        if pid in seen:
            break
        seen.add(pid)
        ph = headers.get(pid, {})
        ancestors.append({"id": pid, "origin": ph.get("origin"),
                          "delegation_depth": ph.get("delegationDepth"),
                          "created_at": ph.get("createdAt")})
        cur = pid
    children = [
        {"id": sid, "origin": h.get("origin"),
         "delegation_depth": h.get("delegationDepth"),
         "created_at": h.get("createdAt")}
        for sid, h in headers.items()
        if h.get("parentSession") == session_id
    ]
    children.sort(key=lambda c: c.get("created_at") or 0, reverse=True)
    return ancestors, children


# ── SSE normalization proxy (omniRoute keepalive workaround) ─────────
_proxy_lock = threading.Lock()
_proxy_proc: Optional[subprocess.Popen] = None
_proxy_port: Optional[int] = None
_proxy_upstream: str = ""
_proxy_owned: bool = False  # True iff THIS process spawned the proxy (and so
                            # may terminate it + clear the lockfile)


def _sse_proxy_script() -> Path:
    return Path(__file__).resolve().parent / "sse_proxy.py"


def _proxy_lockfile(upstream: Optional[str] = None) -> Path:
    """Lockfile recording the live proxy's port/upstream/pid.

    Lives under the plugin state root so a fresh process can find and reuse
    a proxy orphaned by a hard parent exit (os._exit / SIGKILL skips atexit).
    Keyed by upstream hash so DIFFERENT upstreams never contend on one lock
    (a global lock would let one upstream's proxy block another's spawn).
    """
    try:
        _STATE_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if upstream:
        import hashlib
        h = hashlib.sha256(upstream.encode("utf-8")).hexdigest()[:16]
        return _STATE_ROOT / f"sse_proxy.{h}.lock"
    return _STATE_ROOT / "sse_proxy.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_proxy_lock(port: int, upstream: str, pid: int) -> None:
    try:
        _proxy_lockfile(upstream).write_text(
            json.dumps({"port": port, "upstream": upstream, "pid": pid}),
            encoding="utf-8")
    except OSError:
        pass


def _clear_proxy_lock(upstream: Optional[str] = None) -> None:
    try:
        _proxy_lockfile(upstream).unlink(missing_ok=True)
    except OSError:
        pass


def _acquire_proxy_lock(lock: Path) -> bool:
    """Atomically acquire the proxy lockfile (O_EXCL cross-process guard).

    Only ONE process may hold the lock; a loser returns False and must REUSE
    the winner's proxy rather than spawn a duplicate. The winner writes the
    real lock (port/upstream/pid) after spawning. A STALE lock (dead pid) is
    broken and re-acquired; an EMPTY lock is the winner's in-progress state
    and must NEVER be broken (that would defeat the O_EXCL guard).
    """
    try:
        # Break a stale lock only: a populated lock whose pid is dead (the
        # owner crashed), or an EMPTY lock older than 30s (the winner crashed
        # before populating it — otherwise a permanently-empty lock would
        # wedge everyone forever). A fresh empty lock is the winner's
        # in-progress state and must NEVER be broken (that would defeat the
        # O_EXCL guard).
        if lock.exists():
            try:
                data = json.loads(lock.read_text(encoding="utf-8"))
                pid = data.get("pid") if isinstance(data, dict) else None
                if pid and not _pid_alive(int(pid)):
                    lock.unlink(missing_ok=True)
            except (OSError, ValueError):
                age = _time.time() - lock.stat().st_mtime
                if age > 30:
                    lock.unlink(missing_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def _ensure_proxy(upstream_base: str) -> Tuple[Optional[str], Optional[str]]:
    """Start (or reuse) the SSE normalization proxy for an upstream base URL.

    Returns (proxy_base_url, None) on success, (None, error) on failure.
    dsh talks to the proxy; the proxy talks to the real gateway and always
    terminates the SSE stream with a clean `[DONE]` (fixes STREAM_CLOSED from
    omniRoute keepalive events).
    """
    global _proxy_proc, _proxy_port, _proxy_upstream, _proxy_owned
    with _proxy_lock:
        upstream = upstream_base.rstrip("/")
        if (_proxy_proc is not None and _proxy_proc.poll() is None
                and _proxy_upstream == upstream):
            return f"http://127.0.0.1:{_proxy_port}", None
        if _proxy_proc is not None and _proxy_proc.poll() is None:
            _proxy_proc.terminate()
            try:
                _proxy_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                _proxy_proc.kill()
            _proxy_proc = None
        script = _sse_proxy_script()
        if not script.exists():
            return None, "sse_proxy.py missing beside plugin"
        # Cross-process coordination: parallel Hermes sessions (separate
        # processes) can race _ensure_proxy for the same upstream. The
        # threading lock is process-local, so the lockfile must be acquired
        # ATOMICALLY (O_EXCL): the winner spawns the proxy; a loser detects
        # the winner's lock (or that a live same-upstream proxy now exists)
        # and REUSES it instead of spawning a duplicate.
        lock = _proxy_lockfile(upstream)
        acquired = _acquire_proxy_lock(lock)
        # The loser of the O_EXCL race must WAIT for the winner to populate
        # the lock (the winner writes port/pid only after spawning+ready).
        # Poll briefly; if the winner's live proxy appears, reuse it.
        import time as _t0
        if not acquired:
            for _ in range(20):  # up to ~2s
                _t0.sleep(0.1)
                try:
                    if lock.exists():
                        data = json.loads(lock.read_text(encoding="utf-8"))
                        if (data.get("upstream") == upstream and data.get("pid")
                                and _pid_alive(int(data["pid"]))):
                            port = int(data["port"])
                            try:
                                with urllib.request.urlopen(
                                        f"http://127.0.0.1:{port}/healthz",
                                        timeout=2) as r:
                                    if r.status == 200:
                                        _proxy_port = port
                                        _proxy_upstream = upstream
                                        _proxy_proc = None
                                        _proxy_owned = False
                                        return f"http://127.0.0.1:{port}", None
                            except Exception:  # noqa: BLE001
                                pass
                except (OSError, ValueError):
                    pass
            if not acquired:
                # We lost the O_EXCL race and the winner's proxy did not
                # appear within the wait. Never spawn a duplicate: report the
                # contention so the caller can retry.
                return None, ("another process is starting the SSE proxy for "
                              "this upstream; retry in a moment")
        try:
            # Orphan reuse: a hard-exited parent (os._exit / SIGKILL) leaves its
            # proxy alive and invisible to this fresh module state. If the
            # lockfile names a live proxy for the SAME upstream, reuse it instead
            # of spawning a duplicate (accumulating leak on repeated hard kills).
            try:
                if lock.exists():
                    data = json.loads(lock.read_text(encoding="utf-8"))
                    if data.get("upstream") == upstream and data.get("pid"):
                        if _pid_alive(int(data["pid"])):
                            port = int(data["port"])
                            try:
                                with urllib.request.urlopen(
                                        f"http://127.0.0.1:{port}/healthz",
                                        timeout=2) as r:
                                    if r.status == 200:
                                        _proxy_port = port
                                        _proxy_upstream = upstream
                                        _proxy_proc = None  # not ours; never kill
                                        _proxy_owned = False
                                        return f"http://127.0.0.1:{port}", None
                            except Exception:  # noqa: BLE001
                                pass
            except Exception:  # noqa: BLE001
                pass
            # pick a free port
            import socket
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(script), str(port), upstream],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                return None, f"cannot start sse proxy: {e}"
            # wait for readiness — probe the proxy's OWN listener (/healthz does
            # not forward upstream), so a dead/absent upstream never makes the
            # proxy look unready (it is still ready to serve dsh's requests).
            import time as _t
            ready = False
            for _ in range(40):
                if proc.poll() is not None:
                    return None, "sse proxy exited during startup"
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                        if r.status == 200:
                            ready = True
                            break
                except Exception:  # noqa: BLE001
                    pass
                _t.sleep(0.1)
            if not ready:
                proc.kill()
                return None, "sse proxy did not become ready"
            _proxy_proc = proc
            _proxy_port = port
            _proxy_upstream = upstream
            _proxy_owned = True
            _write_proxy_lock(port, upstream, proc.pid)
            return f"http://127.0.0.1:{port}", None
        finally:
            # If we ACQUIRED the lock but did NOT spawn a live proxy (reused
            # an existing one, or spawn/readiness failed), our empty lock
            # would wedge the next process (no pid to stale-break). Clear it
            # so the winner's real lock (if any) or a fresh acquisition wins.
            if acquired and not _proxy_owned:
                _clear_proxy_lock(upstream)


def _shutdown_proxy() -> None:
    global _proxy_proc, _proxy_owned
    with _proxy_lock:
        # Only terminate + clear the lockfile if THIS process spawned the
        # proxy. A reused orphan (not ours) must keep its lockfile so the
        # NEXT process can still find it — clearing it would make the next
        # process spawn a duplicate.
        if _proxy_owned and _proxy_proc is not None and _proxy_proc.poll() is None:
            _proxy_proc.terminate()
            try:
                _proxy_proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                _proxy_proc.kill()
        _proxy_proc = None
        if _proxy_owned:
            _clear_proxy_lock(_proxy_upstream or None)
        _proxy_owned = False


def _cleanup() -> None:
    _shutdown_proxy()


atexit.register(_cleanup)


def _workspace_slug(cwd: Optional[str]) -> str:
    """Replicate dsh's JSONL workspace-slug derivation from a cwd path.

    Mirrors packages/session/session-persistence-jsonl/src/format.ts:
    separators (`/` `\\` `:`) collapse to a single `-`; `~` is skipped;
    [A-Za-z0-9._-] passes through; anything else becomes `~XXXX` uppercase
    hex of its char code; leading `-`s are stripped (empty -> `root`);
    wrapped in `--...--` capped at 251 chars.
    """
    if not cwd:
        # dsh inherits the launching process's cwd; the slug must match what
        # the subprocess will actually use (NOT the plugin dir — a run from
        # any other cwd would land in a different slug and be misattributed).
        cwd = os.getcwd()
    readable = ""
    separator_run = False
    for ch in str(cwd):
        if ch in ("/", "\\", ":"):
            if not separator_run:
                readable += "-"
            separator_run = True
        elif ch != "~" and (ch.isascii() and (ch.isalnum() or ch in "._-")):
            readable += ch
            separator_run = False
        else:
            readable += "~" + format(ord(ch), "04X").upper()
            separator_run = False
    slug = readable.lstrip("-") or "root"
    return "--" + slug[:251] + "--"


def _trunc(s: Any, n: int = 400) -> str:
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return s[:n] + ("…" if len(s) > n else "")


# ── handlers ───────────────────────────────────────────────────────────
def _handle_dsh_status(args: dict, **kwargs: Any) -> str:
    try:
        node_ok, node_detail = _node_ok()
        bin_cmd, resolution = _resolve_bin()
        dsh_home = Path(args.get("dsh_home") or str(_default_dsh_home()))
        llm_env = _resolve_llm_env()
        base = llm_env.get("DEEPSEEK_BASE_URL", "")
        model = _resolve_model()
        store = _find_session_store(dsh_home)
        result = {
            "node": node_detail,
            "node_satisfies": node_ok,
            "dsh_installed": bin_cmd is not None,
            "dsh_bin": (bin_cmd[1] if bin_cmd and bin_cmd[0] == "node"
                        else (bin_cmd[0] if bin_cmd else None)),
            "resolution_source": resolution.get("source"),
            "dsh_home": str(dsh_home),
            "dsh_home_exists": dsh_home.exists(),
            "deepseek_key_present": bool(llm_env.get("DEEPSEEK_API_KEY")),
            "effective_base_url": base,
            "model_override": model,
            "managed_install_dir": str(_STATE_ROOT),
            "session_store_found": store is not None,
        }
        if store is not None:
            result["session_store_kind"] = store["kind"]
        result["sse_proxy_active"] = (_proxy_proc is not None
                                      and _proxy_proc.poll() is None)
        if _proxy_proc is not None and _proxy_proc.poll() is None:
            result["sse_proxy_url"] = f"http://127.0.0.1:{_proxy_port}"
        if resolution.get("error"):
            result["resolution_note"] = resolution["error"]
        return json.dumps(result, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


def _handle_dsh_bootstrap(args: dict, **kwargs: Any) -> str:
    try:
        force = bool(args.get("force"))
        bin_cmd, detail = _do_install(force=force)
        dsh_home = Path(args.get("dsh_home") or str(_default_dsh_home()))
        result = {
            "installed": bin_cmd is not None,
            "bin": (bin_cmd[1] if bin_cmd else None),
            "source": detail.get("source"),
            "already": detail.get("already", False),
            "installed_now": detail.get("installed", False),
            "state_root": str(_STATE_ROOT),
            "dsh_home": str(dsh_home),
        }
        if detail.get("error"):
            result["error"] = detail["error"]
            return json.dumps(result)
        # Version probe — proves the managed binary actually boots.
        try:
            probe = subprocess.run(
                bin_cmd + ["--version"], capture_output=True, text=True,
                timeout=_VERSION_PROBE_TIMEOUT,
                env=_build_env(dsh_home),
            )
            result["dsh_version"] = (probe.stdout or probe.stderr or "").strip()[:200]
            result["probe_exit"] = probe.returncode
        except (subprocess.TimeoutExpired, OSError) as e:
            result["probe_error"] = str(e)
        return json.dumps(result, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


def _handle_dsh_run(args: dict, **kwargs: Any) -> str:
    global _active_runs
    try:
        task = str(args.get("task") or "").strip()
        if not task:
            return json.dumps({"error": "task is required (non-empty string)"})
        timeout = max(10, min(_to_int(args.get("timeout"), _RUN_TIMEOUT_DEFAULT),
                              _MAX_RUN_TIMEOUT))
        dsh_home = Path(args.get("dsh_home") or str(_default_dsh_home()))
        cwd = args.get("cwd") or None

        bin_cmd, resolution = _resolve_bin()
        if bin_cmd is None:
            return json.dumps({"error": "dsh is not installed — run dsh_bootstrap "
                                        f"first ({resolution.get('error')})"})

        pre_ids: Optional[set] = None
        store = _find_session_store(dsh_home)
        if store is not None:
            try:
                pre_ids = _store_session_ids(store)
            except Exception:  # noqa: BLE001
                pre_ids = None

        retries = max(0, min(_to_int(args.get("retries"), 2), 4))

        import time as _time
        started = _time.time()
        base_cmd = bin_cmd + ["--profile", "headless"]
        patch_file: Optional[Path] = None
        model = _resolve_model()
        if model is None:
            return json.dumps({
                "error": "no model override set — dsh's base default "
                         "(deepseek-v4-flash) is not routable on this gateway. "
                         "Set DSH_MODEL (e.g. combo/deepseek-v4-flash) in "
                         "os.environ or ~/.hermes/.env, then retry.",
            })
        patch_file = _model_patch_file(model)
        if patch_file is None:
            return json.dumps({
                "error": "cannot write the model --patch overlay — dsh would "
                         "run the unrouteable base model. Check temp-dir "
                         "write access, then retry.",
            })
        base_cmd += ["--patch", str(patch_file)]
        cmd = base_cmd + [task]

        def _transient(text: str) -> bool:
            """Transient gateway/stream failures worth retrying (QUOTA is not)."""
            low = text.lower()
            if "insufficient balance" in low or "quota" in low:
                return False
            markers = (
                "stream_closed", "stream ended", "transport", "econnreset",
                "econnrefused", "socket hang", "etimedout", "timed out",
                "empty_response", "completed response with no content",
                "429", "502", "503", "504", "too many requests",
                "connection refused", "fetch failed", "network error",
                # dsh NORMALIZES gateway statuses into "dsh: SERVER: <reason>"
                # (e.g. 503 -> "SERVER: overloaded", 502 -> "SERVER: bad gateway")
                # — the numeric codes above never appear in that form.
                "dsh: server", "server: overloaded", "server error",
                "service unavailable", "bad gateway", "overloaded",
            )
            return any(m in low for m in markers)

        attempts: List[Dict[str, Any]] = []
        last_err: Optional[str] = None
        r: Optional[subprocess.CompletedProcess] = None
        # `timeout` is a TOTAL budget for the whole call (documented as "max
        # seconds to wait"), not per-attempt: retries must not stretch the
        # caller's deadline by (retries+1)x. Each attempt gets the remaining
        # budget; when it is exhausted the loop stops without a doomed retry.
        deadline = _time.time() + timeout
        # Concurrency: a force-bootstrap must not rmtree node_modules under a
        # running dsh subprocess. _active_runs gates the bootstrap's clean
        # (see _do_install); the counter is guarded by _boot_lock and covers
        # the whole subprocess wait WITHOUT serializing concurrent runs.
        global _active_runs
        with _boot_lock:
            _active_runs += 1
        try:
            for attempt in range(retries + 1):
                remaining = deadline - _time.time()
                if remaining <= 1:
                    last_err = f"dsh run total budget ({timeout}s) exhausted after {attempt} attempt(s)"
                    attempts.append({"attempt": attempt + 1, "outcome": "budget-exhausted"})
                    break
                try:
                    r = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=remaining,
                        env=_build_env(dsh_home), cwd=cwd,
                    )
                except subprocess.TimeoutExpired as e:
                    tail = _trunc((e.stdout or b"") if isinstance(e.stdout, bytes)
                                  else (e.stdout or ""), 1000)
                    last_err = (f"dsh run exceeded remaining budget "
                                f"({remaining:.0f}s, total {timeout}s) — killed; "
                                "dsh closes the interrupted session with synthetic "
                                "closers on next load")
                    attempts.append({"attempt": attempt + 1, "outcome": "timeout",
                                     "stdout_tail": tail})
                    if attempt < retries and deadline - _time.time() > 2:
                        _time.sleep(min(8, 2 ** (attempt + 1)))
                        continue
                    break
                except FileNotFoundError as e:
                    return json.dumps({"error": f"dsh launch failed: {e}"})

                stderr_text = (r.stderr or "").strip()
                attempts.append({"attempt": attempt + 1, "exit": r.returncode,
                                 "stderr_tail": _trunc(stderr_text, 500)})
                if r.returncode == 0:
                    break
                last_err = stderr_text
                if attempt < retries and _transient(stderr_text or "exit") \
                        and deadline - _time.time() > 2:
                    _time.sleep(min(8, 2 ** (attempt + 1)))
                    continue
                break
        finally:
            # Clean the model patch on EVERY exit path — including
            # KeyboardInterrupt/SystemExit that propagate out of the loop
            # (a Ctrl-C'd run must not leak /tmp/dsh-model-*.yml).
            if patch_file is not None:
                try:
                    patch_file.unlink(missing_ok=True)
                except OSError:
                    pass
            with _boot_lock:
                _active_runs -= 1

        elapsed = round(_time.time() - started, 2)
        answer = (r.stdout or "").strip() if r is not None else ""
        session_ids: List[str] = []
        session_ids_uncertain: List[str] = []
        store2 = _find_session_store(dsh_home)
        if store2 is not None:
            try:
                after = _store_session_ids(store2)
                new_ids = after - pre_ids if pre_ids is not None else after
                rows = _store_session_list(store2, 100)
                # Attribute sessions created under THIS run's workspace slug.
                # Concurrent runs sharing a cwd land in the same slug, so the
                # slug filter cannot tell them apart — those are reported as
                # "uncertain" rather than silently double-claimed.
                slug = _workspace_slug(cwd)
                # Backend-aware workspace match: JSONL rows carry the on-disk
                # `path` (slug embedded); SQLite rows carry the run `cwd`
                # (derive the slug from it — the workspace root the session
                # was created under). A row with neither is treated as
                # non-matching (uncertain) rather than falsely certain.
                def _in_workspace(row: Dict[str, Any]) -> bool:
                    p = row.get("path") or ""
                    if p:
                        return p.find(slug) != -1
                    rc = row.get("cwd")
                    if rc:
                        return _workspace_slug(str(rc)) == slug
                    return False

                slug_new = [row for row in rows
                            if row["id"] in new_ids and _in_workspace(row)]
                if len(slug_new) == 1:
                    # unambiguous: exactly one new session in this workspace
                    session_ids = [slug_new[0]["id"]]
                else:
                    # Concurrent same-cwd runs land in the same slug with
                    # near-identical timestamps — attribution from the store is
                    # genuinely ambiguous. Never guess: report all as
                    # uncertain (the caller can inspect created_at/path), and
                    # claim nothing certain.
                    session_ids_uncertain = [row["id"] for row in slug_new]
                for row in rows:
                    if row["id"] in new_ids and row["id"] not in session_ids \
                            and row["id"] not in session_ids_uncertain:
                        session_ids_uncertain.append(row["id"])
            except Exception:  # noqa: BLE001
                session_ids = []
                session_ids_uncertain = []

        result = {
            "exit": r.returncode if r is not None else 1,
            "completed": (r is not None and r.returncode == 0),
            "elapsed_s": elapsed,
            "answer": answer,
            "session_ids": session_ids,
            "new_sessions": len(session_ids),
            "session_ids_uncertain": session_ids_uncertain,
            "new_sessions_uncertain": len(session_ids_uncertain),
            "attempts": attempts,
        }
        if last_err:
            result["stderr_tail"] = _trunc(last_err, 1200)
        return json.dumps(result, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


def _handle_dsh_sessions(args: dict, **kwargs: Any) -> str:
    try:
        limit = max(1, min(_to_int(args.get("limit"), 20), 100))
        dsh_home = Path(args.get("dsh_home") or str(_default_dsh_home()))
        store = _store_or_error(dsh_home)
        if isinstance(store, dict) and "kind" not in store:
            return json.dumps(store)
        sessions = _store_session_list(store, limit)
        return json.dumps({"count": len(sessions), "sessions": sessions},
                          default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


def _handle_dsh_session_events(args: dict, **kwargs: Any) -> str:
    try:
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return json.dumps({"error": "session_id is required"})
        limit = max(1, min(_to_int(args.get("limit"), _DEFAULT_EVENT_LIMIT),
                           _MAX_EVENT_LIMIT))
        offset = max(0, _to_int(args.get("offset"), 0))
        dsh_home = Path(args.get("dsh_home") or str(_default_dsh_home()))
        store = _store_or_error(dsh_home)
        if isinstance(store, dict) and "kind" not in store:
            return json.dumps(store)
        if store["kind"] == "jsonl":
            header, events = _jsonl_session_events(store, session_id, limit, offset)
            if header is None and not events:
                # distinguish "not found" from "no events": check existence
                if session_id not in _store_session_ids(store):
                    return json.dumps({"error": f"session not found: {session_id}"})
            meta = ({"created_at": header.get("createdAt"),
                     "parent_session": header.get("parentSession"),
                     "version": header.get("version")} if header else None)
            return json.dumps({
                "session_id": session_id,
                "meta": meta,
                "offset": offset,
                "returned": len(events),
                "events": events,
            }, default=str)
        try:
            con = sqlite3.connect(f"file:{store['path']}?mode=ro", uri=True, timeout=5)
            try:
                info = con.execute(
                    "SELECT created_at, parent_session, version FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                rows = con.execute(
                    "SELECT seq, type, time, data FROM events WHERE session_id=? "
                    "ORDER BY seq ASC LIMIT ? OFFSET ?",
                    (session_id, limit, offset),
                ).fetchall()
                events = [
                    {"seq": r[0], "type": r[1], "time": r[2], "data": _trunc(r[3], 600)}
                    for r in rows
                ]
            finally:
                con.close()
        except sqlite3.Error as e:
            return json.dumps({"error": f"session store read failed: {e}"})
        if info is None:
            return json.dumps({"error": f"session not found: {session_id}"})
        return json.dumps({
            "session_id": session_id,
            "meta": {"created_at": info[0], "parent_session": info[1],
                     "version": info[2]},
            "offset": offset,
            "returned": len(events),
            "events": events,
        }, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


def _handle_dsh_session_export(args: dict, **kwargs: Any) -> str:
    try:
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return json.dumps({"error": "session_id is required"})
        max_events = max(1, min(_to_int(args.get("max_events"), _DEFAULT_EXPORT_LIMIT),
                                _MAX_EXPORT_LIMIT))
        full = bool(args.get("full_data"))
        dsh_home = Path(args.get("dsh_home") or str(_default_dsh_home()))
        store = _store_or_error(dsh_home)
        if isinstance(store, dict) and "kind" not in store:
            return json.dumps(store)
        if store["kind"] == "jsonl":
            header, lines = _jsonl_session_export(store, session_id, max_events, full)
            if header is None and not lines:
                if session_id not in _store_session_ids(store):
                    return json.dumps({"error": f"session not found: {session_id}"})
            return json.dumps({
                "session_id": session_id,
                "returned": len(lines),
                "jsonl": "\n".join(lines),
            }, default=str)
        try:
            con = sqlite3.connect(f"file:{store['path']}?mode=ro", uri=True, timeout=5)
            try:
                exists = con.execute(
                    "SELECT 1 FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                rows = con.execute(
                    "SELECT seq, type, time, data, source_event_seqs FROM events "
                    "WHERE session_id=? ORDER BY seq ASC LIMIT ?",
                    (session_id, max_events),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            return json.dumps({"error": f"session store read failed: {e}"})
        if exists is None:
            return json.dumps({"error": f"session not found: {session_id}"})
        lines = []
        for r in rows:
            rec = {"seq": r[0], "type": r[1], "time": r[2]}
            try:
                parsed = json.loads(r[3]) if r[3] else None
                rec["data"] = parsed if full else _trunc(r[3], 400)
            except (ValueError, TypeError):
                rec["data"] = r[3]
            if r[4]:
                rec["source_event_seqs"] = r[4]
            lines.append(json.dumps(rec, default=str))
        return json.dumps({
            "session_id": session_id,
            "returned": len(lines),
            "jsonl": "\n".join(lines),
        }, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


def _handle_dsh_lineage(args: dict, **kwargs: Any) -> str:
    try:
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return json.dumps({"error": "session_id is required"})
        dsh_home = Path(args.get("dsh_home") or str(_default_dsh_home()))
        store = _store_or_error(dsh_home)
        if isinstance(store, dict) and "kind" not in store:
            return json.dumps(store)
        if store["kind"] == "jsonl":
            if session_id not in _store_session_ids(store):
                return json.dumps({"error": f"session not found: {session_id}"})
            ancestors, descendants = _jsonl_lineage(store, session_id)
            return json.dumps({
                "session_id": session_id,
                "ancestors": ancestors,
                "child_count": len(descendants),
                "children": descendants,
            }, default=str)
        try:
            con = sqlite3.connect(f"file:{store['path']}?mode=ro", uri=True, timeout=5)
            try:
                exists = con.execute(
                    "SELECT 1 FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if exists is None:
                    return json.dumps({"error": f"session not found: {session_id}"})
                ancestors: List[Dict[str, Any]] = []
                seen = {session_id}
                cur = session_id
                while True:
                    row = con.execute(
                        "SELECT parent_session FROM sessions WHERE id=?", (cur,)
                    ).fetchone()
                    if not row or not row[0]:
                        break
                    pid = row[0]
                    if pid in seen:  # cycle guard
                        break
                    seen.add(pid)
                    info = con.execute(
                        "SELECT version, origin, delegation_depth, created_at FROM sessions WHERE id=?",
                        (pid,),
                    ).fetchone()
                    ancestors.append({"id": pid,
                                      "origin": info[1] if info else None,
                                      "delegation_depth": info[2] if info else None,
                                      "created_at": info[3] if info else None})
                    cur = pid

                child_rows = con.execute(
                    "SELECT id, origin, delegation_depth, created_at FROM sessions "
                    "WHERE parent_session=? ORDER BY created_at DESC", (session_id,)
                ).fetchall()
                descendants = [
                    {"id": r[0], "origin": r[1], "delegation_depth": r[2],
                     "created_at": r[3]} for r in child_rows
                ]
            finally:
                con.close()
        except sqlite3.Error as e:
            return json.dumps({"error": f"session store read failed: {e}"})
        return json.dumps({
            "session_id": session_id,
            "ancestors": ancestors,
            "child_count": len(descendants),
            "children": descendants,
        }, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


# ── registration ───────────────────────────────────────────────────────
def register(ctx: Any) -> None:
    logger.info("Registering hermes-dsh plugin")

    ctx.register_tool(
        name="dsh_status",
        toolset="dsh",
        schema={
            "name": "dsh_status",
            "description": "DeepSeek Harness (dsh) health/runtime status: Node version + "
                           "satisfies the >=22.19 requirement, dsh binary resolution "
                           "(DSH_BIN override or managed npm install), DSH_HOME session-"
                           "store path, whether the DeepSeek API key is present, the "
                           "effective base URL + model override (DSH_BASE_URL / DSH_API_KEY "
                           "/ DSH_MODEL env win over DEEPSEEK_*), and whether a dsh session "
                           "store exists yet. Use before running dsh tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dsh_home": {
                        "type": "string",
                        "description": "Override dsh home dir (default: $DSH_HOME or ~/.dsh)",
                    },
                },
            },
        },
        handler=_handle_dsh_status,
    )

    ctx.register_tool(
        name="dsh_bootstrap",
        toolset="dsh",
        schema={
            "name": "dsh_bootstrap",
            "description": "Ensure DeepSeek Harness (dsh) is installed. Idempotent: installs "
                           "@deepseek-ai/dsh via npm into the managed state dir once, then "
                           "reuses it. force=true reinstalls. Self-heal on partial installs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "force": {
                        "type": "boolean",
                        "description": "Force a clean reinstall even if present (default false)",
                        "default": False,
                    },
                    "dsh_home": {
                        "type": "string",
                        "description": "Override dsh home dir (default: $DSH_HOME or ~/.dsh)",
                    },
                },
            },
        },
        handler=_handle_dsh_bootstrap,
    )

    ctx.register_tool(
        name="dsh_run",
        toolset="dsh",
        schema={
            "name": "dsh_run",
            "description": "Run one task through deepseek-harness' headless bundle — an "
                           "independent DeepSeek agent with its own event-sourced, crash-safe "
                           "session log. Returns the final answer plus the new durable "
                           "session id(s). Credentials: DSH_API_KEY/DSH_BASE_URL/DSH_MODEL "
                           "env override DEEPSEEK_* (use a DeepSeek-compatible gateway model "
                           "id via DSH_MODEL when the gateway does not route bare ids). Use "
                           "to get a second (DeepSeek-native) agent's perspective, or to "
                           "produce replay-grade session traces.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task to run in the headless DeepSeek Harness agent",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 300, range 10-1800)",
                        "default": 300,
                    },
                    "retries": {
                        "type": "integer",
                        "description": "Extra attempts on transient gateway/stream errors "
                                       "(STREAM_CLOSED, 429/502/503, reset). QUOTA is never "
                                       "retried. Default 2, range 0-4",
                        "default": 2,
                    },
                    "dsh_home": {
                        "type": "string",
                        "description": "Override dsh home / session store dir",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the subprocess (default: none)",
                    },
                },
                "required": ["task"],
            },
        },
        handler=_handle_dsh_run,
    )

    ctx.register_tool(
        name="dsh_sessions",
        toolset="dsh",
        schema={
            "name": "dsh_sessions",
            "description": "List sessions persisted by deepseek-harness in its durable SQLite "
                           "event-sourced store (newest first): id, created_at, parent_session "
                           "(lineage), origin, delegation_depth, revision, event_count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max sessions to return (1-100, default 20)",
                        "default": 20,
                    },
                    "dsh_home": {
                        "type": "string",
                        "description": "Override dsh home / session store dir",
                    },
                },
            },
        },
        handler=_handle_dsh_sessions,
    )

    ctx.register_tool(
        name="dsh_session_events",
        toolset="dsh",
        schema={
            "name": "dsh_session_events",
            "description": "Read one dsh session's durable event-sourced log: the append-only "
                           "seq/type/time/data record that deepseek-harness derives model "
                           "history from. Best for inspecting exactly what a dsh agent did.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "dsh session id"},
                    "limit": {
                        "type": "integer",
                        "description": "Max events to return (default 120, max 2000)",
                        "default": 120,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip first N events (default 0)",
                        "default": 0,
                    },
                    "dsh_home": {
                        "type": "string",
                        "description": "Override dsh home / session store dir",
                    },
                },
                "required": ["session_id"],
            },
        },
        handler=_handle_dsh_session_events,
    )

    ctx.register_tool(
        name="dsh_session_export",
        toolset="dsh",
        schema={
            "name": "dsh_session_export",
            "description": "Dump one dsh session's full raw event log as JSONL — the "
                           "replay/fork/audit artifact. set full_data=true for complete "
                           "event bodies (can be large).",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "dsh session id"},
                    "max_events": {
                        "type": "integer",
                        "description": "Cap on events exported (default 500, max 10000)",
                        "default": 500,
                    },
                    "full_data": {
                        "type": "boolean",
                        "description": "Include full parsed event data (default: truncated)",
                        "default": False,
                    },
                    "dsh_home": {
                        "type": "string",
                        "description": "Override dsh home / session store dir",
                    },
                },
                "required": ["session_id"],
            },
        },
        handler=_handle_dsh_session_export,
    )

    ctx.register_tool(
        name="dsh_lineage",
        toolset="dsh",
        schema={
            "name": "dsh_lineage",
            "description": "Traverse dsh session fork genealogy: walk parent_session to "
                           "ancestors (with cycle guard) and list direct child sessions — the "
                           "lineage deepseek-harness records for forked/resumed/subagent "
                           "sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "dsh session id"},
                    "dsh_home": {
                        "type": "string",
                        "description": "Override dsh home / session store dir",
                    },
                },
                "required": ["session_id"],
            },
        },
        handler=_handle_dsh_lineage,
    )
