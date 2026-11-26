"""Unit tests for dispatcher.scanner.reap_active_scanners."""

from unittest.mock import MagicMock, patch

import dispatcher
import dispatcher.config
import settings
from dispatcher import scanner
from dispatcher.config import _active_scanners
from dispatcher.process_manager import ProcessState
from settings import DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES, DEFAULT_IDLE_SCAN_MAX_TODO


def test_get_scanner_cron_config_falls_back_for_invalid_numeric_settings():
    """Malformed stored scanner numbers use defaults instead of aborting."""

    class SettingsCursor:
        def execute(self, _query):
            return self

        def fetchall(self):
            return [
                ("scan_on_idle", " true "),
                ("idle_scan_cooldown_minutes_board-a", "not-a-number"),
                ("idle_scan_max_todo_board-a", "stale-value"),
            ]

    config = scanner.get_scanner_cron_config("board-a", cursor=SettingsCursor())

    assert config["scan_on_idle"] is True
    assert config["idle_scan_cooldown_minutes"] == DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES
    assert config["idle_scan_max_todo"] == DEFAULT_IDLE_SCAN_MAX_TODO


def test_get_scanner_cron_config_accepts_trimmed_environment_boolean(monkeypatch):
    """Environment overrides retain whitespace-tolerant boolean parsing."""
    monkeypatch.setenv("ZEROFACTORY_SCAN_ON_IDLE", " yes ")

    config = scanner.get_scanner_cron_config("board-a", cursor=MagicMock())

    assert config["scan_on_idle"] is True


def test_get_active_scanner_pids_preserves_embedded_scanner_in_slug(tmp_path):
    """PID-file parsing strips only the filename's scanner prefix."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "scanner_foo_scanner_bar.pid").write_text("424242")

    with (
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=True),
    ):
        active = scanner.get_active_scanner_pids()

    assert active == {"foo_scanner_bar": 424242}


def test_get_active_scanner_pids_discards_live_unrelated_process(tmp_path):
    """A reused live PID is stale unless it still identifies as this scanner."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")

    with (
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=False),
    ):
        active = scanner.get_active_scanner_pids()

    assert active == {}
    assert not pid_file.exists()


def test_kill_board_scanner_does_not_signal_reused_pid(tmp_path):
    """Cleanup removes a stale PID file without signaling its unrelated owner."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")

    with (
        patch.object(scanner, "IS_WINDOWS", False),
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=False),
        patch.object(scanner.os, "kill") as mock_kill,
        patch.object(scanner.os, "killpg", create=True) as mock_killpg,
        patch.object(scanner, "cleanup_process_artifacts"),
    ):
        scanner.kill_board_scanner("board-a")

    mock_kill.assert_not_called()
    mock_killpg.assert_not_called()
    assert not pid_file.exists()


def test_kill_board_scanner_retains_live_pid_when_signaling_fails_and_suppresses_spawn(
    tmp_path, monkeypatch
):
    """Failed cross-process termination keeps the duplicate-spawn guard."""
    _active_scanners.clear()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with (
        patch.object(scanner, "IS_WINDOWS", False),
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=True),
        patch.object(scanner.os, "killpg", side_effect=PermissionError, create=True),
        patch.object(scanner.os, "kill", side_effect=PermissionError),
        patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
    ):
        assert scanner.kill_board_scanner("board-a") is False
        assert pid_file.exists()
        assert scanner.get_active_scanner_pids() == {"board-a": 424242}

        with (
            patch.dict(
                scanner.os.environ,
                {
                    "ZEROFACTORY_SKIP_WORKER_SPAWN": "",
                    "ZEROFACTORY_SKIP_SCANNER_SPAWN": "",
                    "ZEROFACTORY_SCAN_ON_IDLE": "true",
                },
            ),
            patch.object(
                scanner,
                "get_scanner_cron_config",
                wraps=scanner.get_scanner_cron_config,
            ) as mock_config,
            patch.object(scanner.subprocess, "Popen") as mock_popen,
        ):
            assert scanner.spawn_board_scanner("board-a", tmp_path) is None

        mock_config.assert_not_called()
    mock_popen.assert_not_called()
    mock_cleanup.assert_not_called()


def test_kill_board_scanner_retains_pid_until_live_process_exits(tmp_path):
    """A delivered termination signal is not proof that the scanner has exited."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")

    with (
        patch.object(scanner, "IS_WINDOWS", False),
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=True),
        patch.object(scanner.os, "getpgid", return_value=424242, create=True),
        patch.object(scanner.os, "killpg", create=True) as mock_killpg,
        patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
    ):
        assert scanner.kill_board_scanner("board-a") is False

    mock_killpg.assert_called_once()
    mock_cleanup.assert_not_called()
    assert pid_file.exists()


