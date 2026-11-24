from __future__ import annotations

import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest
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
            "status": "todo",
            "board_slug": default_board,
            "labels": ["standalone"],
        },
    )
    assert created.status_code == 200, created.text
    task = created.json()
    task_id = task["id"]
    assert client.get(f"{api}/tasks").status_code == 200
    created_detail = client.get(f"{api}/tasks/{task_id}").json()["task"]
    assert created_detail["description"] == "Exercise the live API"
    assert created_detail["priority"] == "P2"
    assert created_detail["assignee"] == "zf-builder"
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
    comments = client.get(f"{api}/tasks/{task_id}/comments")
    assert comments.status_code == 200
    assert any(
        comment["body"] == "API wiring verified"
        for comment in comments.json()["comments"]
    )
    moved = client.post(f"{api}/tasks/{task_id}/move", json={"status": "todo"})
    assert moved.status_code == 200, moved.text
    updated = client.patch(
        f"{api}/tasks/{task_id}",
        json={"description": "Updated from standalone", "priority": "P1"},
    )
    assert updated.status_code == 200, updated.text
    detail = client.get(f"{api}/tasks/{task_id}").json()
    assert detail["task"]["description"] == "Updated from standalone"
    assert detail["task"]["priority"] == "P1"
    assert detail["task"]["activity"]

    # Exercise the guidance and reason payloads used by task lifecycle controls.
    recovered = client.post(
        f"{api}/tasks/{task_id}/recover", json={"guidance": "Retry after review"}
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "todo"
    blocked = client.post(
        f"{api}/tasks/{task_id}/move",
        json={"status": "blocked", "reason": "Needs operator input"},
    )
    assert blocked.status_code == 200, blocked.text

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
    root = client.get("/")
    assert root.status_code == 200
    assert 'id="root"' in root.text
    # Keep the historical standalone path response unchanged (API is namespaced).
    legacy_tasks = client.get("/tasks")
    assert legacy_tasks.status_code == 404
    assert legacy_tasks.json() == {"detail": "Not Found"}
    for asset in ("app.js", "style.css"):
        response = client.get(f"/assets/{asset}")
        assert response.status_code == 200
        assert response.content
        assert "cdn." not in response.text.lower()


def test_standalone_ui_create_task_invokes_api_and_renders_result(
    initialized_db, default_board
):
    sync_api = pytest.importorskip("playwright.sync_api")
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    react_umd = repo_root / "node_modules/react/umd/react.production.min.js"
    react_dom_umd = repo_root / "node_modules/react-dom/umd/react-dom.production.min.js"
    if not (react_umd.is_file() and react_dom_umd.is_file()):
        pytest.skip("React UMD assets not found in node_modules; run npm ci")

    from tests.e2e.test_dashboard_ui_playwright import (
        _build_test_app,
        _find_chromium_executable,
        _start_http_bridge,
    )

    httpd, thread, port = _start_http_bridge(_build_test_app())
    try:
        with sync_api.sync_playwright() as playwright:
            launch_options = {"headless": True}
            chromium = _find_chromium_executable()
            if chromium:
                launch_options["executable_path"] = chromium
            browser = playwright.chromium.launch(**launch_options)
            page = browser.new_page()
            requests = []
            page.on(
                "request",
                lambda request: (
                    requests.append((request.method, request.url, request.post_data))
                    if "/api/plugins/zerofactory/tasks" in request.url
                    else None
                ),
            )
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_role("button", name="+ New Task").click()
            page.get_by_placeholder("e.g. Implement caching layer for Redis").fill(
                "Standalone UI behavior"
            )
            page.get_by_role("button", name="Create Task").click()
            page.get_by_text("Standalone UI behavior").wait_for(timeout=10000)

            create = next((entry for entry in requests if entry[0] == "POST"), None)
            assert create is not None
            assert create[1].endswith("/api/plugins/zerofactory/tasks")
            assert '"title":"Standalone UI behavior"' in (create[2] or "")
            assert default_board in (create[2] or "")
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


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
