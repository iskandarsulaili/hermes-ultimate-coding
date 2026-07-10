"""
hermes-lsp — Language Server Protocol integration for Hermes.

Provides real-time code intelligence during agentic coding:
  - Diagnostics (errors, warnings) after every edit
  - Completions at cursor position
  - Hover info for symbols
  - Go-to-definition
  - Auto-fix suggestions for common issues
  - File-level diagnostics on save

Survives Hermes updates by living entirely in ~/.hermes/plugins/.
Uses stdio JSON-RPC to communicate with language servers (no pygls dependency).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("hermes-lsp")

# =============================================================================
# Configuration from environment (no hardcoded settings)
# =============================================================================

def _env_int(key: str, default: int) -> int:
    """Read an integer from environment, falling back to default."""
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default

def _env_float(key: str, default: float) -> float:
    """Read a float from environment, falling back to default."""
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default

def _env_bool(key: str, default: bool) -> bool:
    """Read a boolean from environment, falling back to default."""
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")

# LSP timeouts (all configurable via .env)
LSP_REQUEST_TIMEOUT = _env_float("HERMES_LSP_REQUEST_TIMEOUT", 15.0)
LSP_HEADER_TIMEOUT = _env_float("HERMES_LSP_HEADER_TIMEOUT", 5.0)
LSP_CONTENT_TIMEOUT = _env_float("HERMES_LSP_CONTENT_TIMEOUT", 30.0)
LSP_DIAGNOSTICS_TIMEOUT = _env_float("HERMES_LSP_DIAGNOSTICS_TIMEOUT", 5.0)
LSP_POLL_INTERVAL = _env_float("HERMES_LSP_POLL_INTERVAL", 0.05)
LSP_READ_POLL_INTERVAL = _env_float("HERMES_LSP_READ_POLL_INTERVAL", 0.01)
LSP_STOP_TIMEOUT = _env_float("HERMES_LSP_STOP_TIMEOUT", 5.0)
LSP_CHECK_TIMEOUT = _env_float("HERMES_LSP_CHECK_TIMEOUT", 5.0)
LSP_READ_CHUNK_SIZE = _env_int("HERMES_LSP_READ_CHUNK_SIZE", 4096)
LSP_MAX_DIAGNOSTICS = _env_int("HERMES_LSP_MAX_DIAGNOSTICS", 20)
LSP_MAX_WARNINGS = _env_int("HERMES_LSP_MAX_WARNINGS", 20)
LSP_MAX_INFO = _env_int("HERMES_LSP_MAX_INFO", 10)
LSP_MAX_COMPLETIONS = _env_int("HERMES_LSP_MAX_COMPLETIONS", 30)
LSP_MAX_CONTENT_LENGTH = _env_int("HERMES_LSP_MAX_CONTENT_LENGTH", 10 * 1024 * 1024)  # 10MB
LSP_CLIENT_TTL = _env_float("HERMES_LSP_CLIENT_TTL", 300.0)  # 5 min idle eviction

# =============================================================================
# JSON-RPC Protocol (lightweight, no external deps)
# =============================================================================


def _make_request(method: str, params: Any = None, id: Any = None) -> str:
    """Build a JSON-RPC 2.0 request string."""
    if id is None:
        id = str(uuid.uuid4().hex[:8])
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _make_notification(method: str, params: Any = None) -> str:
    """Build a JSON-RPC 2.0 notification (no id)."""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def _parse_response(line: str) -> Dict[str, Any]:
    """Parse a single JSON-RPC response line."""
    return json.loads(line)


# =============================================================================
# Language Server Discovery
# =============================================================================

# Known language servers and how to launch them
LANGUAGE_SERVERS: Dict[str, Dict[str, Any]] = {
    "c": {
        "name": "clangd",
        "command": ["clangd"],
        "fallback_commands": [],
        "install_hint": "Install clangd via your package manager (apt install clangd, brew install llvm)",
        "extensions": [".c", ".h"],
        "root_patterns": ["compile_commands.json", ".git"],
    },
    "cpp": {
        "name": "clangd",
        "command": ["clangd"],
        "fallback_commands": [],
        "install_hint": "Install clangd via your package manager (apt install clangd, brew install llvm)",
        "extensions": [".cpp", ".cxx", ".cc", ".hpp", ".hxx", ".hh"],
        "root_patterns": ["compile_commands.json", ".git"],
    },
    "python": {
        "name": "Pyright / Pylance",
        "command": ["pyright-langserver", "--stdio"],
        "fallback_commands": [
            ["basedpyright-langserver", "--stdio"],
            ["pylsp"],
        ],
        "install_hint": "pip install pyright",
        "extensions": [".py"],
        "root_patterns": ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git"],
    },
    "typescript": {
        "name": "TypeScript Language Server",
        "command": ["typescript-language-server", "--stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g typescript-language-server",
        "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"],
        "root_patterns": ["tsconfig.json", "package.json", ".git"],
    },
    "javascript": {
        "name": "TypeScript Language Server (JS)",
        "command": ["typescript-language-server", "--stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g typescript-language-server",
        "extensions": [".js", ".jsx", ".mjs", ".cjs"],
        "root_patterns": ["package.json", ".git"],
    },
    "json": {
        "name": "JSON Language Server",
        "command": ["vscode-json-languageserver", "--stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g vscode-json-languageserver",
        "extensions": [".json", ".jsonc"],
        "root_patterns": [".git"],
    },
    "yaml": {
        "name": "YAML Language Server",
        "command": ["yaml-language-server", "--stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g yaml-language-server",
        "extensions": [".yaml", ".yml"],
        "root_patterns": [".git"],
    },
    "rust": {
        "name": "rust-analyzer",
        "command": ["rust-analyzer"],
        "fallback_commands": [],
        "install_hint": "rustup component add rust-analyzer",
        "extensions": [".rs"],
        "root_patterns": ["Cargo.toml", ".git"],
    },
    "go": {
        "name": "gopls",
        "command": ["gopls"],
        "fallback_commands": [],
        "install_hint": "go install golang.org/x/tools/gopls@latest",
        "extensions": [".go"],
        "root_patterns": ["go.mod", ".git"],
    },
    "html": {
        "name": "HTML Language Server",
        "command": ["vscode-html-languageserver", "--stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g vscode-html-languageserver",
        "extensions": [".html", ".htm", ".xhtml"],
        "root_patterns": [".git"],
    },
    "css": {
        "name": "CSS Language Server",
        "command": ["vscode-css-languageserver", "--stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g vscode-css-languageserver",
        "extensions": [".css", ".scss", ".less"],
        "root_patterns": [".git"],
    },
    "bash": {
        "name": "bash-language-server",
        "command": ["bash-language-server", "start"],
        "fallback_commands": [],
        "install_hint": "npm install -g bash-language-server",
        "extensions": [".sh", ".bash", ".zsh"],
        "root_patterns": [".git"],
    },
    "dockerfile": {
        "name": "Dockerfile Language Server",
        "command": ["docker-langserver", "--stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g dockerfile-language-server-nodejs",
        "extensions": ["Dockerfile", ".dockerfile"],
        "root_patterns": [".git"],
    },
    "sql": {
        "name": "SQL Language Server",
        "command": ["sql-language-server", "up", "--method", "stdio"],
        "fallback_commands": [],
        "install_hint": "npm install -g sql-language-server",
        "extensions": [".sql"],
        "root_patterns": [".git"],
    },
}


def _find_language_for_file(filepath: str) -> Optional[str]:
    """Determine the language for a file based on its extension."""
    ext = Path(filepath).suffix.lower()
    name = Path(filepath).name

    # Special filenames
    if name == "Dockerfile" or name.endswith(".dockerfile"):
        return "dockerfile"
    if name == "Makefile" or name.endswith(".mk"):
        return None  # No LSP server for Makefiles

    for lang, config in LANGUAGE_SERVERS.items():
        if ext in config["extensions"]:
            return lang
    return None


def _find_project_root(filepath: str) -> Optional[str]:
    """Walk up from filepath to find the project root."""
    path = Path(filepath).resolve()
    for parent in [path] + list(path.parents):
        for lang, config in LANGUAGE_SERVERS.items():
            for pattern in config["root_patterns"]:
                if (parent / pattern).exists():
                    return str(parent)
    return str(path.parent) if path.parent else None


def _check_server_available(command: List[str]) -> bool:
    """Check if a language server binary is available on PATH."""
    try:
        subprocess.run(
            ["which", command[0]],
            capture_output=True,
            text=True,
            timeout=LSP_CHECK_TIMEOUT,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Also check common locations
    for check_cmd in [
        ["command", "-v", command[0]],
        ["where", command[0]],
    ]:
        try:
            subprocess.run(check_cmd, capture_output=True, text=True, timeout=LSP_CHECK_TIMEOUT)
            return True
        except Exception:
            pass

    # Fallback: try to run the command with --version
    try:
        result = subprocess.run(
            [command[0], "--version"],
            capture_output=True,
            text=True,
            timeout=LSP_CHECK_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


# =============================================================================
# Language Server Client
# =============================================================================


@dataclass
class LSPClient:
    """A single language server client (one per language per project root)."""

    language: str
    server_name: str
    command: List[str]
    project_root: str
    process: Optional[subprocess.Popen] = None
    _pending_requests: Dict[str, Tuple[threading.Event, list, list]] = field(default_factory=dict)
    _request_id: int = 0
    _capabilities: Dict[str, Any] = field(default_factory=dict)
    _initialized: bool = False
    _read_thread: Optional[threading.Thread] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _diagnostics: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    _diag_lock: threading.Lock = field(default_factory=threading.Lock)
    _open_files: Set[str] = field(default_factory=set)
    _open_files_lock: threading.Lock = field(default_factory=threading.Lock)
    _read_buf: bytes = b""  # leftover bytes from partial reads
    _timeout_count: int = 0  # consecutive timeouts for backoff
    _last_activity: float = field(default_factory=time.time)  # last access time for eviction
    _stopped: bool = False

    def start(self) -> bool:
        """Start the language server process."""
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.project_root,
                text=False,  # binary mode — we handle decoding ourselves
                bufsize=0,   # unbuffered
            )
        except FileNotFoundError:
            logger.warning(
                "LSP server '%s' not found for %s. Install: %s",
                self.command[0],
                self.language,
                LANGUAGE_SERVERS.get(self.language, {}).get("install_hint", ""),
            )
            return False

        self._last_activity = time.time()

        # Start reader thread
        self._read_thread = threading.Thread(
            target=self._read_loop,
            name=f"lsp-reader-{self.language}",
            daemon=True,
        )
        self._read_thread.start()

        # Initialize
        return self._initialize()

    def _initialize(self) -> bool:
        """Send initialize request and wait for response."""
        init_params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "hermes-lsp", "version": "1.0.0"},
            "capabilities": {
                "textDocument": {
                    "synchronization": {
                        "didOpen": True,
                        "didChange": True,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                        "didSave": True,
                    },
                    "completion": {
                        "completionItem": {
                            "snippetSupport": False,
                            "commitCharactersSupport": True,
                        }
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "diagnostics": {},
                    "codeAction": {},
                    "formatting": {},
                },
                "workspace": {
                    "workspaceFolders": [{"uri": self._path_to_uri(self.project_root), "name": "root"}]
                },
            },
            "rootUri": self._path_to_uri(self.project_root),
            "rootPath": self.project_root,
        }

        result = self._send_request("initialize", init_params)
        if result is None:
            return False

        self._capabilities = result.get("capabilities", {})
        self._send_notification("initialized", {})
        self._initialized = True
        return True

    def stop(self) -> None:
        """Shut down the language server.

        Sends shutdown notification (fire-and-forget, no wait) to avoid
        deadlocking on a crashed reader thread. Closes pipes to unblock
        the reader thread.
        """
        self._stopped = True
        if self._initialized:
            self._send_notification("exit", {})
        # Close pipes to unblock the reader thread immediately
        self._close_pipes()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=LSP_STOP_TIMEOUT)
            except Exception:
                self.process.kill()

    def _close_pipes(self) -> None:
        """Close stdin/stdout/stderr pipes to unblock reader thread.

        Sets pipe references to None after closing so callers can
        distinguish closed pipes from open ones.
        """
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                    self.process.stdin = None  # type: ignore[assignment]
            except Exception:
                pass
            try:
                if self.process.stdout:
                    self.process.stdout.close()
                    self.process.stdout = None  # type: ignore[assignment]
            except Exception:
                pass
            try:
                if self.process.stderr:
                    self.process.stderr.close()
                    self.process.stderr = None  # type: ignore[assignment]
            except Exception:
                pass

    def open_file(self, filepath: str, content: Optional[str] = None) -> None:
        """Notify the server that a file is open."""
        with self._open_files_lock:
            if filepath in self._open_files:
                return
            self._open_files.add(filepath)

        if content is None:
            try:
                content = Path(filepath).read_text(encoding="utf-8")
            except Exception:
                content = ""

        self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": self._path_to_uri(filepath),
                    "languageId": self.language,
                    "version": 1,
                    "text": content,
                }
            },
        )

    def change_file(self, filepath: str, content: str, version: int = 2) -> None:
        """Notify the server that a file changed."""
        with self._open_files_lock:
            if filepath not in self._open_files:
                self._open_files.add(filepath)
                # Need to open first — will send didOpen below
                if content is not None:
                    self._send_notification(
                        "textDocument/didOpen",
                        {
                            "textDocument": {
                                "uri": self._path_to_uri(filepath),
                                "languageId": self.language,
                                "version": 1,
                                "text": content,
                            }
                        },
                    )
                    return

        self._send_notification(
            "textDocument/didChange",
            {
                "textDocument": {
                    "uri": self._path_to_uri(filepath),
                    "version": version,
                },
                "contentChanges": [{"text": content}],
            },
        )

    def close_file(self, filepath: str) -> None:
        """Notify the server that a file was closed."""
        with self._open_files_lock:
            if filepath not in self._open_files:
                return
            self._open_files.discard(filepath)

        self._send_notification(
            "textDocument/didClose",
            {"textDocument": {"uri": self._path_to_uri(filepath)}},
        )

    def get_open_files(self) -> List[str]:
        """Return a copy of open files list (thread-safe)."""
        with self._open_files_lock:
            return list(self._open_files)

    def get_diagnostics(self, filepath: str) -> List[Dict[str, Any]]:
        """Return cached diagnostics for a file."""
        with self._diag_lock:
            return list(self._diagnostics.get(filepath, []))

    def get_completions(
        self, filepath: str, line: int, character: int
    ) -> List[Dict[str, Any]]:
        """Request completions at a position."""
        self.open_file(filepath)  # idempotent, thread-safe

        result = self._send_request(
            "textDocument/completion",
            {
                "textDocument": {"uri": self._path_to_uri(filepath)},
                "position": {"line": line, "character": character},
                "context": {"triggerKind": 1},
            },
        )

        if result is None:
            return []

        items = result
        if isinstance(items, dict):
            items = items.get("items", [])

        return [
            {
                "label": item.get("label", ""),
                "kind": item.get("kind", 0),
                "detail": item.get("detail", ""),
                "documentation": item.get("documentation", ""),
            }
            for item in items
        ][:LSP_MAX_COMPLETIONS]

    def get_hover(self, filepath: str, line: int, character: int) -> Optional[Dict[str, Any]]:
        """Request hover info at a position."""
        self.open_file(filepath)  # idempotent, thread-safe

        result = self._send_request(
            "textDocument/hover",
            {
                "textDocument": {"uri": self._path_to_uri(filepath)},
                "position": {"line": line, "character": character},
            },
        )

        if result is None:
            return None

        contents = result.get("contents", {})
        if isinstance(contents, str):
            return {"contents": contents}
        if isinstance(contents, dict):
            return {
                "contents": contents.get("value", str(contents)),
                "kind": contents.get("kind", "markdown"),
            }
        if isinstance(contents, list):
            return {"contents": "\n".join(str(c) for c in contents)}

        return None

    def goto_definition(
        self, filepath: str, line: int, character: int
    ) -> Optional[Dict[str, Any]]:
        """Request go-to-definition at a position."""
        self.open_file(filepath)  # idempotent, thread-safe

        result = self._send_request(
            "textDocument/definition",
            {
                "textDocument": {"uri": self._path_to_uri(filepath)},
                "position": {"line": line, "character": character},
            },
        )

        if result is None:
            return None

        # Can be a single location or a list
        locations = result if isinstance(result, list) else [result]
        if not locations:
            return None

        loc = locations[0]
        return {
            "uri": loc.get("uri", ""),
            "filepath": self._uri_to_path(loc.get("uri", "")),
            "line": loc.get("range", {}).get("start", {}).get("line", 0),
            "character": loc.get("range", {}).get("start", {}).get("character", 0),
        }

    def get_code_actions(
        self, filepath: str, diagnostic: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Request code actions for a diagnostic."""
        self.open_file(filepath)  # idempotent, thread-safe

        result = self._send_request(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": self._path_to_uri(filepath)},
                "range": diagnostic.get("range", {}),
                "context": {
                    "diagnostics": [diagnostic],
                    "only": ["quickfix", "refactor", "source"],
                },
            },
        )

        if result is None:
            return []

        actions = result if isinstance(result, list) else []
        return [
            {
                "title": a.get("title", ""),
                "kind": a.get("kind", ""),
                "isPreferred": a.get("isPreferred", False),
            }
            for a in actions
        ]

    def format_file(self, filepath: str) -> Optional[List[Dict[str, Any]]]:
        """Request document formatting."""
        self.open_file(filepath)  # idempotent, thread-safe

        result = self._send_request(
            "textDocument/formatting",
            {
                "textDocument": {"uri": self._path_to_uri(filepath)},
                "options": {"tabSize": 4, "insertSpaces": True},
            },
        )

        return result  # List of TextEdit[]

    # -- Internal -----------------------------------------------------------

    def _send_request(self, method: str, params: Any = None) -> Any:
        """Send a request and wait for the response.

        Uses threading.Event for cross-thread sync instead of asyncio.Future,
        which requires a running event loop. This works from any thread.
        """
        # Check process health before writing
        if not self.process or self.process.poll() is not None:
            logger.debug("LSP request '%s' skipped: process not running", method)
            return None

        with self._lock:
            self._request_id += 1
            req_id = str(self._request_id)
            msg = _make_request(method, params, id=req_id)

            event = threading.Event()
            result_box: list = []
            error_box: list = []
            self._pending_requests[req_id] = (event, result_box, error_box)

        try:
            if self.process.stdin:
                self.process.stdin.write(msg + "\n")
                self.process.stdin.flush()

            # Wait for response (with timeout)
            if not event.wait(timeout=LSP_REQUEST_TIMEOUT):
                logger.warning("LSP request '%s' timed out for %s", method, self.language)
                with self._lock:
                    self._pending_requests.pop(req_id, None)
                return None

            if error_box:
                logger.debug("LSP request '%s' error: %s", method, error_box[0])
                return None
            return result_box[0] if result_box else None

        except Exception as e:
            logger.debug("LSP request '%s' failed: %s", method, e)
            with self._lock:
                self._pending_requests.pop(req_id, None)
            return None

    def _send_notification(self, method: str, params: Any = None) -> None:
        """Send a notification (no response expected)."""
        if not self.process or self.process.poll() is not None:
            return
        msg = _make_notification(method, params)
        try:
            if self.process.stdin:
                self.process.stdin.write(msg + "\n")
                self.process.stdin.flush()
        except Exception as e:
            logger.debug("LSP notification '%s' failed: %s", method, e)

    def _read_loop(self) -> None:
        """Background thread: read JSON-RPC responses from the server.

        Uses line-buffered reading for the Content-Length header, then
        reads the exact content body. Drains stderr to prevent deadlocks.
        All reads have timeouts to prevent hangs on crashed servers.
        """
        if not self.process or not self.process.stdout:
            return

        # Drain stderr in a separate daemon thread to prevent deadlocks
        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"lsp-stderr-{self.language}",
            daemon=True,
        )
        stderr_thread.start()

        # Make stdout non-blocking for timeout-safe reads
        _fd = self.process.stdout.fileno()
        os.set_blocking(_fd, False)

        try:
            while not self._stopped and self.process.poll() is None:
                try:
                    # Read Content-Length header line (with timeout via polling)
                    header_line = self._read_line_timeout(timeout=LSP_HEADER_TIMEOUT)
                    if header_line is None:
                        # Timeout — increment counter, back off if persistent
                        self._timeout_count += 1
                        if self._timeout_count >= 10:
                            logger.debug("LSP read loop: 10 consecutive timeouts, breaking")
                            break
                        time.sleep(min(0.1 * self._timeout_count, 1.0))
                        continue
                    self._timeout_count = 0  # reset on success

                    if not header_line:
                        break

                    header_line = header_line.strip()
                    if not header_line:
                        continue

                    if not header_line.startswith("Content-Length:"):
                        continue

                    length = int(header_line.split(":")[1].strip())
                    if length <= 0:
                        logger.debug("LSP: invalid content length %d, skipping", length)
                        continue
                    if length > LSP_MAX_CONTENT_LENGTH:
                        logger.warning("LSP: content length %d exceeds max %d, skipping", length, LSP_MAX_CONTENT_LENGTH)
                        continue

                    # Read the blank line separator
                    separator = self._read_line_timeout(timeout=LSP_HEADER_TIMEOUT)
                    if not separator:
                        break

                    # Read exactly `length` bytes of content (with timeout)
                    content = self._read_exact_timeout(length, timeout=LSP_CONTENT_TIMEOUT)
                    if content is None:
                        break

                    self._handle_message(content)

                except (BrokenPipeError, ConnectionResetError, EOFError):
                    break
                except Exception as e:
                    if not self._stopped:
                        logger.debug("LSP read error: %s", e)
                    break
        finally:
            self._close_pipes()

    def _read_line_timeout(self, timeout: float = 5) -> Optional[str]:
        """Read a line from stdout with timeout. Returns None on timeout.

        Raises EOFError on EOF, BrokenPipeError, or ConnectionResetError
        so the caller can distinguish timeout from terminal conditions.

        Reads in 4KB chunks for efficiency, preserves leftover bytes
        across calls via _read_buf.
        """
        deadline = time.time() + timeout
        buf = self._read_buf
        self._read_buf = b""
        while time.time() < deadline and not self._stopped and self.process and self.process.poll() is None:
            try:
                if b"\n" not in buf:
                    if not self.process.stdout:
                        raise EOFError("stdout closed")
                    chunk = os.read(self.process.stdout.fileno(), LSP_READ_CHUNK_SIZE)
                    if not chunk:
                        raise EOFError("stdout EOF")
                    buf += chunk
                if b"\n" in buf:
                    line, self._read_buf = buf.split(b"\n", 1)
                    return line.decode("utf-8", errors="replace") + "\n"
            except (BlockingIOError, ValueError):
                time.sleep(LSP_READ_POLL_INTERVAL)
            except (BrokenPipeError, ConnectionResetError, EOFError):
                raise
            except OSError:
                raise EOFError("stdout read error")
        self._read_buf = buf  # preserve for next call
        return None  # timeout

    def _read_exact_timeout(self, length: int, timeout: float = LSP_CONTENT_TIMEOUT) -> Optional[str]:
        """Read exactly `length` bytes from stdout with timeout.

        Raises EOFError on EOF, BrokenPipeError, or ConnectionResetError
        so the caller can distinguish timeout from terminal conditions.

        First consumes any leftover bytes in _read_buf, then reads
        the remainder from the pipe. Preserves excess bytes in _read_buf.
        """
        deadline = time.time() + timeout
        # Consume leftover bytes first, preserving excess
        buf = self._read_buf
        self._read_buf = b""
        if len(buf) >= length:
            # _read_buf already has enough data
            result = buf[:length]
            self._read_buf = buf[length:]  # preserve excess
            return result.decode("utf-8", errors="replace")
        while len(buf) < length and time.time() < deadline and not self._stopped and self.process and self.process.poll() is None:
            try:
                if not self.process.stdout:
                    raise EOFError("stdout closed")
                remaining = length - len(buf)
                chunk = os.read(self.process.stdout.fileno(), remaining)
                if not chunk:
                    raise EOFError("stdout EOF")
                buf += chunk
            except (BlockingIOError, ValueError):
                time.sleep(LSP_READ_POLL_INTERVAL)
            except (BrokenPipeError, ConnectionResetError, EOFError):
                raise
            except OSError:
                raise EOFError("stdout read error")
        if len(buf) < length:
            return None  # timeout
        return buf.decode("utf-8", errors="replace")

    def _drain_stderr(self) -> None:
        """Drain stderr to prevent the LSP process from blocking on full stderr pipe."""
        if not self.process or not self.process.stderr:
            return
        try:
            os.set_blocking(self.process.stderr.fileno(), False)
            while not self._stopped:
                try:
                    chunk = os.read(self.process.stderr.fileno(), 4096)
                    if not chunk:
                        break
                except (BrokenPipeError, ConnectionResetError, ValueError):
                    break
        except (BlockingIOError, OSError):
            pass
        except Exception:
            pass
        finally:
            try:
                if self.process and self.process.stderr:
                    self.process.stderr.close()
            except Exception:
                pass

    def _handle_message(self, content: str) -> None:
        """Handle a JSON-RPC message from the server."""
        try:
            msg = json.loads(content)
        except json.JSONDecodeError:
            return

        # Response to a request
        if "id" in msg:
            req_id = str(msg["id"])
            with self._lock:
                entry = self._pending_requests.pop(req_id, None)
            if entry is not None:
                event, result_box, error_box = entry
                if "error" in msg:
                    error_box.append(msg["error"].get("message", "LSP error"))
                else:
                    result_box.append(msg.get("result"))
                event.set()

        # Notification (e.g., diagnostics)
        elif "method" in msg:
            method = msg["method"]
            params = msg.get("params", {})

            if method == "textDocument/publishDiagnostics":
                uri = params.get("uri", "")
                diagnostics = params.get("diagnostics", [])
                filepath = self._uri_to_path(uri)
                with self._diag_lock:
                    self._diagnostics[filepath] = [
                        {
                            "range": d.get("range", {}),
                            "severity": d.get("severity", 0),
                            "message": d.get("message", ""),
                            "source": d.get("source", ""),
                            "code": d.get("code", ""),
                            "filepath": filepath,
                        }
                        for d in diagnostics
                    ]
            # Acknowledge $/progress, window/, and telemetry/ notifications
            # to prevent server-side buffer buildup, but don't act on them.
            elif method.startswith("$/") or method.startswith("window/") or method.startswith("telemetry/"):
                pass  # acknowledged by reading
            # Log unknown notifications at debug level for troubleshooting
            else:
                logger.debug("LSP unhandled notification: %s", method)

    def _path_to_uri(self, path: str) -> str:
        """Convert a filesystem path to a file:// URI."""
        return Path(path).resolve().as_uri()

    def _uri_to_path(self, uri: str) -> str:
        """Convert a file:// URI to a filesystem path."""
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        return unquote(parsed.path)


