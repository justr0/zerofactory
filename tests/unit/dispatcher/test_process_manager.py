"""Unit tests for dispatcher/process_manager.py: process group termination and worker cleanup."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from dispatcher.config import _active_workers
from dispatcher.process_manager import (
    ProcessState,
    build_shell_wrapper,
    cleanup_process_artifacts,
    get_exit_file_path,
    get_process_exit_code,
    is_pid_alive,
    process_matches_identity,
    stop_task_worker,
    terminate_process_group,
    terminate_task_worker,
)


def _spawn_test_process(command, *, isolated_session):
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=isolated_session and os.name != "nt",
    )


def test_is_pid_alive():
    """Verify alive check for valid, non-existent, and invalid PIDs."""
    # Invalid PIDs
    assert is_pid_alive(None) is False
    assert is_pid_alive(0) is False
    assert is_pid_alive(-1) is False
    assert is_pid_alive("invalid") is False  # type: ignore

    # Current process PID must be alive
    assert is_pid_alive(os.getpid()) is True

    # High non-existent PID
    assert is_pid_alive(9999999) is False


def test_process_matches_identity_requires_task_specific_marker():
    """A generic Hermes command is not enough to validate a persisted PID."""
    with (
        patch("dispatcher.process_manager.IS_WINDOWS", False),
        patch(
            "pathlib.Path.stat",
            return_value=MagicMock(st_mode=0o040755),
        ),
        patch(
            "pathlib.Path.read_bytes",
            return_value=b"python\0-c\0task-123\0",
        ),
    ):
        assert process_matches_identity(1234, "scanner_no_such_board_marker") is False


def test_process_matches_identity_accepts_real_worker_prefix_and_scanner_exit_path():
    """Recognize production wrapper argv for workers and scanner artifacts."""
    for task_id, suffix in (("task-123", ""), ("scanner_board-a", ".exit")):
        with (
            patch("dispatcher.process_manager.IS_WINDOWS", False),
            patch(
                "pathlib.Path.stat",
                return_value=MagicMock(st_mode=0o040755),
            ),
            patch(
                "pathlib.Path.read_bytes",
                return_value=(
                    f"sh\0-c\0script\0zf-runner\0/logs/worker_{task_id}{suffix}\0"
                    "hermes\0"
                ).encode(),
            ),
        ):
            assert process_matches_identity(1234, task_id) is True, task_id


def test_process_matches_identity_reads_windows_command_line():
    """Use the Windows process command line query and require the same marker."""
    result = MagicMock(
        returncode=0,
        stdout=(
            'python.exe "C:\\python\\_worker_wrapper.py" '
            '"C:\\logs\\worker_scanner_board-a.exit" hermes'
        ),
    )
    with (
        patch("dispatcher.process_manager.IS_WINDOWS", True),
        patch("dispatcher.process_manager.subprocess.run", return_value=result),
    ):
        assert process_matches_identity(1234, "scanner_board-a") is True


def test_terminate_process_group_with_dead_group():
    """Calling terminate_process_group on dead or non-existent group does not raise."""
    # Should safely return without error
    terminate_process_group(None, 9999999, grace=0.1)


def test_terminate_process_group_kills_session_group():
    """Verify SIGTERM followed by SIGKILL to the process group."""
    mock_proc = MagicMock()
    mock_proc.pid = 1234
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=0.1)

    calls = []

    def mock_killpg(pgid, sig):
        calls.append((pgid, sig))

    import dispatcher

    with (
        patch.object(dispatcher.os, "getpgid", return_value=1234),
        patch.object(dispatcher.os, "killpg", side_effect=mock_killpg),
        patch.object(dispatcher.time, "sleep", return_value=None),
    ):
        terminate_process_group(mock_proc, 1234, grace=0.1)

    # Pre-flight probe (signal 0), then SIGTERM, then SIGKILL
    assert (1234, 0) in calls
    assert (1234, signal.SIGTERM) in calls
    assert (1234, signal.SIGKILL) in calls


def test_stop_task_worker_in_memory_and_db():
    """Verify stop_task_worker pops from _active_workers and terminates."""
    mock_proc = MagicMock()
    mock_proc.pid = 4321
    _active_workers["task-test-1"] = mock_proc

    with patch("dispatcher.process_manager.terminate_worker_process") as mock_term:
        stop_task_worker("task-test-1")
        assert "task-test-1" not in _active_workers
        mock_term.assert_called_once_with(mock_proc, 4321, task_id="task-test-1")


def test_stop_task_worker_falls_back_to_db_metadata():
    """When not in memory, reads worker_pid from tasks table metadata."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, metadata TEXT)")
    conn.execute(
        "INSERT INTO tasks VALUES ('t-meta', ?)", (json.dumps({"worker_pid": 8888}),)
    )
    conn.commit()

    with patch("dispatcher.process_manager.terminate_worker_process") as mock_term:
        stop_task_worker("t-meta", cursor=conn.cursor())
        mock_term.assert_called_once_with(None, 8888, task_id="t-meta")


