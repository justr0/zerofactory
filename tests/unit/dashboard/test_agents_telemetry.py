"""Regression tests for full-history agent telemetry."""

from __future__ import annotations

import sqlite3
import time

import pytest

from dashboard.routes import agents as agents_routes


@pytest.mark.parametrize(
    ("session_count", "null_tool_calls", "active_index", "expected_calls"),
    [
        (0, False, None, 0),
        (3, True, None, 0),
        (105, False, 104, 600),
    ],
)
def test_agent_telemetry_counts_full_session_history_and_keeps_active_session(
    api_client,
    tmp_path,
    monkeypatch,
    session_count,
    null_tool_calls,
    active_index,
    expected_calls,
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
        for index in range(session_count):
            started_at = now - (session_count - index) * 10
            is_active = index == active_index
            conn.execute(
                "INSERT INTO sessions VALUES (?, 'test-model', ?, ?, ?, NULL, 1, ?, '/ws', ?)",
                (
                    f"session-{index}",
                    started_at,
                    None if is_active else started_at + 1,
                    now if is_active else started_at,
                    None if null_tool_calls else (100 if index < 5 else 1),
                    f"Session {index}",
                ),
            )

    monkeypatch.setattr(agents_routes, "AGENT_ROLES", (profile,))
    monkeypatch.setattr(
        agents_routes,
        "_resolve_profile_state_db_dyn",
        lambda role: state_db if role == profile else None,
    )

    response = api_client.get("/api/plugins/zerofactory/agents")
    assert response.status_code == 200
    agent = response.json()["agents"][0]

    assert agent["stats"]["total_sessions"] == session_count
    assert agent["stats"]["total_tool_calls"] == expected_calls
    assert agent["is_active"] is (active_index is not None)
    if active_index is not None:
        assert agent["active_session"]["session_id"] == f"session-{active_index}"
    else:
        assert agent["active_session"] is None