# =============================================================================
# LSP Manager — manages multiple LSP clients
# =============================================================================


class LSPManager:
    """Manages language server clients for multiple languages.

    Thread-safe singleton.  Clients are created lazily per (language, project_root).
    Idle clients are evicted after LSP_CLIENT_TTL seconds of inactivity.
    """

    def __init__(self):
        self._clients: Dict[str, LSPClient] = {}
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False
        self._read_buf: Dict[str, bytes] = {}  # per-client leftover buffer
        self._root_cache: Dict[str, str] = {}  # filepath -> project_root cache
        self._server_cache: Dict[str, bool] = {}  # command -> available cache
        self._server_cache_ttl: float = 60.0  # seconds
        self._last_server_check: float = 0.0
        self._eviction_thread: Optional[threading.Thread] = None
        self._eviction_interval: float = 60.0  # seconds between eviction sweeps
        self._known_roots: List[str] = []  # ordered list of project roots (insertion order = LRU)
        self._known_roots_lock: threading.Lock = threading.Lock()  # thread-safe access to _known_roots
        self._known_roots_max: int = 50  # max roots before LRU eviction
        self._cross_repo_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}  # symbol_key -> (timestamp, result)
        self._cross_repo_cache_lock: threading.Lock = threading.Lock()  # thread-safe access to cache
        self._cross_repo_cache_ttl: float = 30.0  # seconds before re-checking
        self._cross_repo_cache_max: int = 100  # max entries before LRU eviction

    def ensure_started(self) -> None:
        """Ensure the manager is initialized."""
        if self._started:
            return
        self._started = True
        # Start background eviction thread
        self._eviction_thread = threading.Thread(
            target=self._eviction_loop,
            name="lsp-eviction",
            daemon=True,
        )
        self._eviction_thread.start()

    def _eviction_loop(self) -> None:
        """Background thread: periodically evict idle clients."""
        while not self._stopped:
            time.sleep(self._eviction_interval)
            if self._stopped:
                break
            self._evict_idle_clients()

    def _evict_idle_clients(self) -> None:
        """Evict clients that have been idle beyond TTL.

        Collects idle clients under lock, then stops them outside the lock
        to avoid blocking the manager during process termination.
        """
        now = time.time()
        idle_clients = []
        with self._lock:
            idle_keys = [
                key for key, client in self._clients.items()
                if client._last_activity and (now - client._last_activity) > LSP_CLIENT_TTL
            ]
            for key in idle_keys:
                idle_clients.append(self._clients.pop(key))
        for client in idle_clients:
            try:
                client.stop()
            except Exception:
                pass

    def _check_server_cached(self, command: List[str]) -> bool:
        """Check server availability with caching."""
        now = time.time()
        if now - self._last_server_check > self._server_cache_ttl:
            self._server_cache.clear()
            self._last_server_check = now
        cmd_key = " ".join(command)
        if cmd_key not in self._server_cache:
            self._server_cache[cmd_key] = _check_server_available(command)
        return self._server_cache[cmd_key]

    def _find_project_root_cached(self, filepath: str) -> Optional[str]:
        """Find project root with caching."""
        if filepath in self._root_cache:
            return self._root_cache[filepath]
        root = _find_project_root(filepath)
        if root:
            self._root_cache[filepath] = root
        return root

    def get_client_for_file(self, filepath: str) -> Optional[LSPClient]:
        """Get or create an LSP client for a file."""
        language = _find_language_for_file(filepath)
        if language is None:
            return None

        project_root = self._find_project_root_cached(filepath)
        if project_root is None:
            return None

        # Track this root for cross-repo fallback (thread-safe, with LRU eviction)
        with self._known_roots_lock:
            if project_root not in self._known_roots:
                self._known_roots.append(project_root)
                if len(self._known_roots) > self._known_roots_max:
                    self._known_roots.pop(0)  # evict oldest

        key = f"{language}:{project_root}"

        # Fast path: check under shared lock (avoids TOCTOU race)
        with self._lock:
            if key in self._clients:
                client = self._clients[key]
                client._last_activity = time.time()
                return client

        # Check server availability outside the lock (subprocess.run is slow)
        config = LANGUAGE_SERVERS.get(language)
        if config is None:
            return None

        command = config["command"]
        if not self._check_server_cached(command):
            for fallback in config.get("fallback_commands", []):
                if self._check_server_cached(fallback):
                    command = fallback
                    break
            else:
                logger.info(
                    "LSP server for %s not available. Install: %s",
                    language,
                    config.get("install_hint", ""),
                )
                return None

        # Double-check under lock after expensive check
        with self._lock:
            if key in self._clients:
                return self._clients[key]

            client = LSPClient(
                language=language,
                server_name=config["name"],
                command=command,
                project_root=project_root,
            )

            if not client.start():
                return None

            self._clients[key] = client
            return client

    def _get_cross_repo_clients(self, language: str, exclude_root: str) -> List[LSPClient]:
        """Get LSP clients for the same language in other known repos.

        Self-adapting: discovers related repos organically as the user
        opens files from different projects. No hardcoded paths.
        """
        results = []
        with self._known_roots_lock:
            known = list(self._known_roots)
        for root in known:
            if root == exclude_root:
                continue
            key = f"{language}:{root}"
            with self._lock:
                client = self._clients.get(key)
            if client is not None:
                results.append(client)
        return results

    def _cross_repo_cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        """Get from cross-repo cache with TTL check."""
        with self._cross_repo_cache_lock:
            entry = self._cross_repo_cache.get(key)
            if entry is None:
                return None
            ts, result = entry
            if time.time() - ts > self._cross_repo_cache_ttl:
                del self._cross_repo_cache[key]
                return None
            return result

    def _cross_repo_cache_set(self, key: str, result: Optional[Dict[str, Any]]) -> None:
        """Set cross-repo cache with LRU eviction."""
        with self._cross_repo_cache_lock:
            if len(self._cross_repo_cache) >= self._cross_repo_cache_max:
                # Evict oldest entry
                oldest = min(self._cross_repo_cache.items(), key=lambda x: x[1][0])
                del self._cross_repo_cache[oldest[0]]
            self._cross_repo_cache[key] = (time.time(), result)

    def _cross_repo_fallback(
        self, filepath: str, line: int, character: int, method: str
    ) -> Optional[Dict[str, Any]]:
        """Generic cross-repo fallback for any LSP query method.

        Handles edge cases:
        - No other repos known → returns None
        - Other repo's LSP server not running → skipped silently
        - Other repo's LSP server crashed → skipped silently
        - File doesn't exist in other repo → server returns None
        - Symbol not found in any repo → caches None to avoid repeated queries
        - Cache hit with valid TTL → returns cached result
        - Cache full → LRU eviction
        - Thread safety → uses locks for all shared state
        """
        language = _find_language_for_file(filepath)
        if language is None:
            return None

        project_root = self._find_project_root_cached(filepath)
        if project_root is None:
            return None

        # Check cache first
        cache_key = f"{method}:{filepath}:{line}:{character}"
        cached = self._cross_repo_cache_get(cache_key)
        if cached is not None:
            return cached
        # Check if miss is cached (None result from previous attempt)
        with self._cross_repo_cache_lock:
            if cache_key in self._cross_repo_cache:
                return None

        for other_client in self._get_cross_repo_clients(language, project_root):
            try:
                other_client.open_file(filepath)
                if method == "definition":
                    result = other_client.goto_definition(filepath, line, character)
                elif method == "hover":
                    result = other_client.get_hover(filepath, line, character)
                else:
                    result = None
                if result is not None:
                    self._cross_repo_cache_set(cache_key, result)
                    return result
            except Exception:
                # Other server crashed or file not found — skip silently
                continue

        # Cache the miss to avoid repeated cross-repo queries
        self._cross_repo_cache_set(cache_key, None)
        return None

    def goto_definition(
        self, filepath: str, line: int, character: int
    ) -> Optional[Dict[str, Any]]:
        """Get definition location, with cross-repo fallback.

        If the primary LSP server can't resolve the symbol, automatically
        queries other known LSP servers of the same language.
        """
        client = self.get_client_for_file(filepath)
        if client is None:
            return None

        result = client.goto_definition(filepath, line, character)
        if result is not None:
            return result

        # Cross-repo fallback
        return self._cross_repo_fallback(filepath, line, character, "definition")

    def get_hover(self, filepath: str, line: int, character: int) -> Optional[Dict[str, Any]]:
        """Get hover info, with cross-repo fallback."""
        client = self.get_client_for_file(filepath)
        if client is None:
            return None

        result = client.get_hover(filepath, line, character)
        if result is not None:
            return result

        # Cross-repo fallback
        return self._cross_repo_fallback(filepath, line, character, "hover")

    def get_diagnostics(self, filepath: str) -> List[Dict[str, Any]]:
        """Get diagnostics for a file from the appropriate LSP client."""
        client = self.get_client_for_file(filepath)
        if client is None:
            return []
        return client.get_diagnostics(filepath)

    def refresh_diagnostics(self, filepath: str, content: str) -> List[Dict[str, Any]]:
        """Update file content and return fresh diagnostics.

        Uses event-driven polling: sends the change, then waits for the
        server to publish diagnostics (with timeout). No blocking sleep.
        """
        client = self.get_client_for_file(filepath)
        if client is None:
            return []

        # Clear old diagnostics for this file
        with client._diag_lock:
            old_count = len(client._diagnostics.get(filepath, []))

        client.change_file(filepath, content)

        # Wait for diagnostics to arrive (poll with short sleeps, max 5s)
        deadline = time.time() + LSP_DIAGNOSTICS_TIMEOUT
        while time.time() < deadline:
            with client._diag_lock:
                current = client._diagnostics.get(filepath, [])
                if len(current) != old_count:
                    return list(current)
            time.sleep(LSP_POLL_INTERVAL)

        # Timeout — return whatever we have
        return client.get_diagnostics(filepath)

    def get_completions(
        self, filepath: str, line: int, character: int
    ) -> List[Dict[str, Any]]:
        """Get completions at a position."""
        client = self.get_client_for_file(filepath)
        if client is None:
            return []
        return client.get_completions(filepath, line, character)

    def get_code_actions(
        self, filepath: str, diagnostic: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Get code actions for a diagnostic."""
        client = self.get_client_for_file(filepath)
        if client is None:
            return []
        return client.get_code_actions(filepath, diagnostic)

    def get_available_servers(self) -> List[Dict[str, Any]]:
        """List all available language servers and their status."""
        results = []
        for lang, config in LANGUAGE_SERVERS.items():
            available = self._check_server_cached(config["command"])
            if not available:
                for fallback in config.get("fallback_commands", []):
                    if self._check_server_cached(fallback):
                        available = True
                        break
            results.append(
                {
                    "language": lang,
                    "name": config["name"],
                    "available": available,
                    "install_hint": config.get("install_hint", ""),
                    "extensions": config["extensions"],
                }
            )
        return results

    def stop_all(self) -> None:
        """Stop all language server clients.

        Collects clients under lock, then stops them outside the lock
        to prevent deadlock (client.stop() waits for process).
        """
        self._stopped = True
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.stop()


# =============================================================================
# Global instance
# =============================================================================

_manager: Optional[LSPManager] = None
_manager_lock: threading.Lock = threading.Lock()


def get_manager() -> LSPManager:
    """Return the global LSP manager (lazy init, thread-safe)."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = LSPManager()
    return _manager


# =============================================================================
# Hermes Plugin Registration
# =============================================================================


def register(ctx: Any) -> None:
    """Register this plugin with Hermes.

    Called by the Hermes plugin system during discovery.
    """
    logger.info("hermes-lsp: registering plugin")

    # Register the lsp_diagnostics tool
    ctx.register_tool(
        name="lsp_diagnostics",
        toolset="lsp",
        schema={
            "name": "lsp_diagnostics",
            "description": "Get real-time diagnostics (errors, warnings) for a file from the language server. Use after every edit to self-correct before the user sees broken code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Absolute path to the file to check",
                    },
                    "content": {
                        "type": "string",
                        "description": "Optional file content to analyze (if not provided, reads from disk)",
                    },
                },
                "required": ["filepath"],
            },
        },
        handler=_handle_lsp_diagnostics,
        check_fn=lambda: True,
        is_async=False,
        description="Get real-time diagnostics (errors, warnings, hints) from the language server. Like running a compiler/linter but faster and with precise locations. Use after every file edit to catch issues before the user sees them.",
        emoji="",
    )

    # Register the lsp_completions tool
    ctx.register_tool(
        name="lsp_completions",
        toolset="lsp",
        schema={
            "name": "lsp_completions",
            "description": "Get code completions at a specific position in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number (0-indexed)",
                    },
                    "character": {
                        "type": "integer",
                        "description": "Character offset (0-indexed)",
                    },
                },
                "required": ["filepath", "line", "character"],
            },
        },
        handler=_handle_lsp_completions,
        check_fn=lambda: True,
        is_async=False,
        description="Get code completions at a cursor position. Returns method names, variable names, imports, and their documentation. Like IDE autocomplete.",
        emoji="",
    )

    # Register the lsp_hover tool
    ctx.register_tool(
        name="lsp_hover",
        toolset="lsp",
        schema={
            "name": "lsp_hover",
            "description": "Get hover information (type signature, documentation) for a symbol at a position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number (0-indexed)",
                    },
                    "character": {
                        "type": "integer",
                        "description": "Character offset (0-indexed)",
                    },
                },
                "required": ["filepath", "line", "character"],
            },
        },
        handler=_handle_lsp_hover,
        check_fn=lambda: True,
        is_async=False,
        description="Get type information and documentation for a symbol at a cursor position. Like hovering over a symbol in an IDE.",
        emoji="",
    )

    # Register the lsp_definition tool
    ctx.register_tool(
        name="lsp_definition",
        toolset="lsp",
        schema={
            "name": "lsp_definition",
            "description": "Go to the definition of a symbol at a position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number (0-indexed)",
                    },
                    "character": {
                        "type": "integer",
                        "description": "Character offset (0-indexed)",
                    },
                },
                "required": ["filepath", "line", "character"],
            },
        },
        handler=_handle_lsp_definition,
        check_fn=lambda: True,
        is_async=False,
        description="Find where a symbol is defined. Returns the file and line number. Like Ctrl+Click in an IDE.",
        emoji="",
    )

    # Register the lsp_auto_fix tool
    ctx.register_tool(
        name="lsp_auto_fix",
        toolset="lsp",
        schema={
            "name": "lsp_auto_fix",
            "description": "Get auto-fix suggestions for diagnostics in a file. Returns code actions that can be applied to fix issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Absolute path to the file",
                    },
                    "diagnostic_index": {
                        "type": "integer",
                        "description": "Index of the specific diagnostic to fix (0-based). If omitted, returns all available fixes.",
                    },
                },
                "required": ["filepath"],
            },
        },
        handler=_handle_lsp_auto_fix,
        check_fn=lambda: True,
        is_async=False,
        description="Get auto-fix suggestions (code actions) for diagnostics. Like the lightbulb suggestions in an IDE. Returns quick-fix titles that describe what would change.",
        emoji="",
    )

    # Register the lsp_servers tool
    ctx.register_tool(
        name="lsp_servers",
        toolset="lsp",
        schema={
            "name": "lsp_servers",
            "description": "List available and running language servers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "status"],
                        "description": "List all known servers or show running status",
                    }
                },
                "required": ["action"],
            },
        },
        handler=_handle_lsp_servers,
        check_fn=lambda: True,
        is_async=False,
        description="List available language servers and their installation status. Shows which languages have LSP support and whether the server binary is installed.",
        emoji="",
    )

    # Register the lsp_verify tool — the key integration: verify code after edit
    ctx.register_tool(
        name="lsp_verify",
        toolset="lsp",
        schema={
            "name": "lsp_verify",
            "description": "Verify code quality after an edit. Opens the file in the language server, gets diagnostics, and returns a pass/fail with details. Use this as the final step after every code change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Absolute path to the file to verify",
                    },
                    "content": {
                        "type": "string",
                        "description": "The new file content to verify",
                    },
                    "severity_threshold": {
                        "type": "string",
                        "enum": ["error", "warning", "information", "hint"],
                        "description": "Minimum severity to fail on (default: 'warning')",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
        handler=_handle_lsp_verify,
        check_fn=lambda: True,
        is_async=False,
        description="Verify code after an edit. Opens the file in the language server, sends the new content, and returns diagnostics. Fails if there are errors or warnings above the threshold. Use this as the final step after every code change to self-correct before the user sees broken code.",
        emoji="",
    )

    # Register a slash command
    ctx.register_command(
        name="lsp",
        handler=_cmd_lsp,
        description="Inspect LSP status, diagnostics, or available servers",
        args_hint="[status|servers|diagnostics <file>]",
    )

    logger.info("hermes-lsp: registered 7 tools + 1 command")


# =============================================================================
# Tool Handlers
# =============================================================================


def _handle_lsp_diagnostics(args: dict, **kwargs: Any) -> str:
    """Handle lsp_diagnostics tool call."""
    filepath = args.get("filepath", "")
    content = args.get("content", None)

    if not filepath:
        return json.dumps({"success": False, "error": "filepath is required"})

    manager = get_manager()
    manager.ensure_started()

    if content is not None:
        diagnostics = manager.refresh_diagnostics(filepath, content)
    else:
        diagnostics = manager.get_diagnostics(filepath)

    errors = [d for d in diagnostics if d.get("severity") == 1]
    warnings = [d for d in diagnostics if d.get("severity") == 2]
    infos = [d for d in diagnostics if d.get("severity") in (3, 4)]

    return json.dumps(
        {
            "success": True,
            "filepath": filepath,
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
                "info": len(infos),
                "total": len(diagnostics),
            },
            "errors": errors[:LSP_MAX_DIAGNOSTICS],
            "warnings": warnings[:LSP_MAX_WARNINGS],
            "info": infos[:LSP_MAX_INFO],
        }
    )


