"""Process group termination, worker process management, and active worker registry."""

from __future__ import annotations

import enum
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from paths import get_hermes_home

from .config import _active_workers, _log

try:
    from zf_wincompat import IS_WINDOWS, kill_tree
    from zf_wincompat import is_pid_alive as _win_pid_alive
except ImportError:  # package-mode import without plugin root on sys.path
    from ..zf_wincompat import IS_WINDOWS, kill_tree  # type: ignore
    from ..zf_wincompat import is_pid_alive as _win_pid_alive  # type: ignore


class ProcessState(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CRASHED = "CRASHED"


def build_shell_wrapper(cmd: list[str], exit_prefix: Path | str) -> list[str]:
    """Wrap command in a POSIX shell invocation that atomically writes integer exit code on exit/signal."""
    if IS_WINDOWS:
        wrapper_py = Path(__file__).with_name("_worker_wrapper.py")
        return [sys.executable, str(wrapper_py), str(exit_prefix)] + cmd
    script = (
        'prefix="$1"; shift; '
        'trap "rc=\\$?; printf \\"%d\\n\\" \\"\\$rc\\" > \\"${prefix}.exit.tmp\\" && '
        'mv \\"${prefix}.exit.tmp\\" \\"${prefix}.exit\\"; exit \\$rc" EXIT INT TERM; '
        '"$@"'
    )
    return ["sh", "-c", script, "zf-runner", str(exit_prefix)] + cmd


def get_exit_file_path(task_id: str) -> Path:
    """Return standard .exit file path for a task worker in <HERMES_HOME>/logs."""
    logs_dir = get_hermes_home() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"worker_{task_id}.exit"


def cleanup_process_artifacts(task_id: str) -> None:
    """Unlink lingering .exit and .exit.tmp files for task_id."""
    logs_dir = get_hermes_home() / "logs"
    for name in (f"worker_{task_id}.exit", f"worker_{task_id}.exit.tmp"):
        p = logs_dir / name
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def is_pid_alive(pid: int | None) -> bool:
    """Check if a process exists and is actively running (not a zombie/defunct)."""
    if not pid or not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError, ValueError):
        return False

    # If it is a direct child of the current process, non-blocking waitpid can also reap it
    if hasattr(os, "WNOHANG"):
        try:
            res_pid, _ = os.waitpid(pid, os.WNOHANG)
            if res_pid == pid:
                return False
        except (ChildProcessError, OSError):
            pass

    # On Linux, inspect /proc/<pid>/status to ensure the process is not a zombie (defunct)
    try:
        status_path = Path(f"/proc/{pid}/status")
        if status_path.exists():
            for line in status_path.read_text().splitlines():
                if line.startswith("State:"):
                    # 'Z' indicates zombie state (e.g. "State:\tZ (zombie)")
                    return "Z" not in line and "zombie" not in line.lower()
    except Exception:
        pass

    return True


_PROCFS_ABSENT = object()
# Optional ".exit" suffix: POSIX wrappers carry the exit file, the Windows
# wrapper the bare prefix. The boundary rejects neighbouring IDs (…01 vs …012)
# and sibling artifacts such as "worker_<id>.log".
_WORKER_ID_BOUNDARY = r"(?:\.exit)?(?![A-Za-z0-9_-]|\.[A-Za-z])"


def _read_worker_cmdline(pid: int):
    """Best-effort command line of ``pid``.

    Returns the decoded command line, ``None`` when it cannot be read, or
    ``_PROCFS_ABSENT`` on POSIX hosts that have no procfs at all.
    """
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                return None
            return result.stdout
        procfs_path = Path("/proc")
        try:
            procfs_mode = procfs_path.stat().st_mode
        except FileNotFoundError:
            # Keep best-effort behavior only when procfs is confirmed absent.
            return _PROCFS_ABSENT
        except OSError:
            # An inaccessible procfs does not establish that it is absent.
            return None
        if not stat.S_ISDIR(procfs_mode):
            return None
        cmd_data = Path(f"/proc/{pid}/cmdline").read_bytes()
        if not cmd_data:
            return None
        return cmd_data.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except Exception:
        # Fail closed if procfs exists but this PID's command line is unavailable.
        return None


