"""
_shared/deps.py — JIT dependency management for all hermes-ultimate-coding plugins.

Generic framework supporting pip, npm, go, apt, rustup, brew, and any
other package manager.  Each plugin declares its dependencies as
``DepSpec`` entries and calls ``ensure_deps()`` at registration time.

**Zero manual steps** — if a dep is missing or too old, it is installed
or upgraded automatically.  If installation fails, the plugin loads with
degraded functionality instead of crashing Hermes.

Usage in a plugin::

    from _shared.deps import DepSpec, ensure_deps

    _DEPS = [
        DepSpec("semble", ["python3", "-c", "import semble"],
                install=[sys.executable, "-m", "pip", "install", "semble"],
                purpose="semantic code search"),
    ]

    def register(ctx):
        ensure_deps("my-plugin", _DEPS)
        ...
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("hermes-deps")

# ---------------------------------------------------------------------------
# DepSpec — one format for any package manager
# ---------------------------------------------------------------------------
# ``check`` / ``install`` / ``version_check`` can be either:
#   * ``list[str]`` — executed directly via ``subprocess.run`` (safe, no shell)
#   * ``str``       — executed via ``shell=True`` (needed for pipes / redirects)


@dataclass
class DepSpec:
    name: str
    check: list[str] | str
    install: list[str] | str | None = None
    purpose: str = ""
    version: str | None = None
    version_check: list[str] | str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_verified_plugins: set[str] = set()  # tracks which plugins have run deps check
_verified_lock = threading.Lock()   # guard for _verified_plugins (thread-safe)


def _run_cmd(
    args: list[str] | str,
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a shell command and capture its output.

    * ``list[str]`` → direct ``subprocess.run`` (no shell)
    * ``str``       → ``shell=True`` (required for pipes, redirects)

    Kills the child process on timeout to prevent orphan processes.
    Always uses ``text=True`` so stdout/stderr are ``str``, not ``bytes``.
    """
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if isinstance(args, str):
        kwargs["shell"] = True
        proc = subprocess.Popen(args, **kwargs)
    else:
        proc = subprocess.Popen(args, **kwargs)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise

    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def _stream_cmd(args: list[str] | str, label: str = "  deps", timeout: int = 300) -> None:
    """Run a command and stream its output to stderr in real time.

    Reads output line-by-line as the process runs (not buffered until
    completion), so the user sees pip download bars, apt progress, etc.
    as they happen.

    Raises ``RuntimeError`` on non-zero exit or timeout.
    """
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True}
    if isinstance(args, str):
        kwargs["shell"] = True
        proc = subprocess.Popen(args, **kwargs)
    else:
        proc = subprocess.Popen(args, **kwargs)

    if proc.stdout is None:
        # PIPE was requested but Popen couldn't open it (OOM, etc.)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"command exited {proc.returncode} (no output pipe)")
        return

    # Read output line-by-line in real time via a daemon reader thread
    import queue, threading as _thr
    q: queue.Queue = queue.Queue()
    sentinel = object()

    def _reader(stream):
        try:
            for line in iter(stream.readline, ""):
                q.put(line)
        finally:
            q.put(sentinel)
            stream.close()

    reader = _thr.Thread(target=_reader, args=(proc.stdout,), daemon=True)
    reader.start()

    try:
        while True:
            try:
                item = q.get(timeout=timeout)
            except queue.Empty:
                proc.kill()
                raise RuntimeError(f"command timed out after {timeout}s")
            if item is sentinel:
                break
            if item.strip():
                print(f"{label}   {item.rstrip()}", file=sys.stderr, flush=True)
    finally:
        reader.join(timeout=5)
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"command {' '.join(args) if isinstance(args, list) else args} exited {proc.returncode}")


def _parse_version(ver: str) -> tuple[int, ...]:
    """Parse ``"1.2.3"`` → ``(1, 2, 3)``.  Ignores leading non-digits."""
    import re
    nums = re.findall(r"\d+", ver)
    return tuple(int(n) for n in nums) if nums else (0,)


