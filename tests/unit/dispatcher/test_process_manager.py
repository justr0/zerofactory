"""Unit tests for dispatcher/process_manager.py: process group termination and worker cleanup."""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dispatcher.config import _active_workers
from dispatcher.process_manager import (
    ProcessState,
    build_shell_wrapper,
    cleanup_process_artifacts,
    get_exit_file_path,
    get_signal_file_path,
    get_process_exit_code,
    inspect_process_identity,
    is_pid_alive,
    process_matches_identity,
    read_worker_signal,
    stop_task_worker,
    terminate_process_group,
    terminate_task_worker,
    terminate_worker_process,
)


def _spawn_test_process(command, *, isolated_session):
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=isolated_session and os.name != "nt",
    )


def _require_live_identity_inspection():
    """Skip live identity tests when the platform inspection backend is unavailable."""
    if os.name == "nt":
        if shutil.which("powershell") is None:
            pytest.skip("PowerShell process identity inspection is unavailable")
        return
    try:
        if not Path("/proc").is_dir() or not Path("/proc/self/cmdline").read_bytes():
            pytest.skip("live process identity inspection is unavailable")
    except OSError:
        pytest.skip("live process identity inspection is unavailable")


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
            return_value=b"python\0-c\0task-123\0",
        ),
    ):
        assert process_matches_identity(1234, "scanner_no_such_board_marker") is False


