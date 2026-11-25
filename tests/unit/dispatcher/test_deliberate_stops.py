"""Unit tests for dispatcher-initiated worker stops not being booked as crashes.

The dispatcher stops workers on purpose when it re-routes their task (new review
round, PR merged, verdict recorded). Those processes exit with a signal status
and the reaper used to spend the task's failure budget on them, retrying the
same role and parking the task after three stops.
"""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dispatcher.config import _active_workers
from dispatcher.process_manager import (
    _pid_is_ancestor,
    _stop_marker_path,
    consume_deliberate_stop,
    forget_deliberate_stop,
    mark_deliberate_stop,
    terminate_process_group,
    terminate_task_worker,
    terminate_worker_process,
)
from dispatcher.reaper import reap_active_workers

TASK_ID = "zf-jz-stop0001"
_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_registries(tmp_path, monkeypatch):
    # Marker files live beside the dispatcher lock: keep them in the test's tmp dir.
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatcher.lock"))
    _active_workers.pop(TASK_ID, None)
    yield
    _active_workers.pop(TASK_ID, None)


def _board_db(
    status: str = "running", assignee: str = "zf-reviewer"
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            assignee TEXT,
            metadata TEXT,
            updated_at INTEGER,
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
            details TEXT,
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
    now = 9000
    meta = {
        "worker_pid": 7171,
        "session_id": "sess-stop",
        "started_at": now - 100,
        "sessions": [
            {
                "session_id": "sess-stop",
                "agent": assignee,
                "status": "ongoing",
                "started_at": now - 100,
            }
        ],
    }
    conn.execute(
        "INSERT INTO tasks VALUES (?, 'Stop Task', ?, ?, ?, ?, ?)",
        (TASK_ID, status, assignee, json.dumps(meta), now - 100, now - 100),
    )
    conn.commit()
    return conn


def _activity(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT action, details FROM task_activity WHERE task_id = ? ORDER BY id",
            (TASK_ID,),
        ).fetchall()
    ]


# --- marker helpers -------------------------------------------------------


def test_mark_and_consume_deliberate_stop_matches_only_the_stopped_pid():
    mark_deliberate_stop(TASK_ID, 4242)
    assert consume_deliberate_stop(TASK_ID, 4242) is True
    # Consumed: a second look must not exempt anything else.
    assert consume_deliberate_stop(TASK_ID, 4242) is False


def test_stale_marker_does_not_mask_a_later_workers_crash():
    mark_deliberate_stop(TASK_ID, 4242)
    assert consume_deliberate_stop(TASK_ID, 5353) is False
    assert consume_deliberate_stop(TASK_ID, 4242) is False  # entry was dropped


@pytest.mark.parametrize(
    "task_id,pid", [(None, 10), (TASK_ID, None), (TASK_ID, 0), (TASK_ID, -3)]
)
def test_mark_deliberate_stop_ignores_unusable_input(task_id, pid):
    mark_deliberate_stop(task_id, pid)
    assert _stop_marker_path(TASK_ID).exists() is False
    assert consume_deliberate_stop(TASK_ID, 10) is False


def test_marker_survives_the_process_that_wrote_it():
    """The dashboard or a CLI child stops the worker; the gateway's reaper reads it."""
    import sys

    marker = _stop_marker_path(TASK_ID)
    code = (
        "from dispatcher.process_manager import mark_deliberate_stop;"
        f"mark_deliberate_stop({TASK_ID!r}, 31337)"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=_REPO_ROOT)
    assert marker.read_text().strip() == "31337"
    assert (
        consume_deliberate_stop(TASK_ID, "31337") is True
    )  # pid may come from metadata
    assert marker.exists() is False


def test_forget_deliberate_stop_removes_only_the_matching_marker():
    mark_deliberate_stop(TASK_ID, 4242)
    forget_deliberate_stop(TASK_ID, 9999)
    assert consume_deliberate_stop(TASK_ID, 4242) is True
    mark_deliberate_stop(TASK_ID, 4242)
    forget_deliberate_stop(TASK_ID, 4242)
    assert consume_deliberate_stop(TASK_ID, 4242) is False


# --- where the marker is set ---------------------------------------------


def test_terminate_task_worker_marks_the_stop_when_it_signals():
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.process_matches_identity", return_value=True),
        patch(
            "dispatcher.process_manager.terminate_process_group", return_value=True
        ) as term,
        patch("dispatcher.process_manager.cleanup_process_artifacts"),
    ):
        terminate_task_worker(TASK_ID, 9876)
    term.assert_called_once_with(None, 9876)
    assert consume_deliberate_stop(TASK_ID, 9876) is True


def test_terminate_worker_process_marks_only_after_a_signal_was_sent():
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.process_matches_identity", return_value=True),
        patch("dispatcher.process_manager.terminate_process_group", return_value=True),
    ):
        terminate_worker_process(None, 9876, task_id=TASK_ID)
    assert consume_deliberate_stop(TASK_ID, 9876) is True

    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch("dispatcher.process_manager.process_matches_identity", return_value=True),
        patch("dispatcher.process_manager.terminate_process_group", return_value=False),
    ):
        terminate_worker_process(None, 9876, task_id=TASK_ID)
    # Nothing was signalled (e.g. own process group, pid already gone): the
    # worker's later exit must still be judged on its own merits.
    assert _stop_marker_path(TASK_ID).exists() is False


