"""Security boundaries for the standalone dashboard."""

from __future__ import annotations

import pytest


def test_standalone_rejects_non_loopback_host(monkeypatch):
    from dashboard import standalone

    monkeypatch.setattr("sys.argv", ["zerofactory-standalone", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit) as exc_info:
        standalone.main()
    assert exc_info.value.code == 2


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_standalone_accepts_loopback_ip(host):
    from dashboard.standalone import _loopback_host

    assert _loopback_host(host) == host


def test_settings_response_omits_secret_values(api_client):
    from dashboard.db import get_db_conn

    with get_db_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, 1)",
            [
                ("langfuse_secret_key", "secret-langfuse-value"),
                ("mem0_api_key", "secret-mem0-value"),
            ],
        )
        conn.commit()

    response = api_client.get("/api/plugins/zerofactory/settings")

    assert response.status_code == 200
    settings = response.json()["settings"]
    assert "langfuse_secret_key" not in settings
    assert "mem0_api_key" not in settings
    assert "secret-langfuse-value" not in response.text
    assert "secret-mem0-value" not in response.text


def test_settings_save_preserves_blank_secrets(api_client):
    from dashboard.db import get_db_conn

    with get_db_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, 1)",
            [
                ("langfuse_secret_key", "saved-langfuse-secret"),
                ("mem0_api_key", "saved-mem0-secret"),
            ],
        )
        conn.commit()

    response = api_client.patch(
        "/api/plugins/zerofactory/settings",
        json={
            "max_active_tasks": 6,
            "langfuse_secret_key": "",
            "mem0_api_key": "",
        },
    )

    assert response.status_code == 200
    assert "saved-langfuse-secret" not in response.text
    assert "saved-mem0-secret" not in response.text
    with get_db_conn() as conn:
        stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert stored["langfuse_secret_key"] == "saved-langfuse-secret"
    assert stored["mem0_api_key"] == "saved-mem0-secret"


def test_new_langfuse_secret_is_synced_but_not_returned(api_client, monkeypatch):
    from dashboard.routes import settings as settings_route

    synced = {}
    monkeypatch.setattr(
        settings_route,
        "sync_langfuse_profiles",
        lambda settings: synced.update(settings),
    )

    response = api_client.patch(
        "/api/plugins/zerofactory/settings",
        json={"langfuse_secret_key": "new-langfuse-secret"},
    )

    assert response.status_code == 200
    assert synced["langfuse_secret_key"] == "new-langfuse-secret"
    assert "langfuse_secret_key" not in response.json()["settings"]
    assert "new-langfuse-secret" not in response.text


@pytest.mark.parametrize(
    "origin",
    [
        "https://attacker.example",
        "null",
        "http://127.0.0.1/path",
        "http://127.0.0.1:invalid",
    ],
)
def test_standalone_blocks_cross_origin_reaper_post(
    initialized_db, monkeypatch, origin
):
    from fastapi.testclient import TestClient

    import dispatcher
    from dashboard.standalone import app

    reaped = []
    monkeypatch.setattr(
        dispatcher, "reap_stuck_tasks", lambda **kwargs: reaped.append(kwargs)
    )
    client = TestClient(app, base_url="http://127.0.0.1")

    response = client.post(
        "/api/plugins/zerofactory/health/reap-stuck",
        headers={"Origin": origin},
    )

    assert response.status_code == 403
    assert reaped == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/health/reap-stuck"),
        ("PUT", "/cron/missing"),
        ("PATCH", "/settings"),
        ("DELETE", "/memories/missing"),
    ],
)
def test_standalone_blocks_cross_origin_mutation_methods(initialized_db, method, path):
    from fastapi.testclient import TestClient

    from dashboard.standalone import app

    response = TestClient(app).request(
        method,
        f"/api/plugins/zerofactory{path}",
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "base_url", ["http://127.0.0.1", "http://[::1]", "http://localhost"]
)
def test_standalone_allows_same_origin_dashboard_mutations(
    initialized_db, default_board, base_url
):
    from fastapi.testclient import TestClient

    from dashboard.standalone import app

    client = TestClient(app, base_url=base_url)
    response = client.post(
        "/api/plugins/zerofactory/tasks",
        headers={"Origin": base_url},
        json={
            "title": "Same-origin dashboard action",
            "description": "Origin validation preserves dashboard actions",
            "priority": "P2",
            "assignee": "zf-builder",
            "status": "todo",
            "board_slug": default_board,
        },
    )

    assert response.status_code == 200, response.text
    task_id = response.json()["id"]
    task = client.get(f"/api/plugins/zerofactory/tasks/{task_id}")
    assert task.json()["task"]["title"] == "Same-origin dashboard action"


def test_standalone_allows_headerless_api_mutations(initialized_db, default_board):
    from fastapi.testclient import TestClient

    from dashboard.standalone import app

    response = TestClient(app).post(
        "/api/plugins/zerofactory/tasks",
        json={
            "title": "Headerless API action",
            "description": "Non-browser clients remain supported",
            "priority": "P2",
            "assignee": "zf-builder",
            "status": "todo",
            "board_slug": default_board,
        },
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://attacker.example"},
        {"Sec-Fetch-Site": "same-origin"},
    ],
)
def test_standalone_rejects_nonlocal_host_even_when_browser_metadata_matches(
    initialized_db, monkeypatch, headers
):
    from fastapi.testclient import TestClient

    import dispatcher
    from dashboard.standalone import app

    reaped = []
    monkeypatch.setattr(
        dispatcher, "reap_stuck_tasks", lambda **kwargs: reaped.append(kwargs)
    )
    client = TestClient(app, base_url="http://attacker.example")
    response = client.post(
        "/api/plugins/zerofactory/health/reap-stuck",
        headers=headers,
    )

    assert response.status_code == 403
    assert reaped == []


def test_standalone_sync_issues_is_post_only(
    initialized_db, default_board, monkeypatch
):
    from fastapi.testclient import TestClient

    import issues.github
    import issues.importer
    from dashboard.standalone import app

    calls = []

    class StubGitHubClient:
        def __init__(self, default_repo=None):
            pass

        def fetch_investigation_issues(self, **kwargs):
            calls.append(kwargs)
            return [object()]

    monkeypatch.setattr(issues.github, "GitHubIssueClient", StubGitHubClient)
    monkeypatch.setattr(
        issues.importer,
        "import_external_issue",
        lambda **kwargs: {"duplicate": False, "issue_id": "stub"},
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    path = f"/api/plugins/zerofactory/boards/{default_board}/sync-gh-issues"

    legacy_get = client.get(path, headers={"Origin": "https://attacker.example"})
    assert legacy_get.status_code == 405
    assert calls == []

    same_origin_post = client.post(path, headers={"Origin": "http://127.0.0.1"})
    assert same_origin_post.status_code == 200, same_origin_post.text
    assert same_origin_post.json()["imported_count"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize("fetch_site", ["cross-site", "same-site"])
def test_standalone_blocks_non_same_origin_fetch_without_origin(
    initialized_db, fetch_site
):
    from fastapi.testclient import TestClient

    from dashboard.standalone import app

    response = TestClient(app).post(
        "/api/plugins/zerofactory/health/reap-stuck",
        headers={"Sec-Fetch-Site": fetch_site},
    )

    assert response.status_code == 403
