from fastapi.testclient import TestClient

from dashboard.standalone import app


def test_standalone_serves_local_dashboard_assets():
    client = TestClient(app)

    page = client.get("/")
    script = client.get("/assets/app.js")
    styles = client.get("/assets/style.css")

    assert page.status_code == 200
    assert 'id="root"' in page.text
    assert "/assets/app.js" in page.text
    assert script.status_code == 200
    assert "createRoot" in script.text
    assert "/api/plugins/zerofactory" in script.text
    assert "https://" not in page.text
    assert "cdn." not in page.text.lower()
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")

    # The standalone bundle ships the same management surfaces as the plugin UI.
    for route in (
        "/tasks",
        "/boards",
        "/boards/test-clone",
        "/dispatch/run",
        "/settings/langfuse/test",
        "/settings/profiles/sync",
        "/cron",
        "/memories/",
        "/sessions/",
        "/approve",
        "/reject",
        "/recover",
        "/split",
        "/report-bug",
        "/comments",
    ):
        assert route in script.text


def test_standalone_health_controls_use_existing_api_routes(initialized_db):
    client = TestClient(app)
    page = client.get("/").text

    assert "dispatch/status" in page
    assert "health/stuck-tasks" in page
    assert "health/reap-stuck" in page
    assert client.get("/api/plugins/zerofactory/tasks").status_code == 200


def test_standalone_app_serves_and_uses_isolated_database(initialized_db):
    client = TestClient(app)

    assert client.get("/").status_code == 200
    created = client.post(
        "/api/plugins/zerofactory/boards",
        json={
            "slug": "standalone-smoke",
            "repositories": [
                {
                    "repo_alias": "main",
                    "git_url": "https://example.invalid/repo.git",
                }
            ],
        },
    )
    assert created.status_code == 200
    task = client.post(
        "/api/plugins/zerofactory/tasks",
        json={"title": "Standalone smoke task", "board_slug": "standalone-smoke"},
    )
    assert task.status_code == 200

    tasks = client.get("/api/plugins/zerofactory/tasks?board=standalone-smoke")
    assert tasks.status_code == 200
    payload = tasks.json()
    assert set(payload) == {"ok", "tasks", "count"}
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["id"] == task.json()["id"]
    assert payload["tasks"][0]["title"] == "Standalone smoke task"