def test_kill_board_scanner_retains_pid_when_windows_tree_kill_fails(tmp_path):
    """A failed Windows tree kill does not erase a still-live scanner PID."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")

    with (
        patch.object(scanner, "IS_WINDOWS", True),
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=True),
        patch.object(scanner, "kill_tree", return_value=False) as mock_kill,
        patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
    ):
        assert scanner.kill_board_scanner("board-a") is False

    mock_kill.assert_called_once_with(424242)
    mock_cleanup.assert_not_called()
    assert pid_file.exists()


def test_unreadable_pid_record_blocks_stop_and_spawn(tmp_path, monkeypatch):
    """Lock failures preserve the PID record and fail closed for spawning."""
    _active_scanners.clear()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    try:
        with (
            patch.object(scanner, "get_hermes_home", return_value=tmp_path),
            patch.object(
                scanner, "_scanner_pid_file_lock", side_effect=PermissionError
            ),
            patch.dict(
                scanner.os.environ,
                {
                    "ZEROFACTORY_SKIP_WORKER_SPAWN": "",
                    "ZEROFACTORY_SKIP_SCANNER_SPAWN": "",
                    "ZEROFACTORY_SCAN_ON_IDLE": "true",
                },
            ),
            patch.object(
                scanner,
                "get_scanner_cron_config",
                return_value={"scan_on_idle": True},
            ) as mock_config,
            patch.object(scanner.subprocess, "Popen") as mock_popen,
        ):
            assert scanner.kill_board_scanner("board-a") is False
            assert pid_file.read_text() == "424242"
            assert scanner.spawn_board_scanner("board-a", tmp_path) is None

        mock_config.assert_not_called()
        mock_popen.assert_not_called()
        assert pid_file.read_text() == "424242"
    finally:
        _active_scanners.clear()


def test_kill_board_scanner_cleans_pid_after_process_is_confirmed_dead(tmp_path):
    """Successful termination removes the persisted liveness record."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")

    with (
        patch.object(scanner, "IS_WINDOWS", False),
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", side_effect=[True, False]),
        patch.object(scanner, "process_matches_identity", return_value=True),
        patch.object(scanner.os, "getpgid", return_value=424242, create=True),
        patch.object(scanner.os, "killpg", create=True) as mock_killpg,
        patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
    ):
        assert scanner.kill_board_scanner("board-a") is True

    mock_killpg.assert_called_once()
    assert not pid_file.exists()
    mock_cleanup.assert_called_once_with("scanner_board-a")


def test_kill_board_scanner_does_not_remove_replacement_pid_record(tmp_path):
    """Cleanup leaves a replacement PID published while the old PID exits."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")

    def publish_replacement(*_args):
        scanner._write_scanner_pid_file(pid_file, 515151)

    with (
        patch.object(scanner, "IS_WINDOWS", False),
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", side_effect=[True, False, True]),
        patch.object(scanner, "process_matches_identity", return_value=True),
        patch.object(scanner.os, "getpgid", return_value=424242, create=True),
        patch.object(
            scanner.os, "killpg", side_effect=publish_replacement, create=True
        ),
        patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
    ):
        assert scanner.kill_board_scanner("board-a") is True
        assert pid_file.read_text() == "515151"
        assert scanner.get_active_scanner_pids() == {"board-a": 515151}
        mock_cleanup.assert_not_called()


def test_kill_board_scanner_retains_live_owned_process_without_pid_file(tmp_path):
    """A local Popen handle remains authoritative if identity inspection fails."""
    _active_scanners.clear()
    proc = MagicMock()
    proc.pid = 424242
    proc.poll.return_value = None
    _active_scanners["board-a"] = proc

    try:
        with (
            patch.object(scanner, "get_hermes_home", return_value=tmp_path),
            patch.object(scanner, "is_pid_alive", return_value=False),
            patch.object(scanner, "process_matches_identity", return_value=False),
            patch.object(scanner, "terminate_process_group"),
            patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
        ):
            assert scanner.kill_board_scanner("board-a") is False
            assert scanner.get_active_scanner_pids() == {"board-a": 424242}

        mock_cleanup.assert_not_called()
        assert _active_scanners["board-a"] is proc
    finally:
        _active_scanners.clear()


def test_get_active_scanner_pids_keeps_matching_live_scanner(tmp_path):
    """A persisted PID remains active when the scanner identity matches."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")

    with (
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=True),
    ):
        active = scanner.get_active_scanner_pids()

    assert active == {"board-a": 424242}
    assert pid_file.exists()