def test_process_matches_identity_accepts_real_worker_prefix_and_scanner_exit_path():
    """Recognize production supervisor argv for workers and scanner artifacts."""
    for task_id, suffix in (("task-123", ""), ("scanner_board-a", ".exit")):
        posix, procfs = _procfs_host()
        with (
            posix,
            procfs,
            patch(
                "pathlib.Path.read_bytes",
                return_value=(
                    f"python\0/dispatcher/_worker_wrapper.py\0/logs/worker_{task_id}{suffix}\0"
                    "hermes\0-p\0zf-builder\0"
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
    for result in (
        MagicMock(returncode=1, stdout=""),
        MagicMock(returncode=0, stdout="  \n"),
    ):
        with (
            patch("dispatcher.process_manager.IS_WINDOWS", True),
            patch("dispatcher.process_manager.subprocess.run", return_value=result),
        ):
            assert inspect_process_identity(42152, "zf-jae-035201b6") is None
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups: os.killpg and os.getpgid do not exist on Windows",
)
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
    """Both platforms use the Python supervisor and preserve the command argv."""
    cmd = ["hermes", "-p", "zf-builder", "chat"]
    exit_prefix = "/tmp/test_exit"
    with patch("dispatcher.process_manager.IS_WINDOWS", False):
        wrapped = build_shell_wrapper(cmd, exit_prefix)

    wrapper_py = Path(sys.modules["dispatcher.process_manager"].__file__).with_name(
        "_worker_wrapper.py"
    )
    assert wrapped == [sys.executable, str(wrapper_py), exit_prefix, *cmd]


def test_build_shell_wrapper_windows():
    """Windows gets the Python helper next to this module, and the helper ships with it."""
    cmd = ["hermes", "-p", "zf-builder", "chat"]
    with patch("dispatcher.process_manager.IS_WINDOWS", True):
        wrapped = build_shell_wrapper(cmd, "logs/worker_a.exit")
    wrapper_py = Path(sys.modules["dispatcher.process_manager"].__file__).with_name(
        "_worker_wrapper.py"
    )
    assert wrapped == [sys.executable, str(wrapper_py), "logs/worker_a.exit", *cmd]
    assert wrapper_py.is_file()


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
    """The Python supervisor argv carries the scanner-specific identity."""
    cmdline = (
        b"python\0/dispatcher/_worker_wrapper.py\0/home/test/.hermes/logs/"
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


def test_terminate_task_worker_stops_real_wrapped_worker_and_cleans_artifacts(
    tmp_path, monkeypatch
):
    """The production wrapper prefix validates, signals, and cleans its worker."""
    _require_live_identity_inspection()
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
    _require_live_identity_inspection()
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


def test_stop_task_worker_skips_recycled_pid():
    """A live PID with a mismatched task marker must not receive a signal."""
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch(
            "dispatcher.process_manager.inspect_process_identity", return_value=False
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
            "dispatcher.process_manager.inspect_process_identity", return_value=False
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
            "dispatcher.process_manager.inspect_process_identity", return_value=False
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
    """Group SIGINT waits for child teardown and preserves its SIGKILL status."""
    if os.name == "nt":
        return
    task_id = "task-signal"
    exit_prefix = tmp_path / f"worker_{task_id}"
    ready_file = tmp_path / "ready"
    log_path = tmp_path / "worker.log"
    child_code = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
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
            child_pid = int(ready_file.read_text())
            os.killpg(proc.pid, signal.SIGINT)
            assert proc.wait(timeout=5) == 130
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    marker = json.loads(exit_prefix.with_suffix(".signal").read_text())
    assert marker == {
        "signal_number": 2,
        "signal_name": "SIGINT",
        "task_id": task_id,
        "elapsed_seconds": marker["elapsed_seconds"],
        "turn_state": "interrupted",
        "child_exit_code": -9,
        "child_signal_number": 9,
    }
    assert int(exit_prefix.with_suffix(".exit").read_text()) == 130
    assert b"task_id=task-signal" in log_path.read_bytes()
    assert marker["elapsed_seconds"] >= 0


def test_supervisor_records_child_only_sigkill_status(tmp_path):
    """A child-only SIGKILL survives the wrapper as signal 9, not unknown/137."""
    if os.name == "nt":
        pytest.skip("POSIX child wait statuses are required")
    prefix = tmp_path / "worker_child-killed"
    ready = tmp_path / "child-pid"
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid())); time.sleep(30)",
        str(ready),
    ]
    proc = subprocess.Popen(
        build_shell_wrapper(command, prefix), start_new_session=True
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists()
        os.kill(int(ready.read_text()), signal.SIGKILL)
        assert proc.wait(timeout=5) == 137
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    marker = json.loads(prefix.with_suffix(".signal").read_text())
    assert marker["signal_number"] == 9
    assert marker["task_id"] == "child-killed"
    assert int(prefix.with_suffix(".exit").read_text()) == 137


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


def test_terminate_task_worker_skips_pid_for_another_task():
    """A live PID whose command line names another task is left alone."""
    posix, procfs = _procfs_host()
    with (
        posix,
        procfs,
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch(
            "dispatcher.process_manager.Path.read_bytes",
            return_value=b"hermes -q task-abc",
        ),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts") as mock_cleanup,
    ):
        terminate_task_worker("task-xyz", 9876)
        mock_term.assert_not_called()
        mock_cleanup.assert_called_once_with("task-xyz")


def _live_handle(pid):
    """A registry handle for ``pid`` whose process has not exited yet."""
    proc = MagicMock(pid=pid)
    proc.poll.return_value = None
    return proc


def test_terminate_task_worker_signals_a_live_registry_handle_without_inspecting(
    tmp_path, monkeypatch
):
    """A handle this process holds for exactly that PID is proof: nothing is queried."""
    proc = _live_handle(4321)
    monkeypatch.setitem(_active_workers, "task-xyz", proc)
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.inspect_process_identity") as mock_inspect,
        patch(
            "dispatcher.process_manager.terminate_process_group", return_value=True
        ) as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts") as mock_cleanup,
    ):
        terminate_task_worker("task-xyz", 4321)
    mock_inspect.assert_not_called()
    mock_term.assert_called_once_with(None, 4321)
    mock_cleanup.assert_called_once_with("task-xyz")
    assert "task-xyz" not in _active_workers
    marker = tmp_path / "dispatcher.lock.stops" / "task-xyz.stop"
    assert marker.read_text().strip() == "4321"


def test_terminate_worker_process_signals_a_live_registry_handle_without_inspecting(
    tmp_path, monkeypatch
):
    """The reaper's own handle is the identity proof, so no command line is read.

    On Windows that read is a PowerShell process with a five-second timeout; a
    registry stop neither waits for it nor fails closed while it is slow.
    """
    proc = _live_handle(4321)
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.inspect_process_identity") as mock_inspect,
        patch(
            "dispatcher.process_manager.terminate_process_group", return_value=True
        ) as mock_term,
    ):
        terminate_worker_process(proc, 4321, task_id="task-xyz")
    mock_inspect.assert_not_called()
    mock_term.assert_called_once_with(proc, 4321)