def _check_version_meets(installed_raw: str, requirement: str) -> tuple[bool, str]:
    """Check ``installed_raw`` against ``requirement`` (e.g. ``">=1.2.3"``).

    Returns ``(ok, message)``.
    """
    installed = _parse_version(installed_raw)

    op = "=="
    req_str = requirement.strip()
    for possible in (">=", "<=", "!=", ">", "<", "=="):
        if req_str.startswith(possible):
            op = possible
            req_str = req_str[len(possible):].strip()
            break

    required = _parse_version(req_str)

    if op == ">=":
        ok = installed >= required
    elif op == "<=":
        ok = installed <= required
    elif op == ">":
        ok = installed > required
    elif op == "<":
        ok = installed < required
    elif op == "!=":
        ok = installed != required
    else:
        ok = installed == required

    if ok:
        return True, f"{'.'.join(map(str, installed))} meets {requirement}"
    return False, f"{'.'.join(map(str, installed))} does NOT meet {requirement}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def ensure_deps(plugin_name: str, specs: list[DepSpec], *, ask: bool = True) -> None:
    """JIT dependency verification — runs once per plugin per process.

    **Silent when everything is fine.** Only prints to stderr when
    something actually happens: installing a missing dep, upgrading a
    version, or a degraded state.

    For each ``DepSpec``:

    1. Run the ``check`` command.  Exit 0 → available (silent).
    2. If missing and ``install`` is set → ask user permission, then run
       the installer with visible progress.  If ``install`` is ``None``,
       the dep is optional — skip silently.
    3. If ``version`` is set, run ``version_check`` and compare.
       If installed version is too old, **ask user permission** before
       auto-upgrading.

    All output goes to *stderr* so it is visible in the terminal even
    when stdout is captured (piped, subagent, etc.).

    When ``ask=True`` (default), the user is prompted on stdin before
    any install or upgrade.  Set ``ask=False`` for fully automatic mode.
    """
    if plugin_name in _verified_plugins:
        return
    with _verified_lock:
        if plugin_name in _verified_plugins:
            return
        _verified_plugins.add(plugin_name)

    label = f"  {plugin_name}"
    all_ok = True

    for spec in specs:
        try:
            result = _run_cmd(spec.check, timeout=30)

            if result.returncode != 0:
                raise FileNotFoundError(f"exit {result.returncode}")

            # Silent when installed and version ok — no output at all

            # Optional version check — ask before upgrading
            if spec.version and spec.version_check:
                vr = _run_cmd(spec.version_check, timeout=15)
                if vr.returncode == 0:
                    installed_raw = vr.stdout.strip()
                    ok, msg = _check_version_meets(installed_raw, spec.version)
                    if ok:
                        logger.info("%s: %s %s", plugin_name, spec.name, msg)
                    elif spec.install is not None:
                        logger.info(
                            "%s: %s %s — auto-upgrading",
                            plugin_name, spec.name, msg,
                        )
                        print(
                            f"{label} … {spec.name} {msg} — upgrading …",
                            file=sys.stderr, flush=True,
                        )
                        if ask:
                            print(
                                f"{label}   Upgrade {spec.name}? [Y/n] ",
                                file=sys.stderr, flush=True,
                            )
                            try:
                                answer = input().strip().lower()
                                if answer not in ("", "y", "yes"):
                                    print(
                                        f"{label} ⚠ {spec.name} upgrade skipped by user",
                                        file=sys.stderr, flush=True,
                                    )
                                    all_ok = False
                                    continue
                            except (EOFError, OSError):
                                pass  # non-interactive → proceed
                        _stream_cmd(spec.install, label=label)
                        # Re-check after upgrade
                        post_up = _run_cmd(spec.check, timeout=15)
                        if post_up.returncode == 0:
                            print(
                                f"{label} ✓ {spec.name} upgraded",
                                file=sys.stderr, flush=True,
                            )
                        else:
                            raise RuntimeError(f"post-upgrade check failed (exit {post_up.returncode})")
                    else:
                        # Version too old but no install command — warn
                        logger.warning(
                            "%s: %s %s — no upgrade path",
                            plugin_name, spec.name, msg,
                        )
                        print(
                            f"{label} ⚠ {spec.name} {msg} — no upgrade path",
                            file=sys.stderr, flush=True,
                        )
                        all_ok = False

        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            if spec.install is None:
                # Optional dep — skip silently
                continue

            print(
                f"{label} … {spec.name} not found → installing …",
                file=sys.stderr, flush=True,
            )
            if ask:
                print(
                    f"{label}   Install {spec.name}? [Y/n] ",
                    file=sys.stderr, flush=True,
                )
                try:
                    answer = input().strip().lower()
                    if answer not in ("", "y", "yes"):
                        print(
                            f"{label} ⚠ {spec.name} install skipped by user",
                            file=sys.stderr, flush=True,
                        )
                        all_ok = False
                        continue
                except (EOFError, OSError):
                    pass  # non-interactive → proceed
            try:
                _stream_cmd(spec.install, label=label)
                # Re-check after install to verify it actually worked
                post_check = _run_cmd(spec.check, timeout=15)
                if post_check.returncode == 0:
                    print(
                        f"{label} ✓ {spec.name} installed",
                        file=sys.stderr, flush=True,
                    )
                else:
                    raise RuntimeError(f"post-install check failed (exit {post_check.returncode})")
            except Exception as exc:
                logger.error(
                    "%s: failed to install %s: %s", plugin_name, spec.name, exc,
                )
                print(
                    f"{label} ⚠ failed to install {spec.name}: {exc}",
                    file=sys.stderr, flush=True,
                )
                print(
                    f"{label}   Plugin will run with degraded functionality",
                    file=sys.stderr, flush=True,
                )
                all_ok = False

    if not all_ok:
        print(f"{label} ⚠ deps degraded — some features unavailable", file=sys.stderr, flush=True)
