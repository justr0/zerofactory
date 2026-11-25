"""Unit tests for dispatcher/process_manager.py: process group termination and worker cleanup."""

import json
import os
import signal
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

from dispatcher.config import _active_workers
from dispatcher.process_manager import (
    ProcessState,
    build_shell_wrapper,
    cleanup_process_artifacts,
    get_exit_file_path,
    _process_identity,
    get_process_exit_code,
    is_pid_alive,
    process_matches_identity,
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


def _procfs_host():
    """Pin a POSIX host with procfs so procfs-mocking tests are host-independent."""
    return (
        patch("dispatcher.process_manager.IS_WINDOWS", False),
        patch(
            "dispatcher.process_manager.Path.stat",
            return_value=MagicMock(st_mode=0o040755),
        ),
    )


def _windows_cmdline(stdout, returncode=0):
    return (
        patch("dispatcher.process_manager.IS_WINDOWS", True),
        patch(
            "dispatcher.process_manager.subprocess.run",
            return_value=MagicMock(returncode=returncode, stdout=stdout),
        ),
    )


def test_process_matches_identity_requires_task_specific_marker():
    """A generic Hermes command is not enough to validate a persisted PID."""
    posix, procfs = _procfs_host()
    with (
        posix,
        procfs,
        patch(
            "pathlib.Path.read_bytes",
            return_value=b"hermes -p zf-builder chat unrelated worker",
        ),
    ):
        assert process_matches_identity(1234, "scanner_no_such_board_marker") is False


def test_process_matches_identity_accepts_matching_worker_or_scanner():
    """The wrapper's task-specific exit-file argument identifies its worker."""
    for task_id in ("task-123", "scanner_board-a"):
        posix, procfs = _procfs_host()
        with (
            posix,
            procfs,
            patch(
                "pathlib.Path.read_bytes",
                return_value=f"hermes -q prompt worker_{task_id}.exit".encode(),
            ),
        ):
            assert process_matches_identity(1234, task_id) is True


def test_process_matches_identity_reads_windows_command_line():
    """Use the Windows process command line query and require the same marker."""
    win, run = _windows_cmdline("python worker_scanner_board-a.exit")
    with win, run:
        assert process_matches_identity(1234, "scanner_board-a") is True


def test_process_matches_identity_accepts_windows_wrapper_prefix():
    """The Windows wrapper is launched with the bare exit-file prefix, not the file."""
    win, run = _windows_cmdline(
        '"C:\\py\\python.exe" C:\\zf\\_worker_wrapper.py '
        '"C:\\hermes\\logs\\worker_zf-jae-035201b6" C:\\bin\\hermes.exe -p zf-reviewer'
    )
    with win, run:
        assert process_matches_identity(42152, "zf-jae-035201b6") is True


def test_process_matches_identity_rejects_windows_neighbors():
    """A neighbouring task ID, a sibling artifact and an unrelated command are not the worker."""
    for cmdline in (
        "python C:\\hermes\\logs\\worker_zf-jae-035201b60 C:\\bin\\hermes.exe",
        "python tail -f C:\\hermes\\logs\\worker_zf-jae-035201b6.log",
        "C:\\bin\\hermes.exe -p zf-reviewer --cli chat -q Task",
    ):
        win, run = _windows_cmdline(cmdline)
        with win, run:
            assert process_matches_identity(42152, "zf-jae-035201b6") is False


def test_windows_unreadable_cmdline_is_unknown_not_mismatch():
    """On Windows an unreadable command line is missing evidence, not a mismatch.

    Treating it as a mismatch let a reap run by a dispatcher without a registry
    handle read a live worker as crashed and respawn/queue it as lost.
    """
    for result in (MagicMock(returncode=1, stdout=""), MagicMock(returncode=0, stdout="  \n")):
        with (
            patch("dispatcher.process_manager.IS_WINDOWS", True),
            patch("dispatcher.process_manager.subprocess.run", return_value=result),
        ):
            assert _process_identity(42152, "zf-jae-035201b6") is None
            assert process_matches_identity(42152, "zf-jae-035201b6") is True


def test_terminate_worker_process_signals_identified_windows_worker():
    """A Windows worker whose command line names the task is signalled (was always skipped)."""
    win, run = _windows_cmdline(
        "python C:\\zf\\_worker_wrapper.py C:\\logs\\worker_task-1 C:\\bin\\hermes.exe"
    )
    with (
        win,
        run,
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
    ):
        terminate_worker_process(None, 42152, task_id="task-1")
    mock_term.assert_called_once_with(None, 42152)


def test_terminate_skips_recycled_windows_pid():
    """A live PID whose command line belongs to something else is never signalled."""
    win, run = _windows_cmdline("C:\\Windows\\System32\\notepad.exe")
    with (
        win,
        run,
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts") as mock_cleanup,
    ):
        terminate_worker_process(None, 42152, task_id="task-1")
        terminate_task_worker("task-1", 42152)
    mock_term.assert_not_called()
    mock_cleanup.assert_called_once_with("task-1")


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
    posix, procfs = _procfs_host()
    with (
        posix,
        procfs,
        patch(
            "pathlib.Path.read_bytes",
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
    posix, procfs = _procfs_host()
    with posix, procfs, patch("pathlib.Path.read_bytes", return_value=cmdline):
        assert process_matches_identity(1234, "scanner_board-a") is True


def test_process_matches_identity_preserves_non_proc_fallback():
    """Platforms with confirmed-absent procfs retain best-effort acceptance."""
    with (
        patch("dispatcher.process_manager.IS_WINDOWS", False),
        patch("dispatcher.process_manager.Path.stat", side_effect=FileNotFoundError),
    ):
        assert process_matches_identity(1234, "scanner_board-a") is True


def test_process_matches_identity_rejects_inaccessible_procfs():
    """An error checking procfs must not be mistaken for an absent procfs."""
    with (
        patch("dispatcher.process_manager.IS_WINDOWS", False),
        patch("dispatcher.process_manager.Path.stat", side_effect=PermissionError),
    ):
        assert process_matches_identity(1234, "scanner_board-a") is False


def test_process_matches_identity_rejects_unavailable_proc_command_line():
    """Missing, empty, or unreadable per-PID cmdline data fails closed."""
    posix, procfs = _procfs_host()
    with (
        posix,
        procfs,
        patch(
            "pathlib.Path.read_bytes",
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


def test_terminate_task_worker():
    """Verify terminate_task_worker calls process group termination and artifact cleanup."""
    posix, procfs = _procfs_host()
    with (
        posix,
        procfs,
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.Path.read_bytes", return_value=b"hermes -q task-xyz"),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts") as mock_cleanup,
    ):
        terminate_task_worker("task-xyz", 9876)
        mock_term.assert_called_once_with(None, 9876)
        mock_cleanup.assert_called_once_with("task-xyz")


def test_terminate_task_worker_skips_pid_for_another_task():
    """A live PID whose command line names another task is left alone."""
    posix, procfs = _procfs_host()
    with (
        posix,
        procfs,
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.Path.read_bytes", return_value=b"hermes -q task-abc"),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts") as mock_cleanup,
    ):
        terminate_task_worker("task-xyz", 9876)
        mock_term.assert_not_called()
        mock_cleanup.assert_called_once_with("task-xyz")


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