def _process_identity(pid: int, task_id: str, *, strict: bool = True) -> bool | None:
    """Whether a live PID is this task's worker.

    ``strict`` requires the launch marker (``worker_<id>``); the terminate
    paths pass ``strict=False`` to keep their historical, looser check that
    the command line mentions the task id at all.

    ``True`` / ``False`` are definite; ``None`` means the command line could
    not be read, which is missing evidence about a *live* PID rather than
    evidence of a mismatch. The Windows wrapper is launched with the exit-file
    prefix ``worker_<id>`` (no ``.exit``), so matching only ``worker_<id>.exit``
    never identified a Windows worker: terminate always skipped the signal and
    every registry-miss reap read the live worker as crashed.
    """
    if not isinstance(pid, int) or pid <= 0 or not task_id:
        return False
    cmdline = _read_worker_cmdline(pid)
    if cmdline is _PROCFS_ABSENT:
        return True
    if cmdline is None:
        return None if IS_WINDOWS else False
    if IS_WINDOWS and not cmdline.strip():
        return None
    if not strict:
        return task_id in cmdline
    if IS_WINDOWS:
        return bool(
            re.search("worker_" + re.escape(task_id) + _WORKER_ID_BOUNDARY, cmdline)
        )
    return f"worker_{task_id}.exit" in cmdline


def process_matches_identity(pid: int, task_id: str) -> bool:
    """Check whether a live process still belongs to the given worker task."""
    return _process_identity(pid, task_id) is not False


def get_process_exit_code(
    task_id: str, pid: int | None
) -> Tuple[ProcessState, Optional[int]]:
    """Determine process status and return code using .exit file and OS PID checks.

    Returns:
        (ProcessState.COMPLETED, returncode): when .exit file exists and is parsed.
        (ProcessState.RUNNING, None): when process is actively running and matches identity.
        (ProcessState.CRASHED, -1): when process is missing or zombie with no .exit file.
    """
    exit_path = get_exit_file_path(task_id)
    if exit_path.exists():
        try:
            content = exit_path.read_text(encoding="utf-8").strip()
            if content:
                rc = int(content)
                return ProcessState.COMPLETED, rc
        except Exception:
            pass

    if pid and is_pid_alive(pid):
        if process_matches_identity(pid, task_id):
            return ProcessState.RUNNING, None

    return ProcessState.CRASHED, -1


def terminate_process_group(
    proc: subprocess.Popen | None, pid: int | None, grace: float = 2.0
) -> None:
    """Terminate a worker's ENTIRE process group (session), not just the child.

    All Zero Factory spawn sites run children with ``start_new_session=True``
    so the tree survives the spawner; the direct child (the ``hermes`` CLI
    wrapper) spawns its own descendants (the agent loop, git, npm, network
    subprocesses).

    Because ``start_new_session=True`` puts the child in a NEW session whose
    PGID equals its PID, ``os.killpg`` on that group reaches every descendant.
    """
    pgid: int | None = None
    if proc is not None and isinstance(proc.pid, int):
        try:
            pgid = os.getpgid(proc.pid)
        except (AttributeError, OSError):
            pgid = None
    if pgid is None and isinstance(pid, int) and pid > 0:
        pgid = pid

    if pgid is None:
        _log.debug(
            "terminate_process_group: no resolvable process group (proc=%s, pid=%s)",
            proc,
            pid,
        )
        return

    my_pid = os.getpid()
    if hasattr(os, "getpgrp"):
        try:
            my_pgid = os.getpgrp()
        except OSError:
            my_pgid = None
    else:
        my_pgid = my_pid
    if (my_pgid is not None and pgid == my_pgid) or (
        isinstance(pid, int) and pid == my_pid
    ):
        _log.warning(
            "terminate_process_group: refusing to signal own process group (pgid=%s, pid=%s)",
            pgid,
            pid,
        )
        return

    # Windows: no process groups or group signals — one hard tree kill.
    if IS_WINDOWS:
        target = pid if isinstance(pid, int) and pid > 0 else pgid
        kill_tree(target)
        if proc is not None and isinstance(proc.pid, int):
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        return

    # Cheap guard: probe group existence before signalling
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        if pid:
            if not is_pid_alive(pid):
                try:
                    if hasattr(os, "WNOHANG"):
                        os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                return
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(min(grace, 0.5))
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                if hasattr(os, "WNOHANG"):
                    os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        return
    except OSError as e:
        _log.debug("terminate_process_group: probe on group %s failed: %s", pgid, e)

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError) as e:
        _log.debug("terminate_process_group: SIGTERM to group %s failed: %s", pgid, e)
        return

    if proc is not None and isinstance(proc.pid, int):
        try:
            proc.wait(timeout=grace)
            return  # group exited gracefully within grace window
        except Exception:
            pass
    elif grace > 0:
        time.sleep(grace)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError) as e:
        _log.debug("terminate_process_group: SIGKILL to group %s failed: %s", pgid, e)

    if proc is not None and isinstance(proc.pid, int):
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    elif pid and isinstance(pid, int):
        try:
            if hasattr(os, "WNOHANG"):
                os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


