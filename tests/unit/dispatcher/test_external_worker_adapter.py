import pytest

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
