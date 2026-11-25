"""Auto-start scope: a one-shot `hermes zerofactory <command>` process must not
start a background dispatcher (regression: a one-off CLI re-route spawned a
reviewer from a dispatcher thread that died with the command)."""

import os
import sys
from unittest.mock import MagicMock, patch

import zerofactory as zf

ONE_OFF_ARGV = (
    ["hermes", "zerofactory", "move", "zf-1", "todo"],
    ["hermes", "zerofactory", "dispatch"],
    ["C:/py/python.exe", "-m", "hermes_cli.main", "zerofactory", "list"],
    ["hermes", "--profile", "default", "zerofactory", "recover", "zf-1"],
)
LONG_LIVED_ARGV = (
    ["hermes", "gateway", "run"],
    ["hermes", "chat"],
    ["hermes", "-p", "zf-builder", "--cli", "chat", "-q", "Task ID: zf-1"],
    ["C:/py/python.exe", "C:/zf/dashboard/standalone.py"],
)


def test_one_off_plugin_cli_is_detected():
    for argv in ONE_OFF_ARGV:
        with patch.object(sys, "argv", argv):
            assert zf._is_one_off_cli_invocation() is True, argv


def test_long_lived_processes_are_not_one_off():
    for argv in LONG_LIVED_ARGV:
        with patch.object(sys, "argv", argv):
            assert zf._is_one_off_cli_invocation() is False, argv


def test_auto_start_is_skipped_for_one_off_cli_without_touching_the_db():
    env_without_opt_out = {
        "ZEROFACTORY_SKIP_DISPATCHER": "",
        "ZEROFACTORY_DISABLE_DISPATCHER": "",
    }
    with (
        patch.dict(os.environ, env_without_opt_out),
        patch.object(sys, "argv", ["hermes", "zerofactory", "move", "zf-1", "todo"]),
        patch.object(zf, "init_db") as mock_init,
        patch.object(zf, "start_background_dispatcher") as mock_start,
    ):
        assert zf._should_auto_start_dispatcher() is False
        zf.register(MagicMock())
    mock_init.assert_not_called()
    mock_start.assert_not_called()