def test_terminate_worker_process_does_not_let_a_stale_handle_steer_the_signal(
    tmp_path, monkeypatch
):
    """A handle for an earlier worker of the task never picks the process group.

    The PID is proven by its own command line, so it is signalled, but by its own
    PID: the stale handle names another worker's process group.
    """
    stale = _live_handle(1111)
    win, run = _windows_cmdline(
        "python C:\\zf\\_worker_wrapper.py C:\\logs\\worker_task-xyz C:\\bin\\hermes.exe"
    )
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)
    with (
        win,
        run,
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch(
            "dispatcher.process_manager.terminate_process_group", return_value=True
        ) as mock_term,
    ):
        terminate_worker_process(stale, 4321, task_id="task-xyz")
    mock_term.assert_called_once_with(None, 4321)


def test_terminate_without_a_handle_skips_an_unidentifiable_pid(tmp_path, monkeypatch):
    """With no handle, a live PID whose command line cannot be read is left alone.

    That PID comes from the DB and may have been recycled. An unreadable command
    line is not proof of anything, so both kill paths fail closed.
    """
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)
    queries = (
        {"return_value": MagicMock(returncode=1, stdout="")},
        {"return_value": MagicMock(returncode=0, stdout=" \n")},
        {"side_effect": OSError("PowerShell unavailable")},
    )
    for query in queries:
        with (
            patch("dispatcher.process_manager.IS_WINDOWS", True),
            patch("dispatcher.process_manager.subprocess.run", **query),
            patch("dispatcher.process_manager.is_pid_alive", return_value=True),
            patch("dispatcher.process_manager.terminate_process_group") as mock_term,
            patch(
                "dispatcher.process_manager.cleanup_process_artifacts"
            ) as mock_cleanup,
        ):
            terminate_worker_process(None, 4321, task_id="task-unreadable")
            terminate_task_worker("task-unreadable", 4321)
        mock_term.assert_not_called()
        mock_cleanup.assert_called_once_with("task-unreadable")
    assert not (tmp_path / "dispatcher.lock.stops" / "task-unreadable.stop").exists()


def test_terminate_skip_log_names_the_offending_command_line(tmp_path, monkeypatch):
    """The operator can see what the recycled PID actually is before any stop."""
    win, run = _windows_cmdline("C:\\Windows\\System32\\notepad.exe")
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    monkeypatch.setattr("dispatcher.process_manager.get_hermes_home", lambda: tmp_path)
    with (
        win,
        run,
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
        patch("dispatcher.process_manager.cleanup_process_artifacts"),
        patch("dispatcher.process_manager._log") as mock_log,
    ):
        terminate_worker_process(None, 4321, task_id="task-xyz")
        terminate_task_worker("task-xyz", 4321)
    mock_term.assert_not_called()
    assert mock_log.warning.call_count == 2
    for call in mock_log.warning.call_args_list:
        template, logged_pid, logged_task, cmdline = call.args
        assert (logged_pid, logged_task) == (4321, "task-xyz")
        assert "notepad.exe" in cmdline
        assert cmdline in template % (logged_pid, logged_task, cmdline)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups: os.killpg and os.getpgid do not exist on Windows",
)
def test_terminate_process_group_refuses_self_pgid():
    """Never signal our own process group (prevents CLI move from self-killing)."""
    my_pgid = os.getpgrp()
    with patch("dispatcher.process_manager.os.killpg") as mock_killpg:
        terminate_process_group(None, my_pgid, grace=0.1)
        mock_killpg.assert_not_called()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups: os.killpg and os.getpgid do not exist on Windows",
)
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


def test_terminate_process_group_windows_kills_the_tree_by_pid():
    """No process groups on Windows: one tree kill aimed at the pid, then wait on the handle."""
    unattached = 2**22 + 7
    proc = MagicMock(pid=unattached)
    with (
        patch("dispatcher.process_manager.IS_WINDOWS", True),
        # The ancestor guard reads a tool-help snapshot on Windows: an empty one
        # leaves the fake pid nobody's ancestor without touching ctypes.windll.
        patch("dispatcher.process_manager.parent_pid_map", return_value={}),
        patch("dispatcher.process_manager.kill_tree") as mock_kill_tree,
    ):
        assert terminate_process_group(proc, unattached) is True
        mock_kill_tree.assert_called_once_with(unattached)
        proc.wait.assert_called_once()


def test_terminate_process_group_windows_refuses_own_pid():
    """The guards run before the platform split, so the tree kill never targets this process."""
    with (
        patch("dispatcher.process_manager.IS_WINDOWS", True),
        patch("dispatcher.process_manager.kill_tree") as mock_kill_tree,
    ):
        assert terminate_process_group(None, os.getpid()) is False
        mock_kill_tree.assert_not_called()


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
