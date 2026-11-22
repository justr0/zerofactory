"""Windows compatibility layer for Zero Factory.

POSIX hosts never take the Windows branches: every call site gates on
``zf_wincompat.IS_WINDOWS``. Preserved semantics:

- ``flock``: whole-file advisory lock via ``msvcrt`` byte-range locks,
  tracked per fd so unlock matches the locked region. Conflicts raise
  ``BlockingIOError`` like POSIX ``flock(LOCK_EX | LOCK_NB)``.
- ``is_pid_alive``: kernel probe via ``OpenProcess``/``GetExitCodeProcess``
  (``os.kill(pid, 0)`` TERMINATES the process on Windows — never use it).
- ``kill_tree``: hard process-tree kill via ``taskkill /T /F`` (Windows has
  no group SIGTERM; the two-stage grace flow degrades to one hard kill).
- ``detached_popen_kwargs``: ``CREATE_NO_WINDOW`` + new process group instead
  of ``start_new_session=True``.
- ``resolve_hermes_bin``: HERMES_BIN env, PATH, then the active venv's
  ``hermes.exe`` (the POSIX ``~/.local/bin/hermes`` default never exists).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# fcntl constants (subset used by Zero Factory)
LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

# Lock a byte range far beyond EOF (Windows allows range locks past EOF), so
# any writer appended content stays inside the locked region.
_LOCK_NBYTES = (2**31) - 1
_locked_fds: dict[int, int] = {}


def flock(fd: int, operation: int) -> None:
    """Emulate fcntl.flock on Windows via msvcrt byte-range locks."""
    import errno
    import msvcrt

    if operation & LOCK_UN:
        nbytes = _locked_fds.pop(fd, None)
        if nbytes is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, nbytes)
            except OSError:
                pass  # released implicitly at close on Windows
        return

    os.lseek(fd, 0, os.SEEK_SET)
    blocking = not (operation & LOCK_NB)
    attempts = 300 if blocking else 1  # ~30s worst case, then give up like LK_LOCK
    for attempt in range(attempts):
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_NBYTES)
        except OSError as e:
            if attempt + 1 >= attempts:
                raise BlockingIOError(errno.EAGAIN, "resource temporarily unavailable") from e
            time.sleep(0.1)
        else:
            _locked_fds[fd] = _LOCK_NBYTES
            return


def is_pid_alive(pid: int | None) -> bool:
    """Windows PID liveness probe that never signals the process."""
    import ctypes
    from ctypes import wintypes

    if not pid or not isinstance(pid, int) or pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False  # gone, or inaccessible (treated as not-running)
    try:
        exit_code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def kill_tree(pid: int | None, timeout: int = 15) -> bool:
    """Hard-kill a process and all descendants (taskkill /T /F)."""
    if not pid or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        try:
            os.kill(int(pid), 9)  # TerminateProcess fallback (direct child only)
            return True
        except OSError:
            return False


def getpgrp() -> int:
    """Group identity stand-in: own PID (sufficient for self-kill guards)."""
    return os.getpid()


def detached_popen_kwargs() -> dict:
    """Popen kwargs giving a detached child on the current platform."""
    if IS_WINDOWS:
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True}


def resolve_hermes_bin() -> str:
    """Locate the hermes CLI for spawned workers/scanners (cross-platform)."""
    env_bin = os.environ.get("HERMES_BIN")
    if env_bin:
        return env_bin
    found = shutil.which("hermes")
    if found:
        return found
    if IS_WINDOWS:
        try:
            candidate = Path(sys.executable).with_name("hermes.exe")
            if candidate.exists():
                return str(candidate)
        except Exception:
            pass
    local = Path.home() / ".local" / "bin" / "hermes"
    if local.exists():
        return str(local)
    return "hermes"
