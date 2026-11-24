"""Behavioral smoke tests for the standalone FastAPI dashboard."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_legacy_task_list_envelope_is_exact(api_client: TestClient, default_board: str):
    response = api_client.get(
        "/api/plugins/zerofactory/tasks", params={"board": default_board}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "tasks", "count"}
    assert body == {"ok": True, "tasks": [], "count": 0}


def test_standalone_page_and_task_api_use_isolated_database(
    initialized_db, monkeypatch
):
    from dashboard.standalone import app

    monkeypatch.setenv("ZEROFACTORY_DB", str(initialized_db))
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert 'id="root"' in page.text
    assert 'src="/assets/app.js"' in page.text

    board = client.post(
        "/api/plugins/zerofactory/boards",
        json={"git_url": "https://example.invalid/local-only.git", "slug": "local"},
    )
    assert board.status_code == 200
    created = client.post(
        "/api/plugins/zerofactory/tasks",
        json={"title": "Standalone smoke", "board_slug": "local"},
    )
    assert created.status_code == 200
    task_id = created.json()["id"]

    listing = client.get("/api/plugins/zerofactory/tasks", params={"board": "local"})
    assert listing.status_code == 200
    body = listing.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["tasks"][0]["id"] == task_id
    assert body["tasks"][0]["title"] == "Standalone smoke"
