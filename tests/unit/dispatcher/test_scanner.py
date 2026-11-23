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
                ("scan_on_idle", "true"),
                ("idle_scan_cooldown_minutes_board-a", "not-a-number"),
                ("idle_scan_max_todo_board-a", "stale-value"),
            ]

    config = scanner.get_scanner_cron_config("board-a", cursor=SettingsCursor())

    assert config["scan_on_idle"] is True
    assert config["idle_scan_cooldown_minutes"] == DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES
    assert config["idle_scan_max_todo"] == DEFAULT_IDLE_SCAN_MAX_TODO


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
        patch.object(scanner, "get_hermes_home", return_value=tmp_path),
        patch.object(scanner, "is_pid_alive", return_value=True),
        patch.object(scanner, "process_matches_identity", return_value=False),
        patch.object(scanner.os, "kill") as mock_kill,
        patch.object(scanner.os, "killpg") as mock_killpg,
        patch.object(scanner, "cleanup_process_artifacts"),
    ):
        scanner.kill_board_scanner("board-a")

    mock_kill.assert_not_called()
    mock_killpg.assert_not_called()
    assert not pid_file.exists()


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


def test_spawn_board_scanner_ignores_reused_pid_file(tmp_path):
    """A live unrelated PID in the persisted file does not suppress a scan."""
    _active_scanners.clear()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "scanner_board-a.pid").write_text("424242")
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
            patch.object(scanner.subprocess, "Popen", return_value=proc) as mock_popen,
        ):
            pid = scanner.spawn_board_scanner("board-a")

        assert pid == 515151
        mock_popen.assert_called_once()
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
