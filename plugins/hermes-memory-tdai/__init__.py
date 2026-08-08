"""
hermes-memory-tdai — Four-layer agent memory for Hermes via TencentDB Agent Memory.

Wraps the TencentDB Agent Memory Gateway (MemoryCore) — the open-source
four-layer memory system:
    L0  Conversation store      (raw conversations, SQLite + sqlite-vec)
    L1  Atomic memories         (structured facts, episodic, instructions)
    L2  Scenario blocks         (Markdown scene files)
    L3  Core memory             (persona / user profile synthesis)

ARCHITECTURE:
  The gateway is a Node.js sidecar (MemoryCore/src/gateway/server.ts) that
  exposes an HTTP API on 127.0.0.1:8420 (TDAI_GATEWAY_PORT). This plugin is
  a thin HTTP client + process supervisor that plugs the gateway into Hermes:

    Hermes Agent (Python)
      └─ _TdaiEngine (this plugin)
           ├─ _GatewaySupervisor  — starts / health-checks the sidecar
           └─ _TdaiClient         — POST /v3/conversation/*, /v3/atomic/*,
                                    /v3/scenario/*, /v3/core/*, /health
                │
                ▼  HTTP (127.0.0.1:8420)
        memory-tdai Gateway (Node.js, MemoryCore)

  The gateway repo is auto-cloned to ~/.hermes/tdai/tencentdb-agent-memory
  on first use and kept up to date via git pull (agents_update-style).
  npm install runs once. LLM credentials for L1-L3 extraction are read from
  env (TDAI_LLM_API_KEY / TDAI_LLM_BASE_URL / TDAI_LLM_MODEL); without them
  the gateway still serves L0 conversation add/search.

THREAD SAFETY:
  All gateway HTTP calls are serialized via a module-level RLock. This is
  acceptable since memory operations are agent-driven and infrequent.

DEPENDENCIES (JIT installed):
  - Node.js 20+ (for the gateway sidecar)
  - The TencentDB Agent Memory repo (git clone) + npm install in MemoryCore
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Config (env overridable) ─────────────────────────────────────────────
TDAI_GATEWAY_HOST = os.environ.get("TDAI_GATEWAY_HOST", "127.0.0.1")
TDAI_GATEWAY_PORT = int(os.environ.get("TDAI_GATEWAY_PORT", "8420"))
TDAI_GATEWAY_API_KEY = os.environ.get("TDAI_GATEWAY_API_KEY", "")
TDAI_REPO_DIR = Path(os.environ.get("TDAI_REPO_DIR", str(Path.home() / ".hermes" / "tdai" / "tencentdb-agent-memory")))
TDAI_TIMEOUT = int(os.environ.get("TDAI_TIMEOUT", "15"))
TDAI_LLM_BASE_URL = os.environ.get("TDAI_LLM_BASE_URL", "")
TDAI_LLM_API_KEY = os.environ.get("TDAI_LLM_API_KEY", "")
TDAI_LLM_MODEL = os.environ.get("TDAI_LLM_MODEL", "")
# Gateway data dir (L0-L3 storage)
TDAI_DATA_DIR = os.environ.get("TDAI_DATA_DIR", str(Path.home() / ".hermes" / "tdai" / "data"))

# ── JIT dependency management ──────────────────────────────────────────────
try:
    from _shared.deps import DepSpec, ensure_deps

    _TDAI_DEPS: List[DepSpec] = [
        DepSpec(
            "node",
            ["node", "--version"],
            install=None,
            purpose="Node.js runtime for the TencentDB Agent Memory gateway",
        ),
    ]

    def _ensure_tdai_deps() -> str | None:
        """Install Node.js if not found. Returns error string or None on success."""
        try:
            ensure_deps("hermes-memory-tdai", _TDAI_DEPS, ask=False)
            return None
        except Exception as e:
            return str(e)

except ImportError:
    def _ensure_tdai_deps() -> str | None:
        return "_shared.deps not available — cannot auto-install dependencies"


# ── Gateway supervisor ────────────────────────────────────────────────────
def _find_gateway_script() -> Optional[str]:
    """Locate the gateway server.ts in the cloned repo."""
    candidates = [
        TDAI_REPO_DIR / "MemoryCore" / "src" / "gateway" / "server.ts",
        TDAI_REPO_DIR / "src" / "gateway" / "server.ts",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _clone_gateway_repo() -> Optional[str]:
    """Clone the TencentDB Agent Memory repo. Returns error or None."""
    if _find_gateway_script():
        return None
    try:
        TDAI_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        if TDAI_REPO_DIR.exists():
            # Safety: only remove if it looks like the repo (has package.json or .git)
            # AND the path is inside our owner dir (~/.hermes/tdai/) — never rmtree
            # an arbitrary env-overridden path (e.g. TDAI_REPO_DIR=/)
            owner_dir = Path.home() / ".hermes" / "tdai"
            is_inside_owner = str(TDAI_REPO_DIR.resolve()).startswith(str(owner_dir.resolve()) + os.sep)
            if not is_inside_owner:
                return "refusing to remove TDAI_REPO_DIR outside ~/.hermes/tdai"
            if not (TDAI_REPO_DIR / ".git").exists() and not (TDAI_REPO_DIR / "package.json").exists():
                shutil.rmtree(str(TDAI_REPO_DIR))
        r = subprocess.run(
            ["git", "clone", "https://github.com/TencentCloud/TencentDB-Agent-Memory.git", str(TDAI_REPO_DIR)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            return f"git clone failed: {r.stderr[:300]}"
        return None
    except Exception as e:
        return f"clone failed: {e}"


def _update_gateway_repo() -> Optional[str]:
    """git pull the gateway repo to get the latest version. Returns error or None."""
    if not (TDAI_REPO_DIR / ".git").exists():
        return _clone_gateway_repo()
    try:
        # Fail fast on offline: short timeout, then treat as non-fatal
        r = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            capture_output=True, text=True, timeout=15,
            cwd=str(TDAI_REPO_DIR),
        )
        if r.returncode != 0:
            # Try HEAD instead of main (branch name drift)
            r = subprocess.run(
                ["git", "pull", "--ff-only"],
                capture_output=True, text=True, timeout=15,
                cwd=str(TDAI_REPO_DIR),
            )
            if r.returncode != 0:
                return f"git pull failed: {r.stderr[:300]}"
        return None
    except subprocess.TimeoutExpired:
        return "git pull timed out (offline?)"
    except Exception as e:
        return f"git pull error: {e}"


def _install_gateway_deps() -> Optional[str]:
    """Run npm install in MemoryCore. Returns error or None."""
    memory_core = TDAI_REPO_DIR / "MemoryCore"
    if not (memory_core / "package.json").exists():
        return "MemoryCore/package.json not found after clone"
    # Skip if node_modules already present
    if (memory_core / "node_modules").exists():
        return None
    try:
        r = subprocess.run(
            ["npm", "install"],
            capture_output=True, text=True, timeout=600,
            cwd=str(memory_core),
        )
        if r.returncode != 0:
            return f"npm install failed: {r.stderr[:300]}"
        return None
    except Exception as e:
        return f"npm install error: {e}"


# ── HTTP client ────────────────────────────────────────────────────────────
class _TdaiClient:
    """Minimal HTTP client for the memory-tdai Gateway (v3 API)."""

    def __init__(self, base_url: str, api_key: str = "", timeout: int = 15, service_id: str = "default"):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._service_id = service_id or "default"

    def _headers(self, content_type: bool = True) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if content_type:
            h["Content-Type"] = "application/json"
        h["Authorization"] = f"Bearer {self._api_key or 'local'}"
        h["x-tdai-service-id"] = self._service_id
        return h

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(True), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                # v3 envelope: {code, message, data}
                if isinstance(raw, dict) and "code" in raw and raw.get("code") != 0:
                    return {"error": f"gateway code={raw.get('code')}: {raw.get('message', '')}", "raw": raw}
                return raw
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            return {"error": f"HTTP {e.code}: {body_text}"}
        except Exception as e:
            return {"error": str(e)}

    def _get(self, path: str) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        req = urllib.request.Request(url, headers=self._headers(False), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}"}
        except Exception as e:
            return {"error": str(e)}

    def health(self) -> Dict[str, Any]:
        return self._get("/health")

    # L0 conversation
    def conversation_add(self, messages: List[Dict[str, Any]], *, session_id: str = "",
                         team_id: str = "default", agent_id: str = "default", user_id: str = "default") -> Dict[str, Any]:
        body = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id,
                "session_id": session_id, "messages": messages}
        return self._post("/v3/conversation/add", body)

    def conversation_search(self, query: str, *, limit: int = 5, session_id: str = "",
                            team_id: str = "default", agent_id: str = "default", user_id: str = "default") -> Dict[str, Any]:
        body = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id,
                "query": query, "limit": limit}
        if session_id:
            body["session_id"] = session_id
        return self._post("/v3/conversation/search", body)

    # L1 atomic
    def atomic_search(self, query: str, *, limit: int = 5, type_filter: str = "",
                      team_id: str = "default", agent_id: str = "default", user_id: str = "default") -> Dict[str, Any]:
        body = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id,
                "query": query, "limit": limit}
        if type_filter:
            body["type"] = type_filter
        return self._post("/v3/atomic/search", body)

    # L2 scenario
    def scenario_ls(self, *, team_id: str = "default", agent_id: str = "default", user_id: str = "default") -> Dict[str, Any]:
        body = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id}
        return self._post("/v3/scenario/ls", body)

    def scenario_read(self, path: str, *, team_id: str = "default", agent_id: str = "default", user_id: str = "default") -> Dict[str, Any]:
        body = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id, "path": path}
        return self._post("/v3/scenario/read", body)

    # L3 core
    def core_read(self, *, team_id: str = "default", agent_id: str = "default", user_id: str = "default") -> Dict[str, Any]:
        body = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id}
        return self._post("/v3/core/read", body)

    def core_write(self, content: str, *, team_id: str = "default", agent_id: str = "default", user_id: str = "default") -> Dict[str, Any]:
        body = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id, "content": content}
        return self._post("/v3/core/write", body)


# ── Engine ─────────────────────────────────────────────────────────────────
class _TdaiEngine:
    """Lazy singleton managing gateway lifecycle + all memory operations."""

    def __init__(self):
        self._ready = False
        self._error: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None
        self._client: Optional[_TdaiClient] = None
        self._gateway_script: Optional[str] = None
        self._stderr_path: Optional[str] = None
        self._stderr_handle: Any = None

    def ensure_ready(self) -> Optional[str]:
        """Ensure the gateway is cloned, installed, started, and healthy."""
        if self._ready:
            return None
        with _TDAI_LOCK:
            if self._ready:
                return None

            # 1. Node.js
            err = _ensure_tdai_deps()
            if err:
                self._error = err
                return err

            # 2. Clone or update repo (always pull latest from original source)
            if not _find_gateway_script():
                err = _clone_gateway_repo()
                if err:
                    self._error = err
                    return err
            err = _update_gateway_repo()
            if err:
                logger.warning("memory-tdai: repo update skipped (%s) — using existing checkout", err)

            # 3. npm install (one-time)
            err = _install_gateway_deps()
            if err:
                self._error = err
                return err

            # 4. Start gateway if not running
            err = self._start_gateway()
            if err:
                self._error = err
                return err

            self._ready = True
            self._error = None  # clear any stale error from a prior failed attempt
            return None

    def _start_gateway(self) -> Optional[str]:
        """Start the gateway sidecar if not already running, wait for /health."""
        script = _find_gateway_script()
        if not script:
            return "gateway server.ts not found"
        self._gateway_script = script

        # Check if already running
        probe = _TdaiClient(f"http://{TDAI_GATEWAY_HOST}:{TDAI_GATEWAY_PORT}", TDAI_GATEWAY_API_KEY, 3)
        health = probe.health()
        if "error" not in health:
            self._client = probe
            logger.info("memory-tdai: gateway already running on %s:%s", TDAI_GATEWAY_HOST, TDAI_GATEWAY_PORT)
            return None

        # Start it
        env = dict(os.environ)
        env.setdefault("TDAI_GATEWAY_PORT", str(TDAI_GATEWAY_PORT))
        env.setdefault("TDAI_GATEWAY_HOST", TDAI_GATEWAY_HOST)
        if TDAI_GATEWAY_API_KEY:
            env.setdefault("TDAI_GATEWAY_API_KEY", TDAI_GATEWAY_API_KEY)
        env.setdefault("TDAI_DATA_DIR", TDAI_DATA_DIR)
        if TDAI_LLM_BASE_URL:
            env.setdefault("TDAI_LLM_BASE_URL", TDAI_LLM_BASE_URL)
        if TDAI_LLM_API_KEY:
            env.setdefault("TDAI_LLM_API_KEY", TDAI_LLM_API_KEY)
        if TDAI_LLM_MODEL:
            env.setdefault("TDAI_LLM_MODEL", TDAI_LLM_MODEL)

        # Log gateway stderr to a file so crashes are diagnosable
        log_dir = Path.home() / ".hermes" / "logs" / "memory-tdai"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        stderr_path = log_dir / "gateway.stderr.log"
        stderr_handle = None
        try:
            stderr_handle = open(stderr_path, "ab")
        except Exception:
            stderr_handle = None

        try:
            self._process = subprocess.Popen(
                ["node", "--import", "tsx", script],
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle or subprocess.DEVNULL,
                cwd=str(TDAI_REPO_DIR / "MemoryCore"),
                env=env,
            )
            self._stderr_path = str(stderr_path) if stderr_handle else None
            self._stderr_handle = stderr_handle
        except Exception as e:
            if stderr_handle:
                stderr_handle.close()
            return f"failed to start gateway: {e}"

        # Wait for /health
        client = _TdaiClient(f"http://{TDAI_GATEWAY_HOST}:{TDAI_GATEWAY_PORT}", TDAI_GATEWAY_API_KEY, 3)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._process and self._process.poll() is not None:
                break  # gateway exited — no point waiting
            health = client.health()
            if "error" not in health:
                self._client = client
                return None
            time.sleep(0.5)

        # Timed out or exited — capture stderr if possible
        detail = ""
        if self._process and self._process.poll() is not None:
            detail = " (process exited)"
            # Tail the stderr log for the actual error
            if self._stderr_path:
                try:
                    tail = Path(self._stderr_path).read_text(errors="replace")[-1500:]
                    if tail.strip():
                        detail += f" — stderr tail: {tail.strip()[-800:]}"
                except Exception:
                    pass
            # Clean up the dead process
            try:
                self._process.kill()
            except Exception:
                pass
            self._process = None
            # Process died — close the stderr log handle
            if stderr_handle:
                try:
                    stderr_handle.close()
                except Exception:
                    pass
                self._stderr_handle = None
        else:
            # Process still alive but not healthy — keep the handle so
            # shutdown() can terminate it later. Do NOT null it out.
            logger.warning(
                "memory-tdai: gateway still running but not healthy after 30s (pid=%s)",
                self._process.pid if self._process else "?",
            )
        return f"gateway did not become healthy within 30s{detail}"

    def _ensure_client(self) -> Optional[_TdaiClient]:
        err = self.ensure_ready()
        if err:
            return None
        assert self._client is not None
        return self._client

    # ── Operations ─────────────────────────────────────────────────────
    def shutdown(self) -> None:
        """Stop the gateway subprocess if we started it (not if external)."""
        with _TDAI_LOCK:
            proc = self._process
            self._process = None
            self._ready = False
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            # Close the stderr log handle (owned by the gateway's lifetime)
            if self._stderr_handle:
                try:
                    self._stderr_handle.close()
                except Exception:
                    pass
                self._stderr_handle = None

    def status(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ready": self._ready,
            "gateway": f"http://{TDAI_GATEWAY_HOST}:{TDAI_GATEWAY_PORT}",
            "repo": str(TDAI_REPO_DIR),
            "gateway_script": self._gateway_script,
        }
        if self._error:
            result["error"] = self._error
        if self._client and self._ready:
            health = self._client.health()
            if "error" in health:
                # Gateway died after we marked ready — reflect reality
                result["ready"] = False
                result["health"] = f"unreachable: {health['error']}"
            else:
                result["health"] = "ok"
        return result

    def recall(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Recall from all memory layers (L1 atomic + L0 conversations)."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}

        with _TDAI_LOCK:
            result: Dict[str, Any] = {}
            atomic = client.atomic_search(query, limit=limit)
            if "error" not in atomic:
                result["memories"] = atomic
            conv = client.conversation_search(query, limit=limit)
            if "error" not in conv:
                result["conversations"] = conv
            if "memories" not in result and "conversations" not in result:
                return {"error": atomic.get("error") or conv.get("error") or "no results"}
            return result

    def capture(self, messages: List[Dict[str, Any]], *, session_id: str = "") -> Dict[str, Any]:
        """Capture conversation messages to L0."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}
        # The gateway requires a non-empty session_id for writes;
        # use time+pid so concurrent captures never collide on the same id
        if not session_id:
            session_id = f"hermes-{os.getpid()}-{int(time.time() * 1000)}"
        with _TDAI_LOCK:
            return client.conversation_add(messages, session_id=session_id)

    def search_memories(self, query: str, limit: int = 5, type_filter: str = "") -> Dict[str, Any]:
        """Search L1 structured memories."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}
        with _TDAI_LOCK:
            return client.atomic_search(query, limit=limit, type_filter=type_filter)

    def search_conversations(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Search L0 raw conversation history."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}
        with _TDAI_LOCK:
            return client.conversation_search(query, limit=limit)

    def scenarios(self) -> Dict[str, Any]:
        """List L2 scenario blocks."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}
        with _TDAI_LOCK:
            return client.scenario_ls()

    def read_scenario(self, path: str) -> Dict[str, Any]:
        """Read a specific L2 scenario block."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}
        with _TDAI_LOCK:
            return client.scenario_read(path)

    def read_core(self) -> Dict[str, Any]:
        """Read L3 core memory (persona)."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}
        with _TDAI_LOCK:
            return client.core_read()

    def write_core(self, content: str) -> Dict[str, Any]:
        """Write L3 core memory (persona)."""
        client = self._ensure_client()
        if not client:
            return {"error": self._error or "gateway not ready"}
        with _TDAI_LOCK:
            return client.core_write(content)


