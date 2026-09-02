"""
_shared/procs.py — supervised subprocess helpers for hermes-ultimate-coding.

Several plugins launch long-lived helpers: language servers, a node memory
gateway, a SearXNG webapp, a browser launcher, graph extractors. Those helpers
routinely spawn children of their own — ``pyright-langserver`` starts node
workers, ``node --import tsx`` starts a compiler child, ``npm`` starts whatever
the package asked for.

Signalling only the direct child leaves those grandchildren running, which
costs twice:

* **Leaked processes.** They outlive the plugin and keep burning CPU/memory.
* **Pipes that never close.** A grandchild inherits stdout/stderr, so the pipe
  stays open, ``read()`` never sees EOF and ``communicate()`` blocks until its
  timeout — often while a lock is held.

Giving each helper its own process group makes both problems solvable: the
whole tree can be signalled with one ``killpg``.

Usage::

    from _shared.procs import popen_supervised, kill_tree

    proc = popen_supervised(["node", "server.js"], stdout=subprocess.PIPE)
    ...
    kill_tree(proc)

Safety invariant: :func:`kill_tree` never signals the caller's own process
group. If the child was not given its own group, only the child is killed.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Any, List, Optional, Sequence, Union

logger = logging.getLogger("hermes-procs")

__all__ = ["popen_supervised", "kill_tree", "supports_process_groups"]


def supports_process_groups() -> bool:
    """True when this platform can isolate and signal process groups."""
    return os.name == "posix" and hasattr(os, "killpg")


def popen_supervised(
    args: Union[Sequence[str], str],
    **kwargs: Any,
) -> subprocess.Popen:
    """``subprocess.Popen`` with the child placed in its own process group.

    Identical to ``Popen`` otherwise. On POSIX, ``start_new_session=True`` is
    added unless the caller already specified session/group handling; on other
    platforms this is a plain ``Popen``.
    """
    if supports_process_groups():
        if "start_new_session" not in kwargs and "preexec_fn" not in kwargs:
            kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def _own_group() -> Optional[int]:
    try:
        return os.getpgid(0)
    except OSError:
        return None


def _child_group(proc: subprocess.Popen) -> Optional[int]:
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return None


def kill_tree(
    proc: Optional[subprocess.Popen],
    *,
    grace: float = 5.0,
    reap: bool = True,
) -> None:
    """Terminate *proc* and every process it spawned. Never raises.

    Sends SIGTERM to the child's process group, waits up to *grace* seconds for
    a clean exit, then SIGKILLs whatever is left. Finishes by reaping the child
    so it cannot linger as a zombie.

    If the child shares this process's group — i.e. it was not started with
    :func:`popen_supervised` — only the child itself is signalled. Killing the
    shared group would take down the agent hosting the plugin.
    """
    if proc is None:
        return
    if proc.poll() is not None:
        if reap:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
        return

    pgid = _child_group(proc) if supports_process_groups() else None
    isolated = pgid is not None and pgid != _own_group()

    def _signal(sig: int) -> None:
        if isolated and pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except Exception:
            pass

    _signal(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal(signal.SIGKILL)
    except Exception:
        pass

    if reap:
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    # Even after the direct child is reaped, grandchildren in the group may
    # still hold inherited pipes open. One final group SIGKILL closes them.
    if isolated and pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