def test_spawn_board_scanner_ignores_reused_pid_file(tmp_path, monkeypatch):
    """A live unrelated PID in the persisted file does not suppress a scan."""
    _active_scanners.clear()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "scanner_board-a.pid").write_text("424242")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    proc = MagicMock()
    proc.pid = 515151

    try:
        with (
            patch.dict(
                scanner.os.environ,
                {
                    "ZEROFACTORY_SKIP_WORKER_SPAWN": "",
                    "ZEROFACTORY_SKIP_SCANNER_SPAWN": "",
                },
            ),
            patch.object(scanner, "get_hermes_home", return_value=tmp_path),
            patch.object(scanner, "is_pid_alive", return_value=True),
            patch.object(scanner, "process_matches_identity", return_value=False),
            patch.object(
                scanner,
                "get_scanner_cron_config",
                return_value={"scan_on_idle": True},
            ),
            patch.object(scanner, "resolve_hermes_bin", return_value="hermes"),
            patch.object(scanner, "sync_repo_main"),
            patch.object(scanner.subprocess, "Popen", return_value=proc) as mock_popen,
        ):
            pid = scanner.spawn_board_scanner("board-a", tmp_path)

        assert pid == 515151
        mock_popen.assert_called_once()
        assert (logs_dir / "scanner_board-a.pid").read_text() == "515151"
    finally:
        _active_scanners.clear()


def test_spawn_board_scanner_rechecks_pid_replaced_during_stale_cleanup(
    tmp_path, monkeypatch
):
    """A live replacement discovered after stale cleanup suppresses spawning."""
    _active_scanners.clear()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_board-a.pid"
    pid_file.write_text("424242")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cleanup_raced = False

    def replace_stale_record(path, expected_pid):
        nonlocal cleanup_raced
        if path == pid_file and expected_pid == 424242 and not cleanup_raced:
            cleanup_raced = True
            scanner._write_scanner_pid_file(path, 515151)
            return False
        return original_unlink(path, expected_pid)

    original_unlink = scanner._unlink_scanner_pid_file
    try:
        with (
            patch.dict(
                scanner.os.environ,
                {
                    "ZEROFACTORY_SKIP_WORKER_SPAWN": "",
                    "ZEROFACTORY_SKIP_SCANNER_SPAWN": "",
                    "ZEROFACTORY_SCAN_ON_IDLE": "true",
                },
            ),
            patch.object(scanner, "get_hermes_home", return_value=tmp_path),
            patch.object(
                scanner, "is_pid_alive", side_effect=lambda pid: pid == 515151
            ),
            patch.object(scanner, "process_matches_identity", return_value=True),
            patch.object(
                scanner, "_unlink_scanner_pid_file", side_effect=replace_stale_record
            ),
            patch.object(scanner, "get_scanner_cron_config") as mock_config,
            patch.object(scanner.subprocess, "Popen") as mock_popen,
        ):
            assert scanner.spawn_board_scanner("board-a", tmp_path) is None

        assert cleanup_raced
        assert pid_file.read_text() == "515151"
        mock_config.assert_not_called()
        mock_popen.assert_not_called()
    finally:
        _active_scanners.clear()


def test_spawn_board_scanner_closes_log_when_popen_raises(tmp_path, monkeypatch):
    """A failed scanner spawn does not leak its open log file."""
    _active_scanners.clear()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    try:
        with (
            patch.dict(
                scanner.os.environ,
                {
                    "ZEROFACTORY_SKIP_WORKER_SPAWN": "",
                    "ZEROFACTORY_SKIP_SCANNER_SPAWN": "",
                },
            ),
            patch.object(
                scanner,
                "get_scanner_cron_config",
                return_value={"scan_on_idle": True},
            ),
            patch.object(scanner, "resolve_hermes_bin", return_value="hermes"),
            patch.object(scanner, "sync_repo_main"),
            patch.object(
                scanner.subprocess, "Popen", side_effect=OSError("spawn failed")
            ) as mock_popen,
        ):
            pid = scanner.spawn_board_scanner("board-a", tmp_path)

        assert pid is None
        mock_popen.assert_called_once()
        assert mock_popen.call_args.kwargs["stdout"].closed
        assert "board-a" not in _active_scanners
    finally:
        _active_scanners.clear()


