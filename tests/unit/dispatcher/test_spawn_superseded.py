"""Regression: a task re-routed while its worker is spawning.

The dispatcher reads the queue, claims a row (status -> running), then spends
about 12 s provisioning a worktree and spawning the worker. Only ``status`` is
guarded, so an operator's ``move <task> todo --assignee zf-reviewer`` can land
in that window and the worker built for the *old* role is attached to the row
regardless. Its exit report is then judged by the new role: a builder parked as
"Reviewer exited without recording a verdict" with nobody having reviewed.
"""

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from dispatcher import run_dispatch_cycle
from dispatcher import scheduler


def _init_test_db(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                status TEXT DEFAULT 'todo',
                assignee TEXT,
                priority TEXT DEFAULT 'P1',
                metadata TEXT,
                skills TEXT,
                workspace_kind TEXT,
                workspace_path TEXT,
                branch_name TEXT,
                board_slug TEXT,
                tenant TEXT,
                pr_url TEXT,
                worker_pid INTEGER,
                repo_alias TEXT,
                updated_at INTEGER,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id TEXT,
                child_id TEXT,
                link_type TEXT,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                actor TEXT,
                action TEXT,
                details TEXT DEFAULT '',
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                author TEXT,
                body TEXT,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE boards (
                slug TEXT PRIMARY KEY,
                name TEXT,
                description TEXT,
                git_url TEXT,
                target_branch TEXT,
                max_concurrent_running INTEGER DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at INTEGER
            )
            """
        )
        conn.commit()


def _seed_todo(db_path: Path, task_id: str) -> Path:
    workspace = db_path.parent / "wt"
    workspace.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, description, status, assignee, priority, metadata,
                workspace_path, branch_name, updated_at, created_at
            )
            VALUES (?, 'feat: spawn guard', 'Do the thing', 'todo', 'zf-builder',
                    'P2', '{}', ?, 'task/t-spawn', 1, 1)
            """,
            (task_id, str(workspace)),
        )
        conn.commit()
    return workspace


def _operator_edit(db_path: Path, task_id: str, *, status=None, assignee=None) -> None:
    """What the dashboard/CLI writes when an operator re-routes a task."""
    with sqlite3.connect(str(db_path)) as conn:
        if status is not None:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        if assignee is not None:
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id)
            )
        conn.commit()


