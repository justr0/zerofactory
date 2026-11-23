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
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")


def test_standalone_health_controls_use_existing_api_routes():
    client = TestClient(app)
    page = client.get("/").text

    assert "dispatch/status" in page
    assert "health/stuck-tasks" in page
    assert "health/reap-stuck" in page
    assert client.get("/api/plugins/zerofactory/tasks").status_code == 200