_TDAI_LOCK = threading.RLock()
_engine = _TdaiEngine()


def _cleanup() -> None:
    """Stop the gateway subprocess at interpreter exit (no orphan sidecar)."""
    try:
        _engine.shutdown()
    except Exception:
        pass


# Register atexit so the gateway we spawned dies with Hermes (like SearXNG)
import atexit
atexit.register(_cleanup)


# ── Tool handlers ───────────────────────────────────────────────────────────
def _handle_tdai_status(args: dict, **kwargs: Any) -> str:
    """Check memory-tdai gateway + engine status."""
    try:
        return json.dumps(_engine.status(), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _clamp_limit(raw: Any, default: int = 5) -> int:
    """Coerce a limit arg to an int in [1, 20]. Never raises."""
    try:
        return max(1, min(int(raw), 20))
    except (TypeError, ValueError):
        return max(1, min(default, 20))


def _handle_tdai_recall(args: dict, **kwargs: Any) -> str:
    """Recall from all memory layers."""
    try:
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})
        limit = _clamp_limit(args.get("limit", 5))
        return json.dumps(_engine.recall(query, limit), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_tdai_capture(args: dict, **kwargs: Any) -> str:
    """Capture conversation messages to L0."""
    try:
        messages = args.get("messages", [])
        if not messages:
            return json.dumps({"error": "messages is required"})
        # Validate message shape (each must be {role, content})
        for m in messages:
            if not isinstance(m, dict) or not m.get("role") or not m.get("content"):
                return json.dumps({"error": "each message must be an object with role and content"})
        session_id = args.get("session_id", "")
        return json.dumps(_engine.capture(messages, session_id=session_id), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_tdai_search(args: dict, **kwargs: Any) -> str:
    """Search L1 structured memories."""
    try:
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})
        limit = _clamp_limit(args.get("limit", 5))
        type_filter = args.get("type", "")
        return json.dumps(_engine.search_memories(query, limit, type_filter), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_tdai_conversations(args: dict, **kwargs: Any) -> str:
    """Search L0 raw conversation history."""
    try:
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})
        limit = _clamp_limit(args.get("limit", 5))
        return json.dumps(_engine.search_conversations(query, limit), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_tdai_scenarios(args: dict, **kwargs: Any) -> str:
    """List L2 scenario blocks."""
    try:
        return json.dumps(_engine.scenarios(), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_tdai_read_scenario(args: dict, **kwargs: Any) -> str:
    """Read a specific L2 scenario block."""
    try:
        path = args.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        # Block path traversal — scenario blocks live under the data dir
        if ".." in path or path.startswith("/") or "\\" in path:
            return json.dumps({"error": "invalid scenario path"})
        return json.dumps(_engine.read_scenario(path), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_tdai_core(args: dict, **kwargs: Any) -> str:
    """Read L3 core memory (persona)."""
    try:
        return json.dumps(_engine.read_core(), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_tdai_write_core(args: dict, **kwargs: Any) -> str:
    """Write L3 core memory (persona)."""
    try:
        content = args.get("content", "")
        if not content:
            return json.dumps({"error": "content is required"})
        return json.dumps(_engine.write_core(content), default=str)


    # ── Slash command handler ──────────────────────────────────────────────────
    except Exception as e:
        return json.dumps({"error": str(e)})


def _cmd_tdai(raw_args: str) -> str:
    """Handle /memory-tdai slash command."""
    parts = raw_args.strip().split(maxsplit=2)
    if not parts:
        return (
            "Usage: /memory-tdai <subcommand> [args]\n"
            "  status                    — gateway + engine status\n"
            "  recall <query> [limit]    — recall from all memory layers\n"
            "  capture <json>            — capture conversation messages\n"
            "  search <query> [limit]    — search L1 memories\n"
            "  conversations <query>     — search L0 conversations\n"
            "  scenarios                 — list L2 scenario blocks\n"
            "  read-scenario <path>      — read a scenario block\n"
            "  core                      — read L3 persona\n"
            "  write-core <content>      — write L3 persona\n"
        )

    subcmd = parts[0].lower()
    try:
        if subcmd == "status":
            return json.dumps(_engine.status(), default=str, indent=2)
        elif subcmd == "recall":
            query = parts[1] if len(parts) > 1 else ""
            if not query:
                return "Usage: /memory-tdai recall <query> [limit]"
            limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
            limit = max(1, min(limit, 20))
            return json.dumps(_engine.recall(query, limit), default=str, indent=2)
        elif subcmd == "capture":
            messages_json = parts[1] if len(parts) > 1 else ""
            if not messages_json:
                return "Usage: /memory-tdai capture <json-messages>"
            try:
                messages = json.loads(messages_json)
            except json.JSONDecodeError:
                return "Invalid JSON messages"
            if not isinstance(messages, list):
                return "messages must be a JSON list"
            for m in messages:
                if not isinstance(m, dict) or not m.get("role") or not m.get("content"):
                    return "each message must be an object with role and content"
            return json.dumps(_engine.capture(messages), default=str, indent=2)
        elif subcmd == "search":
            query = parts[1] if len(parts) > 1 else ""
            if not query:
                return "Usage: /memory-tdai search <query> [limit]"
            limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
            limit = max(1, min(limit, 20))
            return json.dumps(_engine.search_memories(query, limit), default=str, indent=2)
        elif subcmd == "conversations":
            query = parts[1] if len(parts) > 1 else ""
            if not query:
                return "Usage: /memory-tdai conversations <query>"
            return json.dumps(_engine.search_conversations(query), default=str, indent=2)
        elif subcmd == "scenarios":
            return json.dumps(_engine.scenarios(), default=str, indent=2)
        elif subcmd == "read-scenario":
            path = parts[1] if len(parts) > 1 else ""
            if not path:
                return "Usage: /memory-tdai read-scenario <path>"
            return json.dumps(_engine.read_scenario(path), default=str, indent=2)
        elif subcmd == "core":
            return json.dumps(_engine.read_core(), default=str, indent=2)
        elif subcmd == "write-core":
            content = parts[1] if len(parts) > 1 else ""
            if not content:
                return "Usage: /memory-tdai write-core <content>"
            return json.dumps(_engine.write_core(content), default=str, indent=2)
        else:
            return f"Unknown subcommand: {subcmd}"
    except Exception as e:
        return f"Error: {e}"


# ── Plugin entry point ─────────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    """Register the hermes-memory-tdai plugin."""
    logger.info("Registering hermes-memory-tdai plugin")

    # Register tools
    ctx.register_tool(
        name="tdai_status",
        toolset="memory-tdai",
        schema={
            "name": "tdai_status",
            "description": "Check the TencentDB Agent Memory gateway + engine status: ready state, gateway URL, repo path, gateway script, and health.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_tdai_status,
    )

    ctx.register_tool(
        name="tdai_recall",
        toolset="memory-tdai",
        schema={
            "name": "tdai_recall",
            "description": "Recall from all memory layers (L1 atomic memories + L0 conversations) for a query. The primary memory retrieval tool — use when you need to remember past context, decisions, or facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language query for what to recall",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results per layer (1-20)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
        handler=_handle_tdai_recall,
    )

    ctx.register_tool(
        name="tdai_capture",
        toolset="memory-tdai",
        schema={
            "name": "tdai_capture",
            "description": "Capture conversation messages to L0. Stores raw conversation turns for later L1 extraction. Best for: recording important conversations or decisions for future recall.",
            "parameters": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "description": "user or assistant"},
                                "content": {"type": "string", "description": "message content"},
                            },
                            "required": ["role", "content"],
                        },
                        "description": "List of conversation messages",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session identifier",
                        "default": "",
                    },
                },
                "required": ["messages"],
            },
        },
        handler=_handle_tdai_capture,
    )

    ctx.register_tool(
        name="tdai_search",
        toolset="memory-tdai",
        schema={
            "name": "tdai_search",
            "description": "Search L1 structured memories (facts, episodic memories, instructions). Use when you need specific remembered facts, preferences, or instructions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (1-20)",
                        "default": 5,
                    },
                    "type": {
                        "type": "string",
                        "description": "Memory type filter: persona, episodic, or instruction",
                        "default": "",
                    },
                },
                "required": ["query"],
            },
        },
        handler=_handle_tdai_search,
    )

    ctx.register_tool(
        name="tdai_conversations",
        toolset="memory-tdai",
        schema={
            "name": "tdai_conversations",
            "description": "Search L0 raw conversation history. Use to find past conversation content verbatim.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (1-20)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
        handler=_handle_tdai_conversations,
    )

    ctx.register_tool(
        name="tdai_scenarios",
        toolset="memory-tdai",
        schema={
            "name": "tdai_scenarios",
            "description": "List L2 scenario blocks (Markdown scene files). Use to see what contextual scenarios are stored.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_tdai_scenarios,
    )

    ctx.register_tool(
        name="tdai_read_scenario",
        toolset="memory-tdai",
        schema={
            "name": "tdai_read_scenario",
            "description": "Read a specific L2 scenario block by path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Scenario block path (e.g. 工作.md)",
                    },
                },
                "required": ["path"],
            },
        },
        handler=_handle_tdai_read_scenario,
    )

    ctx.register_tool(
        name="tdai_core",
        toolset="memory-tdai",
        schema={
            "name": "tdai_core",
            "description": "Read L3 core memory (persona / user profile synthesis).",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_tdai_core,
    )

    ctx.register_tool(
        name="tdai_write_core",
        toolset="memory-tdai",
        schema={
            "name": "tdai_write_core",
            "description": "Write L3 core memory (persona / user profile). Replaces the current core memory content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "New core memory content (Markdown)",
                    },
                },
                "required": ["content"],
            },
        },
        handler=_handle_tdai_write_core,
    )

    # Register slash command
    ctx.register_command(
        name="memory-tdai",
        description="TencentDB Agent Memory — four-layer memory search/recall/capture",
        handler=_cmd_tdai,
    )

    return {"name": "hermes-memory-tdai"}
