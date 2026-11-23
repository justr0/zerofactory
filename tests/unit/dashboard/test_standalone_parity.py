from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from fastapi.routing import APIRoute


def test_standalone_ui_wires_every_management_surface(initialized_db, default_board):
    from fastapi.testclient import TestClient

    from dashboard.standalone import app

    client = TestClient(app)
    script = client.get("/assets/app.js").text
    html = client.get("/").text
    api = "/api/plugins/zerofactory"

    ui_routes = (
        "/tasks",
        "/boards",
        "/boards/test-clone",
        "/dispatch/status",
        "/dispatch/start",
        "/dispatch/stop",
        "/dispatch/run",
        "/stats",
        "/health/stuck-tasks",
        "/health/reap-stuck",
        "/settings",
        "/settings/langfuse/test",
        "/settings/profiles/sync",
        "/memories/",
        "/sessions/",
        "/cron",
        "/approve",
        "/reject",
        "/recover",
        "/split",
        "/report-bug",
        "/comments",
        "/stop",
    )
    for path in ui_routes:
        assert path in script or path in html, path

    routes = {
        (method, route.path.removeprefix(api))
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(api)
        for method in route.methods
    }
    required = {
        ("GET", "/tasks"),
        ("POST", "/tasks"),
        ("GET", "/tasks/{task_id}"),
        ("PATCH", "/tasks/{task_id}"),
        ("POST", "/tasks/{task_id}/move"),
        ("POST", "/tasks/{task_id}/comments"),
        ("GET", "/tasks/{task_id}/comments"),
        ("POST", "/tasks/{task_id}/approve"),
        ("POST", "/tasks/{task_id}/reject"),
        ("POST", "/tasks/{task_id}/recover"),
        ("POST", "/tasks/{task_id}/split"),
        ("POST", "/tasks/{task_id}/report-bug"),
        ("GET", "/boards"),
        ("POST", "/boards"),
        ("PATCH", "/boards/{slug}"),
        ("POST", "/boards/test-clone"),
        ("GET", "/dispatch/status"),
        ("POST", "/dispatch/start"),
        ("POST", "/dispatch/stop"),
        ("POST", "/dispatch/run"),
        ("GET", "/stats"),
        ("GET", "/health/stuck-tasks"),
        ("POST", "/health/reap-stuck"),
        ("GET", "/settings"),
        ("PATCH", "/settings"),
        ("POST", "/settings/langfuse/test"),
        ("POST", "/settings/profiles/sync"),
        ("GET", "/memories"),
        ("DELETE", "/memories/{memory_id}"),
        ("GET", "/sessions"),
        ("POST", "/sessions/{session_id}/stop"),
        ("GET", "/cron"),
        ("PUT", "/cron/{job_id}"),
        ("POST", "/cron/{job_id}/run"),
        ("POST", "/cron/{job_id}/reset"),
    }
    assert required <= routes

    created = client.post(
        f"{api}/tasks",
        json={
            "title": "Standalone parity",
            "description": "Exercise the live API",
            "priority": "P2",
            "assignee": "zf-builder",
            "board_slug": default_board,
            "labels": ["standalone"],
        },
    )
    assert created.status_code == 200, created.text
    task = created.json()
    task_id = task["id"]
    assert client.get(f"{api}/tasks").status_code == 200
    assert (
        client.get(f"{api}/tasks/{task_id}").json()["task"]["title"]
        == "Standalone parity"
    )
    assert (
        client.post(
            f"{api}/tasks/{task_id}/comments",
            json={
                "author": "user",
                "body": "API wiring verified",
            },
        ).status_code
        == 200
    )
    assert client.get(f"{api}/tasks/{task_id}/comments").status_code == 200
    moved = client.post(f"{api}/tasks/{task_id}/move", json={"status": "todo"})
    assert moved.status_code == 200, moved.text

    # Exercise representative read surfaces using the same live board database.
    for endpoint in (
        "boards",
        "stats",
        "settings",
        "dispatch/status",
        "health/stuck-tasks",
        "memories",
        "sessions",
        "cron",
    ):
        response = client.get(f"{api}/{endpoint}")
        assert response.status_code == 200, (endpoint, response.text)
    assert default_board in {
        board["slug"] for board in client.get(f"{api}/boards").json()["boards"]
    }


def test_standalone_legacy_root_tasks_contract_and_static_assets():
    from fastapi.testclient import TestClient

    from dashboard.standalone import app

    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/tasks").status_code == 404
    for asset in ("app.js", "style.css"):
        response = client.get(f"/assets/{asset}")
        assert response.status_code == 200
        assert response.content
        assert "cdn." not in response.text.lower()

    script = client.get("/assets/app.js").text
    assert not re.search(r"[ \t]+$", script, re.MULTILINE)


def test_standalone_cli_smoke_without_external_network(initialized_db):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dashboard.standalone",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            if process.poll() is not None:
                raise AssertionError("standalone CLI exited before serving")
            try:
                with urlopen(base_url, timeout=1) as response:
                    assert response.status == 200
                    assert b'id="root"' in response.read()
                with urlopen(f"{base_url}/assets/app.js", timeout=1) as response:
                    assert response.status == 200
                    assert b"/api/plugins/zerofactory" in response.read()
                break
            except URLError:
                time.sleep(0.1)
        else:
            raise AssertionError("standalone CLI did not become ready")
    finally:
        process.terminate()
        process.wait(timeout=5)