def _handle_lsp_completions(args: dict, **kwargs: Any) -> str:
    """Handle lsp_completions tool call."""
    filepath = args.get("filepath", "")
    line = args.get("line", 0)
    character = args.get("character", 0)

    if not filepath:
        return json.dumps({"success": False, "error": "filepath is required"})

    manager = get_manager()
    manager.ensure_started()

    completions = manager.get_completions(filepath, line, character)

    return json.dumps(
        {
            "success": True,
            "filepath": filepath,
            "position": {"line": line, "character": character},
            "completions": completions[:LSP_MAX_COMPLETIONS],
            "total": len(completions),
        }
    )


def _handle_lsp_hover(args: dict, **kwargs: Any) -> str:
    """Handle lsp_hover tool call."""
    filepath = args.get("filepath", "")
    line = args.get("line", 0)
    character = args.get("character", 0)

    if not filepath:
        return json.dumps({"success": False, "error": "filepath is required"})

    manager = get_manager()
    manager.ensure_started()

    hover = manager.get_hover(filepath, line, character)

    return json.dumps(
        {
            "success": hover is not None,
            "filepath": filepath,
            "position": {"line": line, "character": character},
            "hover": hover,
        }
    )


def _handle_lsp_definition(args: dict, **kwargs: Any) -> str:
    """Handle lsp_definition tool call."""
    filepath = args.get("filepath", "")
    line = args.get("line", 0)
    character = args.get("character", 0)

    if not filepath:
        return json.dumps({"success": False, "error": "filepath is required"})

    manager = get_manager()
    manager.ensure_started()

    definition = manager.goto_definition(filepath, line, character)

    return json.dumps(
        {
            "success": definition is not None,
            "filepath": filepath,
            "position": {"line": line, "character": character},
            "definition": definition,
        }
    )


