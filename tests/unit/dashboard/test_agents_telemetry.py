"""Regression tests for full-history agent telemetry."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from dashboard.routes import agents as agents_routes


def test_agent_telemetry_counts_full_session_history_and_keeps_active_session(
    tmp_path, monkeypatch
):
    profile = "zf-builder"
    state_db = tmp_path / "builder.db"
    now = time.time()
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY, model TEXT, started_at REAL, ended_at REAL,
                last_activity_at REAL, last_activity_description TEXT,
                message_count INTEGER, tool_call_count INTEGER, cwd TEXT, title TEXT
            )"""
        )
        for index in range(105):
            started_at = now - (105 - index) * 10
            is_active = index == 104
            conn.execute(
                "INSERT INTO sessions VALUES (?, 'test-model', ?, ?, ?, NULL, 1, ?, '/ws', ?)",
                (
                    f"session-{index}",
                    started_at,
                    None if is_active else started_at + 1,
                    now if is_active else started_at,
                    100 if index < 5 else 1,
                    f"Session {index}",
                ),
            )

    monkeypatch.setattr(agents_routes, "AGENT_ROLES", (profile,))
    monkeypatch.setattr(agents_routes, "init_db", lambda: None)
    monkeypatch.setattr(
        agents_routes,
        "_resolve_profile_state_db_dyn",
        lambda role: state_db if role == profile else None,
    )

    @contextmanager
    def empty_kanban_db():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE tasks (id, title, board_slug, assignee, workspace_path, updated_at, status)"
        )
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(agents_routes, "get_db_conn", empty_kanban_db)

    agent = agents_routes.get_agents_status()["agents"][0]

    assert agent["stats"]["total_sessions"] == 105
    assert agent["stats"]["total_tool_calls"] == 600
    assert agent["is_active"] is True
    assert agent["active_session"]["session_id"] == "session-104"
