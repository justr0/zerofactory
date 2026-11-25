"""Process group termination, worker process management, and active worker registry."""

from __future__ import annotations

import enum
import json
import os
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


# Workers the dispatcher stopped on purpose: one marker file per task id,
# holding the worker pid.  Recording the pid *before* signaling lets the reaper
# tell "stopped by the dispatcher" apart from a crashed worker: a deliberate
# stop is booked as ``worker_stopped`` and never spends the task's failure
# budget, which used to retry the same role (duplicate reviews) and park tasks
# after three stops.  The markers live beside the dispatcher lock file rather
# than in memory because the stop and the reaping usually happen in different
# processes (dashboard stop button, CLI ``move``, gateway-owned reaper).
_STOP_MARKER_MAX_AGE_SECONDS = 24 * 60 * 60


def _stop_markers_dir() -> Path:
    from .config import get_dispatcher_lock_path

    return Path(f"{get_dispatcher_lock_path()}.stops")


def _stop_marker_path(task_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in task_id)
    return _stop_markers_dir() / f"{safe}.stop"


def _sweep_stale_stop_markers() -> None:
    """Drop markers nobody consumed (e.g. a worker pid that was never reaped)."""
    cutoff = time.time() - _STOP_MARKER_MAX_AGE_SECONDS
    try:
        for marker in _stop_markers_dir().glob("*.stop"):
            try:
                if marker.stat().st_mtime < cutoff:
                    marker.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _read_stop_marker(task_id: str) -> int | None:
    try:
        return int(_stop_marker_path(task_id).read_text().strip())
    except (OSError, ValueError):
        return None