def _run_cycle(db_path: Path, during_spawn=None, pid=(4242, "sess-1")):
    """One dispatch cycle; worker spawn and every git/process effect stubbed.

    ``during_spawn(db_path)`` runs inside the claim -> spawn window the guard
    covers. Returns the spawn calls and the ``stop_task_worker`` mock.
    """
    import dispatcher

    spawn_calls = []

    def fake_spawn(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        if during_spawn is not None:
            during_spawn(db_path)
        return pid

    with (
        patch.object(
            dispatcher,
            "get_dispatcher_lock_path",
            return_value=db_path.parent / "dispatcher.lock",
        ),
        patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": "1"}),
        patch.object(dispatcher, "resolve_task_repo_path", return_value=db_path.parent),
        patch(
            "dispatcher.scheduler.subprocess.run",
            side_effect=lambda *a, **k: MagicMock(returncode=0, stdout=""),
        ),
        patch.object(dispatcher, "stop_task_worker") as mock_stop,
        patch.object(dispatcher, "_remove_worktree"),
        patch.object(dispatcher, "setup_worktree"),
        patch.object(dispatcher, "spawn_agent_worker", side_effect=fake_spawn),
    ):
        res = run_dispatch_cycle(db_path)
    assert res["ok"] is True
    return spawn_calls, mock_stop


def _row(db_path: Path, task_id: str):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT status, assignee, worker_pid, metadata FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()


def _actions(db_path: Path, task_id: str):
    with sqlite3.connect(str(db_path)) as conn:
        return [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT action, details FROM task_activity WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
        ]


class TestSpawnSuperseded:
    def test_reassigned_during_spawn_is_stopped_and_rerouted(self, tmp_path: Path):
        """A builder spawned for a row that now says zf-reviewer is never attached."""
        db_path = tmp_path / "test.db"
        _init_test_db(db_path)
        task_id = "t-rerouted"
        _seed_todo(db_path, task_id)

        spawn_calls, mock_stop = _run_cycle(
            db_path, lambda db: _operator_edit(db, task_id, assignee="zf-reviewer")
        )

        assert [c[0][4] for c in spawn_calls] == ["zf-builder"]
        assert mock_stop.call_count == 1
        assert mock_stop.call_args.args[0] == task_id
        assert mock_stop.call_args.kwargs["pid"] == 4242

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"], row["worker_pid"]) == (
            "todo",
            "zf-reviewer",
            None,
        )
        meta = json.loads(row["metadata"])
        assert "worker_pid" not in meta
        assert "spawning_at" not in meta
        assert "sessions" not in meta

        actions = _actions(db_path, task_id)
        assert [a for a, _ in actions] == ["spawn_superseded"]
        assert "PID 4242" in actions[0][1]

        # The next cycle starts the role the operator asked for.
        spawn_calls, _ = _run_cycle(db_path, pid=(5353, "sess-2"))
        assert [c[0][4] for c in spawn_calls] == ["zf-reviewer"]
        starts = [d for a, d in _actions(db_path, task_id) if a == "start"]
        assert len(starts) == 1
        assert "Agent zf-reviewer dispatched" in starts[0]
        assert "PID: 5353" in starts[0]

    def test_move_to_todo_during_spawn_is_not_undone(self, tmp_path: Path):
        """A plain `move <task> todo` in the window restarts the same role;
        the superseded worker is stopped and the row stays todo."""
        db_path = tmp_path / "test.db"
        _init_test_db(db_path)
        task_id = "t-restart"
        _seed_todo(db_path, task_id)

        spawn_calls, mock_stop = _run_cycle(
            db_path, lambda db: _operator_edit(db, task_id, status="todo")
        )

        assert [c[0][4] for c in spawn_calls] == ["zf-builder"]
        assert mock_stop.call_count == 1
        assert mock_stop.call_args.kwargs["pid"] == 4242
        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"], row["worker_pid"]) == (
            "todo",
            "zf-builder",
            None,
        )
        assert [a for a, _ in _actions(db_path, task_id)] == ["spawn_superseded"]

    def test_untouched_spawn_is_attached(self, tmp_path: Path):
        """Control: nothing re-routed -> the worker is attached as before."""
        db_path = tmp_path / "test.db"
        _init_test_db(db_path)
        task_id = "t-normal"
        _seed_todo(db_path, task_id)

        spawn_calls, mock_stop = _run_cycle(db_path)

        assert [c[0][4] for c in spawn_calls] == ["zf-builder"]
        mock_stop.assert_not_called()
        actions = _actions(db_path, task_id)
        assert [a for a, _ in actions] == ["start"]
        assert "Agent zf-builder dispatched" in actions[0][1]
        assert "PID: 4242" in actions[0][1]

    def test_unreadable_row_keeps_previous_behaviour(self):
        cursor = MagicMock()
        cursor.execute.side_effect = sqlite3.OperationalError("database is locked")
        assert scheduler._spawned_worker_is_stale(cursor, "t", "zf-builder") is False

    def test_aliases_normalise_like_the_dispatch_loop(self):
        """The row's assignee is compared through the loop's own normaliser."""
        db = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = {"status": "running", "assignee": "zf-builder"}
        db.cursor.return_value = cursor
        assert scheduler._spawned_worker_is_stale(cursor, "t", "zf-builder") is False
        cursor.fetchone.return_value = {"status": "running", "assignee": "zf-reviewer"}
        assert scheduler._spawned_worker_is_stale(cursor, "t", "zf-builder") is True
        cursor.fetchone.return_value = {"status": "blocked", "assignee": "zf-builder"}
        assert scheduler._spawned_worker_is_stale(cursor, "t", "zf-builder") is True
        cursor.fetchone.return_value = None
        assert scheduler._spawned_worker_is_stale(cursor, "t", "zf-builder") is True
