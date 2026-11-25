import json
import subprocess
import sys

import pytest

from dispatcher.process_manager import build_shell_wrapper
from dispatcher.worker_spawner import build_worker_command


def test_external_worker_command_expands_protocol_placeholders(monkeypatch):
    monkeypatch.setenv("ZEROFACTORY_WORKER_COMMAND", "omp")
    monkeypatch.setenv(
        "ZEROFACTORY_WORKER_ARGS",
        '["run", "--message", "{prompt}", "--cwd", "{workspace}", "{task_id}", "{role}"]',
    )

    assert build_worker_command("do work", "task-1", "/repo", "zf-builder") == [
        "omp",
        "run",
        "--message",
        "do work",
        "--cwd",
        "/repo",
        "task-1",
        "zf-builder",
    ]


def test_external_worker_command_preserves_literal_braces(monkeypatch):
    monkeypatch.setenv("ZEROFACTORY_WORKER_COMMAND", "omp")
    monkeypatch.setenv(
        "ZEROFACTORY_WORKER_ARGS",
        json.dumps(
            [
                "run",
                "--config",
                '{"model":"x"}',
                "--prompt",
                "{prompt}",
                "--label",
                "literal-{unknown}",
            ]
        ),
    )

    assert build_worker_command("do {task_id}", "task-1", "/repo", "zf-builder") == [
        "omp",
        "run",
        "--config",
        '{"model":"x"}',
        "--prompt",
        "do {task_id}",
        "--label",
        "literal-{unknown}",
    ]


def test_external_worker_argv_survives_shell_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEROFACTORY_WORKER_COMMAND", sys.executable)
    monkeypatch.setenv(
        "ZEROFACTORY_WORKER_ARGS",
        json.dumps(
            [
                "-c",
                "import json, sys; print(json.dumps(sys.argv[1:]))",
                '{"model":"x"}',
                "literal-{",
                "--prompt={prompt}",
            ]
        ),
    )

    prompt = "do work {task_id} now"
    command = build_worker_command(prompt, "task-1", "/repo", "zf-builder")
    assert command is not None
    exit_prefix = tmp_path / "worker"
    result = subprocess.run(
        build_shell_wrapper(command, exit_prefix),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert json.loads(result.stdout) == [
        '{"model":"x"}',
        "literal-{",
        "--prompt=do work {task_id} now",
    ]
    assert (tmp_path / "worker.exit").read_text() == "0\n"


def test_external_worker_command_rejects_invalid_args(monkeypatch):
    monkeypatch.setenv("ZEROFACTORY_WORKER_COMMAND", "omp")
    monkeypatch.setenv("ZEROFACTORY_WORKER_ARGS", "{}")
    with pytest.raises(ValueError, match="JSON array"):
        build_worker_command("prompt", "task", "/repo", "zf-builder")


def test_standalone_dashboard_exposes_ui_and_task_api():
    from dashboard.standalone import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/" in paths
    assert "/api/plugins/zerofactory/tasks" in paths