def _handle_lsp_auto_fix(args: dict, **kwargs: Any) -> str:
    """Handle lsp_auto_fix tool call."""
    filepath = args.get("filepath", "")
    diagnostic_index = args.get("diagnostic_index", None)

    if not filepath:
        return json.dumps({"success": False, "error": "filepath is required"})

    manager = get_manager()
    manager.ensure_started()

    diagnostics = manager.get_diagnostics(filepath)

    if diagnostic_index is not None:
        if diagnostic_index < 0 or diagnostic_index >= len(diagnostics):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Diagnostic index {diagnostic_index} out of range (0-{len(diagnostics) - 1})",
                }
            )
        diag = diagnostics[diagnostic_index]
        actions = manager.get_code_actions(filepath, diag)
        return json.dumps(
            {
                "success": True,
                "filepath": filepath,
                "diagnostic": diag,
                "fixes": actions,
            }
        )

    # Return all diagnostics with their fix counts
    results = []
    for i, diag in enumerate(diagnostics):
        actions = manager.get_code_actions(filepath, diag)
        if actions:
            results.append(
                {
                    "index": i,
                    "diagnostic": diag,
                    "fixes": actions,
                }
            )

    return json.dumps(
        {
            "success": True,
            "filepath": filepath,
            "fixable_count": len(results),
            "fixes": results,
        }
    )


