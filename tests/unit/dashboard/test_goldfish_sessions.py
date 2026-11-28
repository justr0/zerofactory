"""Regression coverage for Goldfish session discovery and cleanup."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dashboard import session_service
from dashboard.routes import agents as agents_routes
from dashboard.routes import tasks as tasks_routes


def _make_state_db(path: Path, session_id: str, *, title: str, cwd: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY, model TEXT, started_at REAL, ended_at REAL,
                last_activity_at REAL, last_activity_description TEXT,
                message_count INTEGER, tool_call_count INTEGER, cwd TEXT, title TEXT,
                profile_name TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, 'test-model', 100, NULL, 100, NULL, 0, 0, ?, ?, 'zf-goldfish')",
            (session_id, cwd, title),
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_name TEXT, tool_calls TEXT, content TEXT, reasoning_content TEXT, timestamp REAL)"
        )


def test_goldfish_session_discovered_for_task_and_all_sessions(tmp_path, monkeypatch):
    state_db = tmp_path / "goldfish.db"
    _make_state_db(
        state_db, "gold-session", title="Task ID: zf-gold-task", cwd="/ws/zf-gold-task"
    )
    monkeypatch.setattr(
        session_service,
        "_resolve_profile_state_db_dyn",
        lambda role: state_db if role == "zf-goldfish" else None,
    )

    task_sessions = session_service.resolve_task_all_sessions(
        {"id": "zf-gold-task", "title": "Goldfish task", "status": "done"}
    )
    assert [session["session_id"] for session in task_sessions] == ["gold-session"]

    all_sessions = session_service.list_all_sessions(role="zf-goldfish")
    assert [session["session_id"] for session in all_sessions["sessions"]] == [
        "gold-session"
    ]
    assert all_sessions["sessions"][0]["agent"] == "zf-goldfish"


def test_goldfish_standalone_session_stop_updates_state_db(tmp_path, monkeypatch):
    state_db = tmp_path / "goldfish.db"
    _make_state_db(state_db, "gold-session", title="Standalone", cwd="/tmp")
    monkeypatch.setattr(agents_routes, "init_db", lambda: None)
    monkeypatch.setattr(
        agents_routes,
        "_resolve_profile_state_db_dyn",
        lambda role: state_db if role == "zf-goldfish" else None,
    )

    @contextmanager
    def empty_kanban_db():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id TEXT, metadata TEXT)")
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(agents_routes, "get_db_conn", empty_kanban_db)
    result = agents_routes.stop_session("gold-session")

    assert result["ok"] is True
    with sqlite3.connect(state_db) as conn:
        assert conn.execute(
            "SELECT ended_at FROM sessions WHERE id = 'gold-session'"
        ).fetchone()[0]


def test_goldfish_task_session_cleanup_updates_state_db(tmp_path, monkeypatch):
    state_db = tmp_path / "goldfish.db"
    _make_state_db(state_db, "gold-session", title="Task", cwd="/tmp")
    monkeypatch.setenv("ZEROFACTORY_SKIP_DISPATCHER", "1")
    monkeypatch.setattr(tasks_routes, "init_db", lambda: None)
    monkeypatch.setattr(
        tasks_routes,
        "resolve_profile_state_db",
        lambda role: state_db if role == "zf-goldfish" else None,
    )
    monkeypatch.setattr(tasks_routes, "transition_task", None)

    @contextmanager
    def kanban_db():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, assignee TEXT, metadata TEXT, updated_at INTEGER, blocked_reason TEXT)"
        )
        conn.execute(
            "INSERT INTO tasks (id, status, assignee, metadata, updated_at) VALUES ('task-1', 'running', 'zf-goldfish', ?, 1)",
            (json.dumps({"session_id": "gold-session"}),),
        )
        conn.execute(
            "CREATE TABLE task_activity (task_id TEXT, actor TEXT, action TEXT, details TEXT, created_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT, created_at INTEGER)"
        )
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(tasks_routes, "get_db_conn", kanban_db)
    result = tasks_routes.stop_task_session("task-1")

    assert result["ok"] is True
    with sqlite3.connect(state_db) as conn:
        assert conn.execute(
            "SELECT ended_at FROM sessions WHERE id = 'gold-session'"
        ).fetchone()[0]
