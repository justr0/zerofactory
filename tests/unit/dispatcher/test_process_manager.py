"""Unit tests for dispatcher/process_manager.py: process group termination and worker cleanup."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

from dispatcher.config import _active_workers
from dispatcher.process_manager import (
    ProcessState,
    build_shell_wrapper,
    cleanup_process_artifacts,
    get_exit_file_path,
    get_signal_file_path,
    get_process_exit_code,
    is_pid_alive,
    process_matches_identity,
    read_worker_signal,
    stop_task_worker,
    terminate_process_group,
    terminate_task_worker,
    terminate_worker_process,
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
        patch("pathlib.Path.exists", return_value=True),
        patch(
            "pathlib.Path.read_bytes",
            return_value=b"hermes -p zf-builder chat unrelated worker",
        ),
    ):
        assert process_matches_identity(1234, "scanner_no_such_board_marker") is False


def test_process_matches_identity_accepts_matching_worker_or_scanner():
    """The wrapper's task-specific exit-file argument identifies its worker."""
    for task_id in ("task-123", "scanner_board-a"):
        with (
            patch("dispatcher.process_manager.IS_WINDOWS", False),
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "pathlib.Path.read_bytes",
                return_value=f"hermes -q prompt worker_{task_id}.exit".encode(),
            ),
        ):
            assert process_matches_identity(1234, task_id) is True


def test_process_matches_identity_reads_windows_command_line():
    """Use the Windows process command line query and require the same marker."""
    result = MagicMock(returncode=0, stdout="python worker_scanner_board-a.exit")
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
    """Verify the wrapper writes normal exit codes and handles worker signals."""
    cmd = ["hermes", "-p", "zf-builder", "chat"]
    exit_prefix = "/tmp/test_exit"
    wrapped = build_shell_wrapper(cmd, exit_prefix)

    assert wrapped[0] == "sh"
    assert wrapped[1] == "-c"
    assert 'prefix="$1"; shift;' in wrapped[2]
    assert "printf" in wrapped[2]
    assert "${prefix}.exit" in wrapped[2]
    assert "${prefix}.signal" in wrapped[2]
    assert "write_signal 15 SIGTERM" in wrapped[2]
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
    """Verify cleanup removes both exit and signal marker artifacts."""
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
        (logs_dir / f"worker_{task_id}.signal").write_text("{}")
        (logs_dir / f"worker_{task_id}.signal.tmp").write_text("{}")

        cleanup_process_artifacts(task_id)
        assert not (logs_dir / f"worker_{task_id}.exit").exists()
        assert not (logs_dir / f"worker_{task_id}.exit.tmp").exists()
        assert not (logs_dir / f"worker_{task_id}.signal").exists()
        assert not (logs_dir / f"worker_{task_id}.signal.tmp").exists()


def test_terminate_task_worker():
    """Verify terminate_task_worker calls process group termination and artifact cleanup."""
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.process_matches_identity", return_value=True),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts") as mock_cleanup,
    ):
        terminate_task_worker("task-xyz", 9876)
        mock_term.assert_called_once_with(None, 9876)
        mock_cleanup.assert_called_once_with("task-xyz")


def test_stop_task_worker_skips_recycled_pid():
    """A live PID with a mismatched task marker must not receive a signal."""
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch(
            "dispatcher.process_manager.process_matches_identity", return_value=False
        ),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts"),
    ):
        stop_task_worker("task-old", pid=9876)
        mock_term.assert_not_called()


def test_stop_task_worker_skips_dead_pid_before_signaling():
    """A PID that died before stop cannot be signaled after possible reuse."""
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=False),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts"),
    ):
        stop_task_worker("task-old", pid=9876)
        mock_term.assert_not_called()


def test_terminate_worker_process_skips_recycled_pid():
    """Worker process-group termination validates identity before signaling."""
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch(
            "dispatcher.process_manager.process_matches_identity", return_value=False
        ),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
    ):
        terminate_worker_process(None, 9876, task_id="task-old")
        mock_term.assert_not_called()


def test_terminate_task_worker_skips_recycled_pid():
    """Direct task termination shares the same fail-closed identity guard."""
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch(
            "dispatcher.process_manager.process_matches_identity", return_value=False
        ),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts"),
    ):
        terminate_task_worker("task-old", 9876)
        mock_term.assert_not_called()


def test_read_worker_signal_validates_task_and_payload(tmp_path):
    """Only a valid marker for the requested task is accepted."""
    signal_file = tmp_path / "worker_task-1.signal"
    with patch(
        "dispatcher.process_manager.get_signal_file_path", return_value=signal_file
    ):
        signal_file.write_text(
            '{"signal_number":15,"signal_name":"SIGTERM","task_id":"task-1",'
            '"elapsed_seconds":16,"turn_state":"interrupted"}'
        )
        marker = read_worker_signal("task-1")
        assert marker is not None
        assert marker["signal_number"] == 15

        signal_file.write_text('{"signal_number":15,"task_id":"other"}')
        assert read_worker_signal("task-1") is None

        signal_file.write_text('{"signal_number":true,"task_id":"task-1"}')
        assert read_worker_signal("task-1") is None


def test_shell_wrapper_records_signal_and_nonzero_exit(tmp_path):
    """A process-group SIGTERM is durable and cannot masquerade as exit 0."""
    if os.name == "nt":
        return
    task_id = "task-signal"
    exit_prefix = tmp_path / f"worker_{task_id}"
    ready_file = tmp_path / "ready"
    log_path = tmp_path / "worker.log"
    child_code = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: os._exit(0)); "
        "pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
    )
    cmd = [sys.executable, "-c", child_code, str(ready_file)]
    with log_path.open("wb") as log_file:
        proc = subprocess.Popen(
            build_shell_wrapper(cmd, exit_prefix),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if ready_file.exists():
                    break
                if proc.poll() is not None:
                    raise AssertionError("wrapper exited before test command was ready")
                time.sleep(0.01)
            assert ready_file.exists()
            os.killpg(proc.pid, signal.SIGTERM)
            assert proc.wait(timeout=5) == 143
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    marker = json.loads(exit_prefix.with_suffix(".signal").read_text())
    assert marker == {
        "signal_number": 15,
        "signal_name": "SIGTERM",
        "task_id": task_id,
        "elapsed_seconds": marker["elapsed_seconds"],
        "turn_state": "interrupted",
    }
    assert int(exit_prefix.with_suffix(".exit").read_text()) == 143
    assert b"task_id=task-signal" in log_path.read_bytes()
    assert marker["elapsed_seconds"] >= 0


def test_shell_wrapper_preserves_normal_nonzero_exit(tmp_path):
    """The wrapper must not turn an ordinary command failure into success."""
    exit_prefix = tmp_path / "worker_task-failed"
    result = subprocess.run(
        build_shell_wrapper(["sh", "-c", "exit 17"], exit_prefix),
        check=False,
        timeout=5,
    )
    assert result.returncode == 17
    assert exit_prefix.with_suffix(".exit").read_text() == "17\n"
    assert not exit_prefix.with_suffix(".signal").exists()


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