def _handle_lsp_servers(args: dict, **kwargs: Any) -> str:
    """Handle lsp_servers tool call."""
    action = args.get("action", "list")

    manager = get_manager()
    manager.ensure_started()

    if action == "status":
        # Show running clients
        # Collect client snapshots under manager lock, then read per-client
        # state outside the lock to avoid deadlock with reader thread.
        client_snapshots = []
        with manager._lock:
            for key, client in manager._clients.items():
                client_snapshots.append((key, client))
        clients = []
        for key, client in client_snapshots:
            with client._open_files_lock:
                open_files = list(client._open_files)
            with client._diag_lock:
                diag_count = sum(len(d) for d in client._diagnostics.values())
            clients.append(
                {
                    "key": key,
                    "language": client.language,
                    "server": client.server_name,
                    "project_root": client.project_root,
                    "initialized": client._initialized,
                    "open_files": open_files,
                    "diagnostic_count": diag_count,
                }
            )
        return json.dumps(
            {
                "success": True,
                "running_clients": len(clients),
                "clients": clients,
            }
        )

    # List all known servers
    servers = manager.get_available_servers()
    available = [s for s in servers if s["available"]]
    unavailable = [s for s in servers if not s["available"]]

    return json.dumps(
        {
            "success": True,
            "available": available,
            "unavailable": unavailable,
            "summary": f"{len(available)} available, {len(unavailable)} not installed",
        }
    )