def mark_deliberate_stop(task_id: str | None, pid: int | None) -> None:
    """Record that the dispatcher is about to stop ``task_id``'s worker."""
    if not task_id:
        return
    try:
        stopped_pid = int(pid) if pid is not None else 0
    except (TypeError, ValueError):
        return
    if stopped_pid <= 0:
        return
    marker = _stop_marker_path(str(task_id))
    tmp = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{stopped_pid}\n")
        os.replace(tmp, marker)
        _sweep_stale_stop_markers()
    except OSError as e:
        # Fail open: the stop still happens, it is only booked as a failure.
        _log.warning("mark_deliberate_stop: could not write %s: %s", marker, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def forget_deliberate_stop(task_id: str | None, pid: int | str | None) -> None:
    """Drop a stop marker again when nothing was signalled after all."""
    if task_id and _as_pid(pid) == _read_stop_marker(str(task_id)):
        try:
            _stop_marker_path(str(task_id)).unlink(missing_ok=True)
        except OSError:
            pass


def _as_pid(pid: int | str | None) -> int | None:
    """Task metadata stores the worker pid as JSON: accept int or digit string."""
    try:
        value = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value > 0 else None


def consume_deliberate_stop(task_id: str, pid: int | str | None) -> bool:
    """True exactly once for the worker pid the dispatcher stopped on purpose.

    The marker is always removed: a stale marker (different pid) must never
    mask a genuine crash of a later worker for the same task.
    """
    reaped_pid = _as_pid(pid)
    if reaped_pid is None:
        return False
    stopped_pid = _read_stop_marker(task_id)
    if stopped_pid is None:
        return False
    try:
        _stop_marker_path(task_id).unlink(missing_ok=True)
    except OSError:
        pass
    return reaped_pid == stopped_pid


def _pid_is_ancestor(pid: int) -> bool:
    """True when ``pid`` is this process or one of its parents.

    ``hermes zerofactory block <own task>`` runs as a tool command of the very
    worker whose task it transitions.  Tool commands get their own session, so
    the ``getpgrp`` guard does not see the relationship; the command then
    SIGTERMed its own worker mid-command and the reaper booked the stop as a
    crash.  Walking the parent chain catches that case.
    """
    if pid <= 1:
        return False
    my_pid = os.getpid()
    if pid in (my_pid, os.getppid()):
        return True
    if not Path("/proc/self/stat").exists():
        return False
    current = my_pid
    for _ in range(64):
        try:
            data = Path(f"/proc/{current}/stat").read_bytes()
            ppid = int(data[data.rfind(b")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if ppid <= 1:
            return False
        if ppid == pid:
            return True
        current = ppid
    return False


def build_shell_wrapper(cmd: list[str], exit_prefix: Path | str) -> list[str]:
    """Wrap command to persist its exit code and any caught termination signal."""
    if IS_WINDOWS:
        wrapper_py = Path(__file__).with_name("_worker_wrapper.py")
        return [sys.executable, str(wrapper_py), str(exit_prefix)] + cmd
    script = (
        'prefix="$1"; shift; started_at=$(date +%s); '
        "write_exit() { rc=$?; trap - EXIT; "
        'printf "%s\\n" "$rc" > "${prefix}.exit.tmp" && '
        'mv "${prefix}.exit.tmp" "${prefix}.exit"; }; '
        'write_signal() { signal_number="$1"; signal_name="$2"; kill_name="$3"; '
        "ended_at=$(date +%s); elapsed_seconds=$((ended_at - started_at)); "
        "task_id=${prefix##*/}; task_id=${task_id#worker_}; "
        '[ -z "$child" ] || kill -"$kill_name" "$child" 2>/dev/null || true; '
        'printf \'{"signal_number":%s,"signal_name":"%s","task_id":"%s",'
        '"elapsed_seconds":%s,"turn_state":"interrupted"}\\n\' '
        '"$signal_number" "$signal_name" "$task_id" "$elapsed_seconds" '
        '> "${prefix}.signal.tmp" && mv "${prefix}.signal.tmp" "${prefix}.signal"; '
        'printf "ZeroFactory worker interrupted: signal_number=%s signal_name=%s '
        'task_id=%s elapsed_seconds=%s turn_state=interrupted\\n" '
        '"$signal_number" "$signal_name" "$task_id" "$elapsed_seconds"; '
        "trap - HUP INT TERM; exit $((128 + signal_number)); }; "
        "trap write_exit EXIT; "
        'trap "write_signal 1 SIGHUP HUP" HUP; '
        'trap "write_signal 2 SIGINT INT" INT; '
        'trap "write_signal 15 SIGTERM TERM" TERM; '
        '"$@" & child=$!; wait "$child"; exit $?'
    )
    return ["sh", "-c", script, "zf-runner", str(exit_prefix)] + cmd


def get_exit_file_path(task_id: str) -> Path:
    """Return standard .exit file path for a task worker in <HERMES_HOME>/logs."""
    logs_dir = get_hermes_home() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"worker_{task_id}.exit"


def get_signal_file_path(task_id: str) -> Path:
    """Return the durable signal-interruption marker path for a task worker."""
    return get_exit_file_path(task_id).with_suffix(".signal")


def read_worker_signal(task_id: str) -> dict | None:
    """Read a validated signal marker written by the process-group wrapper."""
    try:
        payload = json.loads(get_signal_file_path(task_id).read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("task_id") != task_id
            or type(payload.get("signal_number")) is not int
            or payload["signal_number"] <= 0
            or type(payload.get("elapsed_seconds")) is not int
            or payload["elapsed_seconds"] < 0
        ):
            return None
        return payload
    except (OSError, ValueError, TypeError):
        return None


def cleanup_process_artifacts(task_id: str) -> None:
    """Unlink lingering worker exit and signal marker files for task_id."""
    logs_dir = get_hermes_home() / "logs"
    for name in (
        f"worker_{task_id}.exit",
        f"worker_{task_id}.exit.tmp",
        f"worker_{task_id}.signal",
        f"worker_{task_id}.signal.tmp",
    ):
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


def process_matches_identity(pid: int, task_id: str) -> bool:
    """Check whether a live process still belongs to the given worker task."""
    if not isinstance(pid, int) or pid <= 0 or not task_id:
        return False
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
                return False
            cmd_data = result.stdout
        else:
            procfs_path = Path("/proc")
            try:
                procfs_mode = procfs_path.stat().st_mode
            except FileNotFoundError:
                # Keep best-effort behavior only when procfs is confirmed absent.
                return True
            except OSError:
                # An inaccessible procfs does not establish that it is absent.
                return False
            if not stat.S_ISDIR(procfs_mode):
                return False
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            cmd_data = cmdline_path.read_bytes()
            if not cmd_data:
                return False
            return f"worker_{task_id}.exit".encode() in cmd_data
        return f"worker_{task_id}.exit" in cmd_data
    except Exception:
        # Fail closed if procfs exists but this PID's command line is unavailable.
        return False


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
) -> bool:
    """Terminate a worker's ENTIRE process group (session), not just the child.

    All Zero Factory spawn sites run children with ``start_new_session=True``
    so the tree survives the spawner; the direct child (the ``hermes`` CLI
    wrapper) spawns its own descendants (the agent loop, git, npm, network
    subprocesses).

    Because ``start_new_session=True`` puts the child in a NEW session whose
    PGID equals its PID, ``os.killpg`` on that group reaches every descendant.

    Returns True only when a signal was actually sent to the worker; False
    when nothing was signalled (own process group, called from inside the
    worker's own process tree, already-dead pid).
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
        return False

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
        return False

    # A worker's own tool command transitioning its task (self ``block``/``move``)
    # lives in its own session: refuse to kill the worker it is running inside.
    for candidate in (pid, pgid):
        if isinstance(candidate, int) and candidate > 0 and _pid_is_ancestor(candidate):
            _log.warning(
                "terminate_process_group: refusing to signal worker pid %s from inside "
                "its own process tree (task transition issued by the worker itself)",
                candidate,
            )
            return False

    # Windows: no process groups or group signals — one hard tree kill.
    if IS_WINDOWS:
        target = pid if isinstance(pid, int) and pid > 0 else pgid
        kill_tree(target)
        if proc is not None and isinstance(proc.pid, int):
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        return True

    # Cheap guard: probe group existence before signalling
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        signalled = False
        if pid:
            if not is_pid_alive(pid):
                try:
                    if hasattr(os, "WNOHANG"):
                        os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                return False
            try:
                os.kill(pid, signal.SIGTERM)
                signalled = True
                time.sleep(min(grace, 0.5))
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                if hasattr(os, "WNOHANG"):
                    os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        return signalled
    except OSError as e:
        _log.debug("terminate_process_group: probe on group %s failed: %s", pgid, e)
        return False

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError) as e:
        _log.debug("terminate_process_group: SIGTERM to group %s failed: %s", pgid, e)
        return False

    if proc is not None and isinstance(proc.pid, int):
        try:
            proc.wait(timeout=grace)
            return True  # group exited gracefully within grace window
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
    return True


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
    if pid is None and proc is not None:
        pid = proc.pid
    if pid:
        if not is_pid_alive(pid):
            _log.debug(
                "terminate_worker_process: PID %s is no longer alive; skipping signal",
                pid,
            )
            return
        if task_id and not process_matches_identity(pid, task_id):
            _log.warning(
                "terminate_worker_process: PID %s no longer matches task %s; "
                "skipping signal to avoid killing an unrelated recycled process",
                pid,
                task_id,
            )
            return
    # Register before signalling: the reaper runs on another thread and can
    # observe the death before this call returns.
    mark_deliberate_stop(task_id, pid)
    try:
        if not terminate_process_group(proc, pid):
            forget_deliberate_stop(task_id, pid)
    except Exception as e:
        _log.debug("terminate_worker_process: group termination raised: %s", e)


def terminate_task_worker(task_id: str, pid: int | None) -> None:
    """Safely terminate any active worker process group for a task and clean up artifacts."""
    _active_workers.pop(task_id, None)
    if pid and is_pid_alive(pid):
        if not process_matches_identity(pid, task_id):
            _log.warning(
                "terminate_task_worker: PID %s no longer matches task %s; skipping signal (stale/recycled PID)",
                pid,
                task_id,
            )
            cleanup_process_artifacts(task_id)
            return
        mark_deliberate_stop(task_id, pid)
        if not terminate_process_group(None, pid):
            forget_deliberate_stop(task_id, pid)
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