def test_terminate_worker_process_skips_mismatched_identity():
    with (
        patch("dispatcher.process_manager.is_pid_alive", return_value=True),
        patch(
            "dispatcher.process_manager.process_matches_identity", return_value=False
        ),
        patch("dispatcher.process_manager.terminate_process_group") as mock_term,
    ):
        terminate_worker_process(None, 9876, task_id=TASK_ID)
    mock_term.assert_not_called()
    assert _stop_marker_path(TASK_ID).exists() is False


def test_terminate_process_group_reports_own_process_group_as_unsignalled():
    # A worker that moves its own task (e.g. `block --reason ...`) must not
    # kill itself, and the refusal must not be booked as a deliberate stop.
    assert terminate_process_group(None, os.getpid()) is False


def test_terminate_process_group_reports_a_real_signal():
    proc = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert terminate_process_group(proc, proc.pid, grace=1.0) is True
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# --- reaper: deliberate stop is not a crash ------------------------------


def test_reaper_books_exit_of_dispatcher_stopped_worker_as_worker_stopped():
    conn = _board_db()
    mock_proc = MagicMock()
    mock_proc.pid = 7171
    mock_proc.poll.return_value = 130  # SIGINT status of a killed worker
    _active_workers[TASK_ID] = mock_proc
    mark_deliberate_stop(TASK_ID, 7171)

    reaped = reap_active_workers(conn.cursor(), now=9000)
    conn.commit()

    assert reaped == 1
    row = conn.execute(
        "SELECT status, assignee, metadata FROM tasks WHERE id = ?", (TASK_ID,)
    ).fetchone()
    # The dispatcher owns the follow-up transition (new review round, PR
    # merged, verdict recorded); the reaper must leave the task alone.
    assert row["status"] == "running"
    assert row["assignee"] == "zf-reviewer"
    saved_meta = json.loads(row["metadata"])
    assert not saved_meta.get("worker_failure_retries")
    assert "blocked_reason" not in saved_meta
    assert saved_meta["last_worker_stop"]["pid"] == 7171
    assert saved_meta["sessions"][0]["status"] == "stopped"

    actions = _activity(conn)
    assert [a for a, _ in actions] == ["worker_stopped"]
    assert "not counted as a failure" in actions[0][1]
    comments = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (TASK_ID,)
    ).fetchall()
    assert comments == []
    conn.close()


def test_reaper_books_vanished_dispatcher_stopped_worker_as_worker_stopped():
    """`stop_task_worker` pops the proc and deletes the exit file before the
    reaper looks, so the stop usually surfaces as "pid not found"."""
    conn = _board_db()
    with patch("dispatcher.is_pid_alive", return_value=False):
        mark_deliberate_stop(TASK_ID, 7171)
        reaped = reap_active_workers(conn.cursor(), now=9000)
        conn.commit()

    assert reaped == 1
    row = conn.execute(
        "SELECT status, metadata FROM tasks WHERE id = ?", (TASK_ID,)
    ).fetchone()
    assert row["status"] == "running"
    saved_meta = json.loads(row["metadata"])
    assert not saved_meta.get("worker_failure_retries")
    assert "blocked_reason" not in saved_meta
    actions = _activity(conn)
    assert [a for a, _ in actions] == ["worker_stopped"]
    conn.close()


def test_reaper_still_books_a_genuine_crash_as_a_failure():
    conn = _board_db()
    mock_proc = MagicMock()
    mock_proc.pid = 7171
    mock_proc.poll.return_value = 130
    _active_workers[TASK_ID] = mock_proc
    mark_deliberate_stop(TASK_ID, 6161)  # stale: an earlier worker of this task

    reaped = reap_active_workers(conn.cursor(), now=9000)
    conn.commit()

    assert reaped == 1
    row = conn.execute(
        "SELECT status, metadata FROM tasks WHERE id = ?", (TASK_ID,)
    ).fetchone()
    assert row["status"] == "todo"
    saved_meta = json.loads(row["metadata"])
    assert saved_meta["worker_failure_retries"] == 1
    assert saved_meta["sessions"][0]["status"] == "failed"
    assert "worker_failed" in [a for a, _ in _activity(conn)]
    conn.close()


# --- a worker's own command must not kill its worker ----------------------


def test_pid_is_ancestor_walks_the_parent_chain():
    import os

    assert _pid_is_ancestor(os.getpid()) is True
    assert _pid_is_ancestor(os.getppid()) is True
    assert _pid_is_ancestor(1) is False  # init: never "ours" to signal
    assert _pid_is_ancestor(999_999_999) is False


def test_terminate_process_group_refuses_to_signal_its_own_worker():
    """``hermes zerofactory block <own task>`` runs inside the worker it transitions."""
    with (
        patch("dispatcher.process_manager._pid_is_ancestor", return_value=True),
        patch("dispatcher.process_manager.os.killpg") as killpg,
    ):
        assert terminate_process_group(None, 4242) is False
    killpg.assert_not_called()


def test_grandparent_worker_is_recognised_from_a_real_subprocess():
    """A tool command sits two levels below the worker (shell -> command)."""
    import os
    import sys
    import textwrap

    code = textwrap.dedent(
        f"""
        import os, subprocess, sys
        outer = {os.getpid()}  # the process standing in for "the worker"
        inner = subprocess.run(
            [sys.executable, "-c",
             "import sys; from dispatcher.process_manager import _pid_is_ancestor;"
             f"sys.exit(0 if _pid_is_ancestor({{outer}}) else 1)"],
            start_new_session=True,
        )
        sys.exit(inner.returncode)
        """
    )
    done = subprocess.run([sys.executable, "-c", code], start_new_session=True)
    assert done.returncode == 0, "grandparent pid was not recognised as an ancestor"