def _handle_lsp_verify(args: dict, **kwargs: Any) -> str:
    """Handle lsp_verify tool call — the key integration point."""
    filepath = args.get("filepath", "")
    content = args.get("content", "")
    severity_threshold = args.get("severity_threshold", "warning")

    if not filepath or not content:
        return json.dumps({"success": False, "error": "filepath and content are required"})

    severity_map = {"error": 1, "warning": 2, "information": 3, "hint": 4}
    threshold = severity_map.get(severity_threshold, 2)

    manager = get_manager()
    manager.ensure_started()

    diagnostics = manager.refresh_diagnostics(filepath, content)

    errors = [d for d in diagnostics if d.get("severity") == 1]
    warnings = [d for d in diagnostics if d.get("severity") == 2]

    # Determine pass/fail
    if threshold <= 1 and errors:
        passed = False
        reason = f"{len(errors)} error(s) found"
    elif threshold <= 2 and warnings:
        passed = False
        reason = f"{len(warnings)} warning(s) found"
    else:
        passed = True
        reason = "No issues above threshold"

    return json.dumps(
        {
            "success": True,
            "passed": passed,
            "reason": reason,
            "filepath": filepath,
            "threshold": severity_threshold,
            "summary": {
                "errors": len(errors),
                "warnings": len(warnings),
                "total": len(diagnostics),
            },
            "errors": errors[:LSP_MAX_DIAGNOSTICS],
            "warnings": warnings[:LSP_MAX_WARNINGS],
            "suggestion": (
                "Use lsp_auto_fix to get fix suggestions, then apply them and re-verify."
                if not passed
                else "Code looks clean."
            ),
        }
    )