def terminate_worker_process(
    proc: subprocess.Popen | None,
    pid: int | None,
    task_id: str | None = None,
) -> None:
    """Safely terminate a worker process group with SIGTERM then SIGKILL.

    When ``task_id`` is provided, the live PID's cmdline is verified to still
    reference the task before signaling: worker PIDs recorded in the DB can
    go stale and be recycled by the OS for an unrelated process, and
    signaling that PID kills an innocent process — a suspected contributor to
    the worker-loss churn. Mismatched PIDs are skipped and logged instead.
    """
    if pid and is_pid_alive(pid):
        if task_id and _process_identity(pid, task_id, strict=False) is False:
            cmdline = _read_worker_cmdline(pid) or ""
            _log.warning(
                "terminate_worker_process: PID %s no longer matches task %s "
                "(cmdline: %.120s); skipping signal to avoid killing an "
                "unrelated recycled process",
                pid,
                task_id,
                cmdline,
            )
            return
    try:
        terminate_process_group(proc, pid)
    except Exception as e:
        _log.debug("terminate_worker_process: group termination raised: %s", e)


def terminate_task_worker(task_id: str, pid: int | None) -> None:
    """Safely terminate any active worker process group for a task and clean up artifacts."""
    _active_workers.pop(task_id, None)
    if pid and is_pid_alive(pid):
        if _process_identity(pid, task_id, strict=False) is False:
            _log.warning(
                "terminate_task_worker: PID %s no longer matches task %s; skipping signal (stale/recycled PID)",
                pid,
                task_id,
            )
            cleanup_process_artifacts(task_id)
            return
        terminate_process_group(None, pid)
    cleanup_process_artifacts(task_id)


def stop_task_worker(
    task_id: str,
    cursor: sqlite3.Cursor | None = None,
    pid: int | None = None,
) -> None:
    """Safely terminate any active worker process for a task.

    Checks in-memory workers first, then explicit ``pid``, then falls back to
    ``tasks.worker_pid`` column or metadata. Idempotent and exception-safe.
    """
    proc = _active_workers.pop(task_id, None)
    if proc is not None:
        pid = proc.pid
    elif pid is None and cursor:
        try:
            try:
                cursor.execute(
                    "SELECT worker_pid, metadata FROM tasks WHERE id = ?", (task_id,)
                )
                row = cursor.fetchone()
                if row:
                    pid = (
                        row["worker_pid"]
                        if hasattr(row, "keys") and "worker_pid" in row.keys()
                        else row[0]
                    )
                    if not pid:
                        raw_meta = (
                            row["metadata"]
                            if hasattr(row, "keys") and "metadata" in row.keys()
                            else (row[1] if len(row) > 1 else None)
                        )
                        meta = json.loads(raw_meta or "{}")
                        pid = meta.get("worker_pid")
            except sqlite3.OperationalError:
                cursor.execute("SELECT metadata FROM tasks WHERE id = ?", (task_id,))
                row = cursor.fetchone()
                if row:
                    raw_meta = (
                        row["metadata"]
                        if hasattr(row, "keys") and "metadata" in row.keys()
                        else row[0]
                    )
                    meta = json.loads(raw_meta or "{}")
                    pid = meta.get("worker_pid")
        except Exception:
            pass
    terminate_worker_process(proc, pid, task_id=task_id)
    cleanup_process_artifacts(task_id)
