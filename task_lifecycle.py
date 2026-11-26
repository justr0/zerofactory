"""Shared task lifecycle writes for the blocked-reason column."""

from __future__ import annotations

import sqlite3


def record_task_block(
    conn: sqlite3.Connection,
    task_id: str,
    actor: str,
    reason: str | None,
    created_at: int,
) -> None:
    """Replace a task's current block reason and record the block event."""
    conn.execute("UPDATE tasks SET blocked_reason = ? WHERE id = ?", (reason, task_id))
    conn.execute(
        "INSERT INTO task_activity (task_id, actor, action, details, created_at) "
        "VALUES (?, ?, 'blocked', ?, ?)",
        (task_id, actor, reason or "No reason provided", created_at),
    )


def clear_task_block(conn: sqlite3.Connection, task_id: str) -> None:
    """Clear the active block reason; history remains in task_activity."""
    conn.execute("UPDATE tasks SET blocked_reason = NULL WHERE id = ?", (task_id,))
