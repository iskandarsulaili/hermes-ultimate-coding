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


def _run_cmd(
    args: list[str] | str,
    *,
    capture: bool = False,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a shell command.

    * ``list[str]`` → direct ``subprocess.run`` (no shell)
    * ``str``       → ``shell=True`` (required for pipes, redirects)
    """
    if isinstance(args, str):
        return subprocess.run(args, shell=True, capture_output=capture, timeout=timeout)
    return subprocess.run(args, capture_output=capture, timeout=timeout)


def _stream_cmd(args: list[str] | str, label: str = "  deps", timeout: int = 300) -> None:
    """Run a command and stream its output to stderr in real time.

    Raises ``RuntimeError`` on non-zero exit or timeout.
    """
    if isinstance(args, str):
        print(f"{label}   running: {args}", file=sys.stderr, flush=True)
        ret = subprocess.call(args, shell=True, timeout=timeout)
        if ret != 0:
            raise RuntimeError(f"command '{args}' exited {ret}")
        return

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout)
        if stdout:
            for line in stdout.splitlines():
                if line.strip():
                    print(f"{label}   {line.rstrip()}", file=sys.stderr, flush=True)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError(f"command {' '.join(args)} timed out after {timeout}s")

    if proc.returncode != 0:
        raise RuntimeError(f"command {' '.join(args)} exited {proc.returncode}")


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


def ensure_deps(plugin_name: str, specs: list[DepSpec]) -> None:
    """JIT dependency verification — runs once per plugin per process.

    For each ``DepSpec``:

    1. Run the ``check`` command.  Exit 0 → available.
    2. If missing and ``install`` is set → run the installer with visible
       progress.  If ``install`` is ``None``, the dep is optional — skip
       silently.
    3. If ``version`` is set, run ``version_check`` and compare.
       If installed version is too old, **auto-upgrade** — no manual
       steps needed.

    All output goes to *stderr* so it is visible in the terminal even
    when stdout is captured (piped, subagent, etc.).
    """
    if plugin_name in _verified_plugins:
        return
    _verified_plugins.add(plugin_name)

    label = f"  {plugin_name}"

    print(f"{label} ⟐ verifying dependencies …", file=sys.stderr, flush=True)

    for spec in specs:
        try:
            result = _run_cmd(spec.check, capture=True, timeout=30)

            if result.returncode != 0:
                raise FileNotFoundError(f"exit {result.returncode}")

            print(
                f"{label} ✓ {spec.name}  — {spec.purpose or 'ok'}",
                file=sys.stderr, flush=True,
            )

            # Optional version check — auto-upgrade if too old
            if spec.version and spec.version_check:
                vr = _run_cmd(spec.version_check, capture=True, timeout=15)
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
                        _stream_cmd(spec.install, label=label)
                        print(
                            f"{label} ✓ {spec.name} upgraded",
                            file=sys.stderr, flush=True,
                        )

        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            if spec.install is None:
                # Optional dep — skip silently
                continue

            print(
                f"{label} … {spec.name} not found → installing …",
                file=sys.stderr, flush=True,
            )
            try:
                _stream_cmd(spec.install, label=label)
                print(
                    f"{label} ✓ {spec.name} installed",
                    file=sys.stderr, flush=True,
                )
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
                # Don't raise — let the plugin load anyway with degraded
                # functionality rather than crashing Hermes.

    print(f"{label} ✓ deps ok", file=sys.stderr, flush=True)
