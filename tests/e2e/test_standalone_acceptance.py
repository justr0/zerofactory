"""Offline acceptance checks for the standalone dashboard HTTP contract."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
API = "/api/plugins/zerofactory"


def _request(url: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def standalone_server(tmp_path):
    """Run the actual standalone ASGI app on an ephemeral loopback port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(
        {
            "ZEROFACTORY_DB": str(tmp_path / "standalone.db"),
            "ZEROFACTORY_LOCK_PATH": str(tmp_path / "dispatcher.lock"),
            "ZEROFACTORY_DISABLE_DISPATCHER": "1",
        }
    )
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
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("standalone dashboard process exited before becoming ready")
        try:
            with urllib.request.urlopen(base_url + "/", timeout=0.2) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    else:
        process.terminate()
        pytest.fail("standalone dashboard did not start")
    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_actual_standalone_serves_dashboard_and_legacy_task_list_shape(
    standalone_server,
):
    with urllib.request.urlopen(standalone_server + "/", timeout=2) as response:
        page = response.read().decode()
    assert "Standalone Kanban control surface" in page
    assert 'id="create"' in page

    status, initial = _request(standalone_server + API + "/tasks")
    assert status == 200
    assert initial["ok"] is True
    assert initial["count"] == 0
    assert isinstance(initial["tasks"], list)

    status, board = _request(
        standalone_server + API + "/boards",
        "POST",
        {"git_url": "https://example.com/org/standalone.git"},
    )
    assert status == 200

    status, created = _request(
        standalone_server + API + "/tasks",
        "POST",
        {
            "title": "offline standalone acceptance",
            "priority": "P2",
            "board_slug": board["slug"],
        },
    )
    assert status == 200
    task_id = created["id"]

    status, listed = _request(standalone_server + API + "/tasks")
    assert status == 200
    assert listed["tasks"][0]["id"] == task_id
    assert listed["tasks"][0]["title"] == "offline standalone acceptance"