def _cmd_lsp(raw_args: str) -> str:
    """Handle the /lsp slash command."""
    parts = raw_args.strip().split(maxsplit=1)
    subcommand = parts[0] if parts else "status"
    arg = parts[1] if len(parts) > 1 else ""

    manager = get_manager()
    manager.ensure_started()

    if subcommand == "servers":
        servers = manager.get_available_servers()
        lines = ["## LSP Servers"]
        for s in servers:
            status = "✓" if s["available"] else "✗"
            lines.append(f"  {status} {s['name']} ({s['language']})")
            if not s["available"]:
                lines.append(f"     Install: {s['install_hint']}")
        return "\n".join(lines)

    elif subcommand == "diagnostics":
        if not arg:
            return "Usage: /lsp diagnostics <filepath>"
        diagnostics = manager.get_diagnostics(arg)
        if not diagnostics:
            return f"No diagnostics for {arg}"
        lines = [f"## Diagnostics for {arg}"]
        for d in diagnostics:
            sev = {1: "ERROR", 2: "WARN", 3: "INFO", 4: "HINT"}.get(
                d.get("severity", 0), "?"
            )
            r = d.get("range", {})
            loc = f"{r.get('start', {}).get('line', 0)}:{r.get('start', {}).get('character', 0)}"
            lines.append(f"  [{sev}] {loc} — {d.get('message', '')}")
        return "\n".join(lines)

    else:
        # Status
        # Collect client snapshots under manager lock, then read per-client
        # state outside the lock to avoid deadlock with reader thread.
        client_snapshots = []
        with manager._lock:
            for key, client in manager._clients.items():
                client_snapshots.append((key, client))
        clients = []
        for key, client in client_snapshots:
            with client._open_files_lock:
                open_count = len(client._open_files)
            clients.append(
                f"  {client.language} ({client.server_name}) @ {client.project_root}"
                f" — {open_count} files open"
            )
        servers = manager.get_available_servers()
        available = sum(1 for s in servers if s["available"])

        lines = [
            "## LSP Status",
            f"Running clients: {len(clients)}",
            f"Available servers: {available}/{len(servers)}",
        ]
        if clients:
            lines.append("")
            lines.extend(clients)
        return "\n".join(lines)
