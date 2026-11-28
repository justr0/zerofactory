"""Per-dispatch handoff token: prompt header, transcript lookup and CLI resolution."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from dispatcher.worker_handoff import (
    EXECUTION_ID_ENV,
    SESSION_ID_ENV,
    execution_token_line,
    resolve_worker_execution_id,
    token_from_prompt,
    token_from_session,
    with_execution_token,
)

CURRENT = "a" * 32
EARLIER = "b" * 32
OTHER = "c" * 32


def _state_db(path, rows):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?)", rows)
        conn.commit()


def _sessions_db(path):
    """A profile transcript: s1 resumed for a second dispatch, s2 once, s3 never."""
    _state_db(
        path,
        [
            ("s2", "user", with_execution_token("other worker", OTHER)),
            ("s1", "user", with_execution_token("first dispatch", EARLIER)),
            ("s1", "assistant", "I will pass ZEROFACTORY_WORKER_EXECUTION_ID=" + OTHER),
            ("s3", "user", "a dispatch from an older dispatcher: no token"),
            ("s1", "user", with_execution_token("second dispatch", CURRENT)),
            ("s1", "user", "a follow-up message that carries no token"),
        ],
    )
    return path


def test_prompt_carries_the_token_line_above_an_untouched_body():
    prompt = with_execution_token("Task ID: zf-x\nTitle: x", CURRENT)
    assert prompt == (
        "Handoff token for this dispatch: pass it as `--execution-id` to every "
        "`hermes zerofactory move` you run.\n"
        f"{execution_token_line(CURRENT)}\n\n"
        "Task ID: zf-x\nTitle: x"
    )
    assert token_from_prompt(prompt) == CURRENT


@pytest.mark.parametrize("token", [None, ""])
def test_prompt_without_a_token_is_untouched(token):
    assert with_execution_token("Task ID: zf-x", token) == "Task ID: zf-x"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ZEROFACTORY_WORKER_EXECUTION_ID=not-a-token",
        "ZEROFACTORY_WORKER_EXECUTION_ID=" + CURRENT.upper(),
        "quoted ZEROFACTORY_WORKER_EXECUTION_ID=" + CURRENT,
        "ZEROFACTORY_WORKER_EXECUTION_ID= " + CURRENT,
    ],
)
def test_only_the_dispatcher_token_line_counts(text):
    """Anything but the exact line the spawner writes is not a token."""
    assert token_from_prompt(text) is None


def test_newest_token_in_a_session_wins(tmp_path):
    db = _sessions_db(tmp_path / "state.db")
    assert token_from_session("s1", db) == CURRENT
    assert token_from_session("s2", db) == OTHER
    assert token_from_session("s3", db) is None
    assert token_from_session("never-seen", db) is None
    assert token_from_session(None, db) is None


@pytest.mark.parametrize("broken", ["missing", "not-a-database", "no-messages-table"])
def test_unreadable_transcript_is_no_token(tmp_path, broken):
    db = tmp_path / "state.db"
    if broken == "not-a-database":
        db.write_text("not a database", encoding="utf-8")
    elif broken == "no-messages-table":
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE other (x TEXT)")
    assert token_from_session("s1", db) is None


@pytest.mark.parametrize(
    ("explicit", "env", "expected"),
    [
        (CURRENT, {EXECUTION_ID_ENV: EARLIER, SESSION_ID_ENV: "s1"}, CURRENT),
        ("", {EXECUTION_ID_ENV: EARLIER, SESSION_ID_ENV: "s1"}, EARLIER),
        (None, {EXECUTION_ID_ENV: EARLIER, SESSION_ID_ENV: "s1"}, EARLIER),
        (None, {SESSION_ID_ENV: "s1"}, CURRENT),
        (None, {SESSION_ID_ENV: "s1", EXECUTION_ID_ENV: ""}, CURRENT),
        (None, {SESSION_ID_ENV: "s3"}, None),
        (None, {}, None),
    ],
)
def test_resolution_order_flag_then_env_then_session(tmp_path, explicit, env, expected):
    """What a worker reports with, given how the token reaches (or misses) its CLI."""
    db = _sessions_db(tmp_path / "state.db")
    resolved = resolve_worker_execution_id(
        explicit, actor="zf-builder", env=env, state_db_resolver=lambda actor: db
    )
    assert resolved == expected


def test_no_session_or_no_actor_skips_the_transcript(tmp_path):
    def resolver(actor):
        raise AssertionError(f"transcript of {actor} must not be read")

    assert (
        resolve_worker_execution_id(
            actor="zf-builder", env={}, state_db_resolver=resolver
        )
        is None
    )
    assert (
        resolve_worker_execution_id(
            env={SESSION_ID_ENV: "s1"}, state_db_resolver=resolver
        )
        is None
    )


def test_unresolvable_profile_is_no_token():
    def no_such_profile(actor):
        raise RuntimeError(f"no state.db for {actor}")

    assert (
        resolve_worker_execution_id(
            actor="zf-nobody",
            env={SESSION_ID_ENV: "s1"},
            state_db_resolver=no_such_profile,
        )
        is None
    )


def test_default_resolver_reads_the_reporting_profiles_own_transcript(
    tmp_path, monkeypatch
):
    """Without an injected resolver the token comes from the actor's state.db layout."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile = tmp_path / "profiles" / "zf-builder"
    profile.mkdir(parents=True)
    _state_db(
        profile / "state.db", [("s1", "user", with_execution_token("t", CURRENT))]
    )
    assert (
        resolve_worker_execution_id(actor="zf-builder", env={SESSION_ID_ENV: "s1"})
        == CURRENT
    )
    # Another profile's sessions are not this worker's: no token, no guess.
    assert (
        resolve_worker_execution_id(actor="zf-reviewer", env={SESSION_ID_ENV: "s1"})
        is None
    )
