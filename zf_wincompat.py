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
- ``git_env``: unattended git environment. Git for Windows' system config
  defaults ``credential.helper`` to ``helper-selector`` (an interactive
  "choose a helper" prompt); inside the daemon it never returns and keeps the
  captured pipes open, so ``subprocess.run(timeout=)`` blocks forever.
- ``run_git``: git with a *hard* timeout — tree-kills the whole process tree
  at the deadline so ``communicate()`` cannot wedge on orphaned grandchildren.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
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
                raise BlockingIOError(
                    errno.EAGAIN, "resource temporarily unavailable"
                ) from e
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

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

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


def git_env(base: dict | None = None) -> dict:
    """Return an environment that makes git safe to run unattended on Windows.

    - ``GIT_TERMINAL_PROMPT=0`` / ``GCM_INTERACTIVE=never``: no terminal or
      credential-manager UI prompts.
    - ``GIT_CONFIG_*`` entries behave like ``-c`` on the command line, so they
      win over every config file. An empty ``credential.helper`` resets the
      helper list (dropping the bundled Git's ``helper-selector``), and GitHub
      auth is routed through the already-authenticated ``gh`` CLI, which Zero
      Factory requires anyway.
    - ``GIT_SSH_COMMAND`` with ``BatchMode=yes``: the HTTPS->SSH push fallback
      fails instead of asking for a key passphrase (agent-provided keys still
      work).
    """
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    extra = (
        ("credential.helper", ""),  # empty value resets the helper list
        ("credential.interactive", "never"),
        ("credential.https://github.com.helper", "!gh auth git-credential"),
    )
    index = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for key, value in extra:
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
        index += 1
    env["GIT_CONFIG_COUNT"] = str(index)
    return env


def run_git(
    args: list[str],
    cwd: str | None = None,
    timeout: float = 60,
    env: dict | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run git with a timeout that is actually bounded on Windows.

    ``subprocess.run(timeout=)`` kills only its direct child and then drains
    the captured pipes; a grandchild that inherited those handles (for example
    a credential prompt waiting for a human) keeps them open, so the drain
    never returns and the caller's "timeout" hangs forever. A watchdog
    tree-kills the whole process tree at the deadline instead: every handle
    closes, ``communicate()`` returns, and ``TimeoutExpired`` is raised.
    """
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
    )
    fired = threading.Event()

    def _watchdog() -> None:
        fired.set()
        kill_tree(proc.pid)

    timer = threading.Timer(timeout, _watchdog)
    timer.daemon = True
    timer.start()
    try:
        out, err = proc.communicate()
    finally:
        timer.cancel()
    if fired.is_set():
        raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)
