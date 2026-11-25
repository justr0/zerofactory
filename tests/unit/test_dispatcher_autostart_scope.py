"""Auto-start scope: the gateway runtime is the one process that starts a
background dispatcher.

Regression 1: a one-shot `hermes zerofactory <command>` process started one and
died with the command, taking its private worker registry with it.
Regression 2: `hermes dashboard` is long-lived, so the one-off check let it start
one. It kept dispatching from the code loaded at ITS start after the gateway had
been restarted onto fixed code, and re-gated tasks that had just been re-routed.
Regression 3: the guard recognised the gateway only by a command line whose first
token is the interpreter. Inside `python -m hermes_cli.main gateway run`,
`sys.argv[0]` is the module file, so after a gateway restart onto that code the
dispatcher never started at all: nothing polled PRs, closed merged tasks or ran a
review.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
GATEWAY_ARGV = (
    ["C:/py/python.exe", "-m", "hermes_cli.main", "gateway", "run"],
    ["C:/py/python.exe", "-X", "utf8", "-m", "hermes_cli.main", "gateway", "run"],
    ["C:/py/python.exe", "C:/hermes/hermes_cli/main.py", "gateway", "run"],
    ["C:/venv/Scripts/hermes.exe", "gateway", "run"],
    ["hermes", "--profile", "default", "gateway", "run"],
    ["hermes", "-p", "zf-builder", "gateway", "run"],
    ["hermes", "gateway", "--accept-hooks", "run"],
    ["hermes", "gateway"],
    ["C:/bin/hermes-gateway.exe"],
    ["C:/py/python.exe", "-m", "gateway.run"],
)
DASHBOARD_ARGV = [
    "C:/py/python.exe",
    "-m",
    "hermes_cli.main",
    "dashboard",
    "--port",
    "9119",
    "--no-open",
]
NOT_A_GATEWAY_ARGV = (
    DASHBOARD_ARGV,
    ["hermes", "dashboard"],
    ["C:/py/python.exe", "C:/zf/dashboard/standalone.py"],
    *[["hermes", "gateway", action] for action in ("status", "stop", "restart")],
    ["hermes", "gateway", "--help"],
    ["hermes", "chat"],
    ["hermes", "chat", "-q", "gateway"],
    ["hermes", "-p", "gateway", "chat"],
    ["hermes", "-p", "zf-builder", "--cli", "chat", "-q", "Task ID: zf-1"],
    ["C:/py/python.exe", "-m", "pytest", "-q", "tests/unit"],
)
ENV_WITHOUT_OPT_OUT = {
    "ZEROFACTORY_SKIP_DISPATCHER": "",
    "ZEROFACTORY_DISABLE_DISPATCHER": "",
}


def _action(argv):
    with patch.object(sys, "argv", argv):
        return zf._gateway_action()


def _argv_inside(tmp_path: Path, mode: str, *tokens: str) -> list[str]:
    """``sys.argv`` as the interpreter really sets it in a process started with
    ``python <mode> hermes_cli.main <tokens>``, read from a stand-in module that
    prints its own.

    The argv lists above are command lines: ``argv[0]`` is the interpreter. A
    ``-m`` or script process never sees itself that way, and that is the only
    view the auto-start guard gets.
    """
    package = tmp_path / "hermes_cli"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    main = package / "main.py"
    main.write_text("import json, sys\nprint(json.dumps(sys.argv))\n", encoding="utf-8")
    target = ["-m", "hermes_cli.main"] if mode == "-m" else [str(main)]
    proc = subprocess.run(
        [sys.executable, *target, *tokens],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    argv = json.loads(proc.stdout.strip().splitlines()[-1])
    assert argv[0].replace("\\", "/").endswith("hermes_cli/main.py"), argv
    assert argv[1:] == list(tokens), argv
    return argv


@pytest.mark.parametrize("mode", ["-m", "script"])
def test_gateway_started_by_the_interpreter_is_recognised_from_inside(tmp_path, mode):
    for tokens, expected in (
        (("gateway", "run"), "run"),
        (("gateway", "--accept-hooks", "run"), "run"),
        (("gateway", "status"), "status"),
        (("gateway", "--help"), None),
        (("dashboard", "--port", "9119", "--no-open"), None),
        (("zerofactory", "dispatch"), None),
    ):
        argv = _argv_inside(tmp_path, mode, *tokens)
        assert _action(argv) == expected, (mode, tokens, argv)


def test_one_off_plugin_cli_is_detected():
    for argv in ONE_OFF_ARGV:
        with patch.object(sys, "argv", argv):
            assert zf._is_one_off_cli_invocation() is True, argv


def test_long_lived_processes_are_not_one_off():
    for argv in LONG_LIVED_ARGV:
        with patch.object(sys, "argv", argv):
            assert zf._is_one_off_cli_invocation() is False, argv


def test_only_the_gateway_runtime_reads_as_a_gateway():
    for argv in GATEWAY_ARGV:
        assert _action(argv) == "run", argv
    for argv in NOT_A_GATEWAY_ARGV:
        assert _action(argv) != "run", argv


def test_gateway_reading_agrees_with_hermes_own_gateway_matcher():
    """Hermes identifies gateways in process tables with
    ``gateway.status.looks_like_gateway_command_line``. The plugin reads the same
    argv; the two must not drift apart."""
    pytest.importorskip("gateway.status")
    from gateway.status import looks_like_gateway_command_line

    for argv in GATEWAY_ARGV + NOT_A_GATEWAY_ARGV:
        if argv[-1] == "gateway":
            continue  # a prompt that is just ``gateway``: Hermes' scan is looser on purpose
        assert (_action(argv) == "run") is looks_like_gateway_command_line(
            " ".join(argv)
        ), argv


def test_auto_start_is_skipped_for_one_off_cli_without_touching_the_db():
    with (
        patch.dict(os.environ, ENV_WITHOUT_OPT_OUT),
        patch.object(sys, "argv", ["hermes", "zerofactory", "move", "zf-1", "todo"]),
        patch.object(zf, "init_db") as mock_init,
        patch.object(zf, "start_background_dispatcher") as mock_start,
    ):
        assert zf._should_auto_start_dispatcher() is False
        zf.register(MagicMock())
    mock_init.assert_not_called()
    mock_start.assert_not_called()


def test_auto_start_is_skipped_for_the_dashboard_without_touching_the_db():
    with (
        patch.dict(os.environ, ENV_WITHOUT_OPT_OUT),
        patch.object(sys, "argv", DASHBOARD_ARGV),
        patch.object(zf, "init_db") as mock_init,
        patch.object(zf, "start_background_dispatcher") as mock_start,
    ):
        assert zf._should_auto_start_dispatcher() is False
        zf.register(MagicMock())
    mock_init.assert_not_called()
    mock_start.assert_not_called()


def test_gateway_runtime_auto_starts_when_the_setting_is_on(tmp_path):
    """The allowlist must not lose the real host: with the setting on, the
    gateway process still starts the dispatcher on plugin load."""
    import zerofactory.dashboard.db as zf_db
    import zerofactory.settings as zf_settings

    db_path = tmp_path / "zerofactory.db"
    db_path.touch()
    with (
        patch.dict(os.environ, ENV_WITHOUT_OPT_OUT),
        patch.object(sys, "argv", _argv_inside(tmp_path, "-m", "gateway", "run")),
        patch.object(zf_db, "get_db_path", lambda: db_path),
        patch.object(
            zf_settings, "load_settings", lambda conn: {"auto_start_dispatcher": True}
        ),
        patch.object(zf, "init_db") as mock_init,
        patch.object(zf, "ensure_builtin_cron_jobs") as mock_cron,
        patch.object(zf, "start_background_dispatcher") as mock_start,
    ):
        assert zf._should_auto_start_dispatcher() is True
        zf.register(MagicMock())
    mock_init.assert_called_once()
    mock_cron.assert_called_once()
    mock_start.assert_called_once()


def test_setting_on_does_not_start_a_dispatcher_in_the_dashboard(tmp_path):
    """Same setting, same database, non-gateway process: nothing starts."""
    import zerofactory.dashboard.db as zf_db
    import zerofactory.settings as zf_settings

    db_path = tmp_path / "zerofactory.db"
    db_path.touch()
    with (
        patch.dict(os.environ, ENV_WITHOUT_OPT_OUT),
        patch.object(sys, "argv", DASHBOARD_ARGV),
        patch.object(zf_db, "get_db_path", lambda: db_path),
        patch.object(
            zf_settings, "load_settings", lambda conn: {"auto_start_dispatcher": True}
        ),
        patch.object(zf, "init_db") as mock_init,
        patch.object(zf, "start_background_dispatcher") as mock_start,
    ):
        assert zf._should_auto_start_dispatcher() is False
        zf.register(MagicMock())
    mock_init.assert_not_called()
    mock_start.assert_not_called()