def test_build_shell_wrapper():
    """Verify build_shell_wrapper sets up POSIX script, shift, and exit code trapping."""
    cmd = ["hermes", "-p", "zf-builder", "chat"]
    exit_prefix = "/tmp/test_exit"
    wrapped = build_shell_wrapper(cmd, exit_prefix)

    assert wrapped[0] == "sh"
    assert wrapped[1] == "-c"
    assert 'prefix="$1"; shift;' in wrapped[2]
    assert "printf" in wrapped[2]
    assert "${prefix}.exit" in wrapped[2]
    assert wrapped[3] == "zf-runner"
    assert wrapped[4] == exit_prefix
    assert wrapped[5:] == cmd


def test_process_matches_identity_rejects_generic_hermes_process():
    """A generic Hermes command is not evidence that a persisted PID is ours."""
    with (
        patch("dispatcher.process_manager.Path.exists", return_value=True),
        patch(
            "dispatcher.process_manager.Path.read_bytes",
            return_value=b"/usr/bin/hermes\0-p\0zf-orchestrator\0",
        ),
    ):
        assert process_matches_identity(1234, "scanner_board-a") is False


def test_process_matches_identity_accepts_wrapped_scanner():
    """The shell wrapper's exit path carries the scanner-specific identity."""
    cmdline = (
        b"sh\0-c\0trap ...\0zf-runner\0/home/test/.hermes/logs/"
        b"worker_scanner_board-a.exit\0/usr/bin/hermes\0"
    )
    with (
        patch("dispatcher.process_manager.Path.exists", return_value=True),
        patch("dispatcher.process_manager.Path.read_bytes", return_value=cmdline),
    ):
        assert process_matches_identity(1234, "scanner_board-a") is True


def test_process_matches_identity_preserves_non_proc_fallback():
    """Platforms with confirmed-absent procfs retain best-effort acceptance."""
    with patch("dispatcher.process_manager.Path.stat", side_effect=FileNotFoundError):
        assert process_matches_identity(1234, "scanner_board-a") is True


def test_process_matches_identity_rejects_inaccessible_procfs():
    """An error checking procfs must not be mistaken for an absent procfs."""
    with patch("dispatcher.process_manager.Path.stat", side_effect=PermissionError):
        assert process_matches_identity(1234, "scanner_board-a") is False


def test_process_matches_identity_rejects_unavailable_proc_command_line():
    """Missing, empty, or unreadable per-PID cmdline data fails closed."""
    with (
        patch(
            "dispatcher.process_manager.Path.stat",
            return_value=MagicMock(st_mode=0o040755),
        ),
        patch(
            "dispatcher.process_manager.Path.read_bytes",
            side_effect=[FileNotFoundError, b"", PermissionError],
        ),
    ):
        for _ in range(3):
            assert process_matches_identity(1234, "scanner_board-a") is False


