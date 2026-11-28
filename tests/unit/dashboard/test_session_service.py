"""Unit tests for dashboard/session_service.py worker-log tailing.

Covers the bounded (64KB) tail read in
``resolve_task_session_progress``: correctness of the last-30-lines tail on
both small and large logs, and that the full file is NOT slurped into memory
for large logs.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from pathlib import Path

import pytest

from dashboard import session_service

TAIL_LIMIT = 64 * 1024


def _make_log(log_path: Path, total_lines: int, line_size: int) -> list[str]:
    """Write a log of ``total_lines`` lines each ~``line_size`` chars; return them."""
    lines = [
        f"line {i:05d} " + "x" * (line_size - len(f"line {i:05d} "))
        for i in range(total_lines)
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect Path.home() (and os.path.expanduser) to a fake home."""
    fake_home = tmp_path / "home"
    (fake_home / ".hermes" / "logs").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    # paths.get_hermes_home() prefers the HERMES_HOME env var over
    # Path.home(); clear it so the fake home is authoritative.
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return fake_home


@pytest.fixture
def no_sessions(monkeypatch: pytest.MonkeyPatch):
    """Short-circuit session discovery so the test only exercises log tailing."""
    monkeypatch.setattr(session_service, "resolve_task_all_sessions", lambda task: [])


def _call_progress(task_id: str) -> dict:
    return session_service.resolve_task_session_progress(
        {"id": task_id, "status": "done"}
    )


def test_log_tail_small_file(hermes_home: Path, no_sessions):
    log_path = hermes_home / ".hermes" / "logs" / "worker_t-small.log"
    lines = _make_log(log_path, total_lines=40, line_size=20)
    tail = _call_progress("t-small")["log_tail"]
    assert tail.endswith("\n".join(lines[-30:]))
    assert len(tail.splitlines()) == 30


def test_log_tail_large_file(hermes_home: Path, no_sessions):
    log_path = hermes_home / ".hermes" / "logs" / "worker_t-big.log"
    lines = _make_log(log_path, total_lines=5000, line_size=60)
    assert log_path.stat().st_size > TAIL_LIMIT
    tail = _call_progress("t-big")["log_tail"]
    assert tail.endswith("\n".join(lines[-30:]))
    assert len(tail.splitlines()) == 30


def test_log_tail_large_file_does_not_read_full_file(
    hermes_home: Path, no_sessions, monkeypatch: pytest.MonkeyPatch
):
    log_path = hermes_home / ".hermes" / "logs" / "worker_t-bounded.log"
    _make_log(log_path, total_lines=5000, line_size=60)
    expected = TAIL_LIMIT  # file is larger than the tail window

    reads: list[int] = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if str(file) == str(log_path):
            original_read = f.read

            def tracked_read(size=-1):
                data = original_read(size)
                reads.append(len(data))
                return data

            f.read = tracked_read
        return f

    monkeypatch.setattr(builtins, "open", spy_open)
    tail = _call_progress("t-bounded")["log_tail"]

    assert reads, "expected the log to be opened and read"
    assert sum(reads) <= expected, (
        f"read {sum(reads)} bytes; expected bounded tail <= {expected}"
    )
    assert len(tail.splitlines()) == 30


def test_log_tail_missing_file(hermes_home: Path, no_sessions):
    assert _call_progress("t-missing")["log_tail"] == ""


@pytest.mark.parametrize(
    ("metadata", "expected_session_id"),
    [
        ({"session_id": "dict-session"}, "dict-session"),
        ('{"session_id":"json-session"}', "json-session"),
        ("{", None),
        ("[]", None),
        ("null", None),
        ('"scalar"', None),
        ("42", None),
        ("true", None),
    ],
)
def test_session_telemetry_coerces_task_metadata(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata,
    expected_session_id: str | None,
):
    monkeypatch.setattr(session_service, "AGENT_ROLES", [])
    task = {"id": "metadata-coercion", "status": "done", "metadata": metadata}

    progress = session_service.resolve_task_session_progress(task, backfill=False)

    assert progress["session_id"] == expected_session_id


@pytest.mark.parametrize("backfill", [False, True])
def test_discovered_session_backfill_respects_flag(
    hermes_home: Path,
    initialized_db: Path,
    default_board: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backfill: bool,
):
    task_id = "zf-session-backfill"
    initial_metadata = {"session_id": "stored-session", "other": "preserved"}
    with sqlite3.connect(initialized_db) as conn:
        conn.execute(
            """INSERT INTO tasks (id, board_slug, title, status, metadata, created_at, updated_at)
               VALUES (?, ?, ?, 'done', ?, 1, 1)""",
            (
                task_id,
                default_board,
                "Backfill session metadata",
                json.dumps(initial_metadata),
            ),
        )

    state_db = tmp_path / "profile-state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY, model TEXT, started_at REAL, ended_at REAL,
                last_activity_at REAL, last_activity_description TEXT,
                message_count INTEGER, tool_call_count INTEGER, cwd TEXT, title TEXT,
                profile_name TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO sessions VALUES (
                'discovered-session', 'test-model', 100, 200, 200, NULL,
                0, 0, ?, 'Backfill session metadata', 'zf-builder'
            )""",
            (f"/workspace/{task_id}",),
        )

    monkeypatch.setattr(session_service, "AGENT_ROLES", ["zf-builder"])
    monkeypatch.setattr(
        session_service,
        "_resolve_profile_state_db_dyn",
        lambda role: state_db if role == "zf-builder" else None,
    )
    task = {
        "id": task_id,
        "title": "Backfill session metadata",
        "status": "done",
        "metadata": initial_metadata,
    }

    progress = session_service.resolve_task_session_progress(task, backfill=backfill)

    assert progress["session_id"] == "discovered-session"
    with sqlite3.connect(initialized_db) as conn:
        stored_metadata = json.loads(
            conn.execute(
                "SELECT metadata FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
        )
    expected_metadata = {**initial_metadata}
    if backfill:
        expected_metadata["session_id"] = "discovered-session"
    assert stored_metadata == expected_metadata


def test_running_telemetry_uses_metadata_start_or_task_timestamp(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(session_service, "AGENT_ROLES", [])
    monkeypatch.setattr(session_service.time, "time", lambda: 1000)
    metadata_task = {
        "id": "metadata-start-time",
        "status": "running",
        "metadata": {"started_at": 975},
        "updated_at": 700,
        "created_at": 600,
    }
    empty_metadata_task = {
        "id": "task-start-time",
        "status": "running",
        "metadata": {},
        "updated_at": 900,
        "created_at": 800,
    }

    metadata_progress = session_service.resolve_task_session_progress(
        metadata_task, backfill=False
    )
    fallback_progress = session_service.resolve_task_session_progress(
        empty_metadata_task, backfill=False
    )

    assert metadata_progress["running_seconds"] == 25
    assert fallback_progress["running_seconds"] == 100
