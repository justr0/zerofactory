"""Route-level tests for specialist agent telemetry."""

from __future__ import annotations


def test_agent_status_preserves_all_running_tasks_per_agent(
    api_client, default_board: str, monkeypatch
):
    from dashboard.db import get_db_conn
    from dashboard.routes import agents as agents_routes

    monkeypatch.setattr(
        agents_routes, "_resolve_profile_state_db_dyn", lambda _profile: None
    )

    with get_db_conn() as conn:
        conn.executemany(
            """
            INSERT INTO tasks (
                id, board_slug, title, status, assignee, workspace_path,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'running', 'zf-builder', ?, 1, ?)
            """,
            [
                ("task-agent-b", default_board, "Second task", "/work/b", 10),
                ("task-agent-newest", default_board, "Newest task", "/work/newest", 11),
                ("task-agent-a", default_board, "First task", "/work/a", 10),
            ],
        )
        conn.commit()

    response = api_client.get(
        "/api/plugins/zerofactory/agents", params={"board_slug": default_board}
    )

    assert response.status_code == 200
    builder = next(
        agent for agent in response.json()["agents"] if agent["name"] == "zf-builder"
    )
    assert [task["id"] for task in builder["current_tasks"]] == [
        "task-agent-newest",
        "task-agent-a",
        "task-agent-b",
    ]
    assert builder["current_task"]["id"] == "task-agent-newest"
    assert builder["is_active"] is True


def test_agent_status_keeps_legacy_null_for_no_running_tasks(
    api_client, default_board: str, monkeypatch
):
    from dashboard.routes import agents as agents_routes

    monkeypatch.setattr(
        agents_routes, "_resolve_profile_state_db_dyn", lambda _profile: None
    )

    response = api_client.get(
        "/api/plugins/zerofactory/agents", params={"board_slug": default_board}
    )

    assert response.status_code == 200
    builder = next(
        agent for agent in response.json()["agents"] if agent["name"] == "zf-builder"
    )
    assert builder["current_tasks"] == []
    assert builder["current_task"] is None
    assert builder["is_active"] is False