def test_get_process_exit_code_completed(tmp_path):
    """Verify COMPLETED status when .exit file exists."""
    task_id = "task-completed-123"
    exit_file = tmp_path / f"worker_{task_id}.exit"
    exit_file.write_text("0\n")

    with patch("dispatcher.process_manager.get_exit_file_path", return_value=exit_file):
        state, rc = get_process_exit_code(task_id, pid=99999)
        assert state == ProcessState.COMPLETED
        assert rc == 0

    exit_file.write_text("143\n")
    with patch("dispatcher.process_manager.get_exit_file_path", return_value=exit_file):
        state, rc = get_process_exit_code(task_id, pid=99999)
        assert state == ProcessState.COMPLETED
        assert rc == 143


def test_get_process_exit_code_running(tmp_path):
    """Verify RUNNING status when .exit file absent and PID is alive."""
    task_id = "task-running-123"
    exit_file = tmp_path / f"worker_{task_id}.exit"

    with (
        patch("dispatcher.process_manager.get_exit_file_path", return_value=exit_file),
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.process_matches_identity", return_value=True),
        patch("pathlib.Path.exists", return_value=False),
    ):
        state, rc = get_process_exit_code(task_id, pid=12345)
        assert state == ProcessState.RUNNING
        assert rc is None


def test_get_process_exit_code_crashed(tmp_path):
    """Verify CRASHED status when .exit file absent and PID is dead."""
    task_id = "task-crashed-123"
    exit_file = tmp_path / f"worker_{task_id}.exit"

    with (
        patch("dispatcher.process_manager.get_exit_file_path", return_value=exit_file),
        patch("dispatcher.process_manager.is_pid_alive", return_value=False),
    ):
        state, rc = get_process_exit_code(task_id, pid=12345)
        assert state == ProcessState.CRASHED
        assert rc == -1


def test_cleanup_process_artifacts(tmp_path):
    """Verify cleanup_process_artifacts removes both .exit and .exit.tmp."""
    task_id = "task-cleanup-123"
    exit_file = tmp_path / f"worker_{task_id}.exit"
    tmp_file = tmp_path / f"worker_{task_id}.exit.tmp"
    exit_file.write_text("0")
    tmp_file.write_text("0")

    with (
        patch("pathlib.Path.home", return_value=tmp_path.parent),
        patch.dict(os.environ, {"HERMES_HOME": ""}),
    ):
        logs_dir = tmp_path.parent / ".hermes" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / f"worker_{task_id}.exit").write_text("0")
        (logs_dir / f"worker_{task_id}.exit.tmp").write_text("0")

        cleanup_process_artifacts(task_id)
        assert not (logs_dir / f"worker_{task_id}.exit").exists()
        assert not (logs_dir / f"worker_{task_id}.exit.tmp").exists()


def test_terminate_task_worker_stops_real_wrapped_worker_and_cleans_artifacts(
    tmp_path, monkeypatch
):
    """The production wrapper prefix validates, signals, and cleans its worker."""
    task_id = "task-xyz"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    exit_file = logs_dir / f"worker_{task_id}.exit"
    tmp_file = logs_dir / f"worker_{task_id}.exit.tmp"
    exit_file.write_text("0")
    tmp_file.write_text("0")
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)
    command = build_shell_wrapper(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        logs_dir / f"worker_{task_id}",
    )
    process = _spawn_test_process(command, isolated_session=True)
    try:
        matches_identity = False
        for _ in range(100):
            if process_matches_identity(process.pid, task_id):
                matches_identity = True
                break
            time.sleep(0.01)
        assert matches_identity, task_id
        terminate_task_worker(task_id, process.pid)
        process.wait(timeout=5)
        assert not exit_file.exists()
        assert not tmp_file.exists()
        marker = tmp_path / "dispatcher.lock.stops" / f"{task_id}.stop"
        assert marker.read_text().strip() == str(process.pid)
    finally:
        if process.poll() is None:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)