def test_spawn_board_scanner_prompt_targets_scanned_board(tmp_path, monkeypatch):
    """Scanner instructions explicitly route findings to the scanned board."""
    _active_scanners.clear()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    proc = MagicMock(pid=515151)

    try:
        with (
            patch.dict(
                scanner.os.environ,
                {
                    "ZEROFACTORY_SKIP_WORKER_SPAWN": "",
                    "ZEROFACTORY_SKIP_SCANNER_SPAWN": "",
                },
            ),
            patch.object(
                scanner,
                "get_scanner_cron_config",
                return_value={"scan_on_idle": True},
            ),
            patch.object(scanner, "get_active_scanner_pids", return_value={}),
            patch.object(scanner, "resolve_hermes_bin", return_value="hermes"),
            patch.object(scanner, "sync_repo_main"),
            patch.object(
                scanner, "build_shell_wrapper", side_effect=lambda cmd, _: cmd
            ),
            patch.object(scanner.subprocess, "Popen", return_value=proc) as mock_popen,
        ):
            assert scanner.spawn_board_scanner("board-b", tmp_path) == proc.pid

        command = mock_popen.call_args.args[0]
        prompt = command[command.index("-q") + 1]
        assert "--board board-b --status todo --assignee zf-builder" in prompt
    finally:
        _active_scanners.clear()


def test_reap_active_scanners_preserves_embedded_scanner_in_slug(tmp_path):
    """Dead PID-file reaping uses the complete board slug for artifact cleanup."""
    _active_scanners.clear()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    pid_file = logs_dir / "scanner_foo_scanner_bar.pid"
    pid_file.write_text("424242")

    try:
        with (
            patch.object(scanner, "get_hermes_home", return_value=tmp_path),
            patch.object(scanner, "is_pid_alive", return_value=False),
            patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
        ):
            reaped = scanner.reap_active_scanners()

        assert reaped == 1
        assert not pid_file.exists()
        mock_cleanup.assert_called_once_with("scanner_foo_scanner_bar")
    finally:
        _active_scanners.clear()


def test_get_scanner_config_uses_supported_defaults_and_exports():
    """Idle scanning is opt-in and only supported scanner knobs are exported."""
    config = scanner.get_scanner_cron_config("board-a", cursor=MagicMock())

    assert config["scan_on_idle"] is False
    assert config["idle_scan_cooldown_minutes"] == DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES
    assert config["idle_scan_max_todo"] == DEFAULT_IDLE_SCAN_MAX_TODO
    for name in ("DEFAULT_SCAN_ON_IDLE", "DEFAULT_IDLE_SCAN_ACTIVE_THRESHOLD"):
        assert not hasattr(settings, name)
        assert not hasattr(dispatcher, name)
        assert not hasattr(dispatcher.config, name)


def test_reap_active_scanners_keeps_running_scanner(tmp_path):
    """Running scanner processes stay in _active_scanners without raising."""
    _active_scanners.clear()
    proc = MagicMock()
    proc.pid = 424242
    proc.poll.return_value = None
    _active_scanners["board-a"] = proc

    try:
        with (
            patch.object(scanner, "get_hermes_home", return_value=tmp_path),
            patch.object(
                scanner,
                "get_process_exit_code",
                return_value=(ProcessState.RUNNING, None),
            ) as mock_exit,
        ):
            reaped = scanner.reap_active_scanners()

        assert reaped == 0
        assert "board-a" in _active_scanners
        mock_exit.assert_called_once_with("scanner_board-a", 424242)
    finally:
        _active_scanners.clear()


def test_reap_active_scanners_reaps_completed_scanner(tmp_path):
    """Completed scanner processes are popped and their artifacts cleaned up."""
    _active_scanners.clear()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    pid_file = logs_dir / "scanner_board-b.pid"
    pid_file.write_text("515151")

    proc = MagicMock()
    proc.pid = 515151
    proc.poll.return_value = 0
    _active_scanners["board-b"] = proc

    try:
        with (
            patch.object(scanner, "get_hermes_home", return_value=tmp_path),
            patch.object(
                scanner,
                "get_process_exit_code",
                return_value=(ProcessState.COMPLETED, 0),
            ) as mock_exit,
            patch.object(scanner, "cleanup_process_artifacts") as mock_cleanup,
        ):
            reaped = scanner.reap_active_scanners()

        assert reaped == 1
        assert "board-b" not in _active_scanners
        assert not pid_file.exists()
        mock_exit.assert_called_once_with("scanner_board-b", 515151)
        mock_cleanup.assert_called_once_with("scanner_board-b")
    finally:
        _active_scanners.clear()
