"""Integration tests for activity filters and scanner rows."""

import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest


def _add_scanner_execution(path, board_slug="test-board", status="completed"):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE executions (id TEXT, job_id TEXT, claimed_at TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?)",
            (
                "scanner-execution-1",
                f"zero-factory-improvement-scanner-{board_slug}",
                datetime.now(timezone.utc).isoformat(),
                status,
            ),
        )


def test_scanner_activity_obeys_search_filter(
    api_client: TestClient, monkeypatch, tmp_path
):
    from dashboard.routes import stats

    executions_db = tmp_path / "executions.db"
    _add_scanner_execution(executions_db)
    monkeypatch.setattr(
        stats, "_get_orchestrator_executions_db_path", lambda: str(executions_db)
    )

    response = api_client.get(
        "/api/plugins/zerofactory/activities", params={"search": "no matching activity"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["activities"] == []
    assert data["total"] == 0


def test_scanner_activity_obeys_assignee_filter(
    api_client: TestClient, monkeypatch, tmp_path
):
    from dashboard.routes import stats

    executions_db = tmp_path / "executions.db"
    _add_scanner_execution(executions_db)
    monkeypatch.setattr(
        stats, "_get_orchestrator_executions_db_path", lambda: str(executions_db)
    )

    response = api_client.get(
        "/api/plugins/zerofactory/activities", params={"assignee": "zf-builder"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["activities"] == []
    assert data["total"] == 0


def test_scanner_activity_preserves_embedded_prefix_in_board_slug(
    api_client: TestClient, monkeypatch, tmp_path
):
    from dashboard.routes import stats

    slug = "demo-zero-factory-improvement-scanner-lab"
    executions_db = tmp_path / "executions.db"
    _add_scanner_execution(executions_db, slug)
    monkeypatch.setattr(
        stats, "_get_orchestrator_executions_db_path", lambda: str(executions_db)
    )

    response = api_client.get(
        "/api/plugins/zerofactory/activities",
        params={"board_slug": slug, "action": "scan"},
    )

    assert response.status_code == 200
    activity = response.json()["activities"][0]
    assert activity["board_slug"] == slug
    assert response.json()["total"] == 1


@pytest.mark.parametrize(
    "status", ["claimed", "running", "failed", "unknown", "cancelled", "missed"]
)
def test_scanner_activity_preserves_execution_status(
    api_client: TestClient, monkeypatch, tmp_path, status
):
    from dashboard.routes import stats

    executions_db = tmp_path / "executions.db"
    _add_scanner_execution(executions_db, status=status)
    monkeypatch.setattr(
        stats, "_get_orchestrator_executions_db_path", lambda: str(executions_db)
    )

    response = api_client.get(
        "/api/plugins/zerofactory/activities", params={"action": "scan"}
    )

    assert response.status_code == 200
    activity = response.json()["activities"][0]
    assert activity["task_status"] == status