def test_terminate_task_worker_rejects_unwrapped_live_process(tmp_path, monkeypatch):
    """Task-id argv text alone never authorizes either group or child signaling."""
    task_id = "task-xyz"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)

    isolated_session_modes = (True, False) if os.name != "nt" else (False,)
    for isolated_session in isolated_session_modes:
        exit_file = logs_dir / f"worker_{task_id}.exit"
        tmp_file = logs_dir / f"worker_{task_id}.exit.tmp"
        exit_file.write_text("0")
        tmp_file.write_text("0")
        process = _spawn_test_process(
            [sys.executable, "-c", "import sys, time; time.sleep(60)", task_id],
            isolated_session=isolated_session,
        )
        try:
            assert process_matches_identity(process.pid, task_id) is False
            if os.name == "nt":
                with patch("dispatcher.process_manager.kill_tree") as mock_kill_tree:
                    terminate_task_worker(task_id, process.pid)
                mock_kill_tree.assert_not_called()
            elif isolated_session:
                terminate_task_worker(task_id, process.pid)
            else:
                with (
                    patch(
                        "dispatcher.process_manager.os.killpg",
                        side_effect=ProcessLookupError,
                    ) as mock_killpg,
                    patch("dispatcher.process_manager.os.kill") as mock_kill,
                ):
                    terminate_task_worker(task_id, process.pid)
                mock_killpg.assert_not_called()
                mock_kill.assert_called_once_with(process.pid, 0)
            assert process.poll() is None
            assert not exit_file.exists()
            assert not tmp_file.exists()
            assert not (tmp_path / "dispatcher.lock.stops" / f"{task_id}.stop").exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_terminate_task_worker_preserves_best_effort_without_procfs(
    tmp_path, monkeypatch
):
    """Confirmed procfs absence keeps best-effort signaling and tmp cleanup."""
    task_id = "task-xyz"
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    exit_file = logs_dir / f"worker_{task_id}.exit"
    tmp_file = logs_dir / f"worker_{task_id}.exit.tmp"
    exit_file.write_text("0")
    tmp_file.write_text("0")
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)
    real_stat = Path.stat

    def stat_without_procfs(path, *args, **kwargs):
        if str(path) == "/proc":
            raise FileNotFoundError
        return real_stat(path, *args, **kwargs)

    with (
        patch("dispatcher.process_manager.IS_WINDOWS", False),
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("pathlib.Path.stat", new=stat_without_procfs),
        patch(
            "dispatcher.process_manager.terminate_process_group", return_value=True
        ) as mock_term,
    ):
        terminate_task_worker(task_id, 9876)
    mock_term.assert_called_once_with(None, 9876)
    assert not exit_file.exists()
    assert not tmp_file.exists()
    marker = tmp_path / "dispatcher.lock.stops" / f"{task_id}.stop"
    assert marker.read_text().strip() == "9876"


def test_terminate_process_group_refuses_self_pgid():
    """Never signal our own process group (prevents CLI move from self-killing)."""
    my_pgid = os.getpgrp()
    with patch("dispatcher.process_manager.os.killpg") as mock_killpg:
        terminate_process_group(None, my_pgid, grace=0.1)
        mock_killpg.assert_not_called()


def test_terminate_process_group_without_getpgrp_refuses_own_pid(monkeypatch):
    """The Windows fallback still guards against signaling the current process."""
    monkeypatch.delattr(os, "getpgrp")
    my_pid = os.getpid()
    proc = MagicMock(pid=1234)
    with (
        patch("dispatcher.process_manager.os.getpgid", return_value=my_pid),
        patch("dispatcher.process_manager.os.killpg") as mock_killpg,
    ):
        terminate_process_group(proc, 1234, grace=0.1)
        mock_killpg.assert_not_called()


def test_get_active_profile_from_hermes_home(monkeypatch):
    """Infer profile name from HERMES_HOME=.../profiles/<name> when HERMES_PROFILE is unset."""
    from paths import get_active_profile

    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/home/test/.hermes/profiles/zf-builder")
    assert get_active_profile() == "zf-builder"

    monkeypatch.setenv("HERMES_HOME", "/home/test/.hermes")
    assert get_active_profile() is None

    monkeypatch.setenv("HERMES_PROFILE", "zf-reviewer")
    assert get_active_profile() == "zf-reviewer"
