"""Per-dispatch handoff token: prompt header, transcript lookup and CLI resolution."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from dispatcher.worker_handoff import (
    EXECUTION_ID_ENV,
    HEADER_INSTRUCTION,
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
INSTRUCTION = (
    "Handoff token for this dispatch: pass it as `--execution-id` to every "
    "`hermes zerofactory move` you run."
)


def _state_db(path, rows):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?)", rows)
        conn.commit()


def _sessions_db(path):
    """A profile transcript: s1 resumed for a second dispatch, s2 once, s3 and s4 never."""
    _state_db(
        path,
        [
            ("s2", "user", with_execution_token("other worker", OTHER)),
            ("s1", "user", with_execution_token("first dispatch", EARLIER)),
            ("s1", "assistant", "I will pass ZEROFACTORY_WORKER_EXECUTION_ID=" + OTHER),
            ("s3", "user", "a dispatch from an older dispatcher: no token"),
            ("s1", "user", with_execution_token("second dispatch", CURRENT)),
            ("s1", "user", "a follow-up message that carries no token"),
            (
                "s4",
                "user",
                "Review the PR. Its description quotes a dispatch header:\n"
                f"{HEADER_INSTRUCTION}\n{execution_token_line(EARLIER)}\n\nend of quote",
            ),
        ],
    )
    return path


def test_prompt_carries_the_token_line_above_an_untouched_body():
    prompt = with_execution_token("Task ID: zf-x\nTitle: x", CURRENT)
    assert (
        prompt
        == f"{INSTRUCTION}\n{execution_token_line(CURRENT)}\n\nTask ID: zf-x\nTitle: x"
    )
    assert token_from_prompt(prompt) == CURRENT


@pytest.mark.parametrize("token", [None, ""])
def test_prompt_without_a_token_is_untouched(token):
    assert with_execution_token("Task ID: zf-x", token) == "Task ID: zf-x"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ZEROFACTORY_WORKER_EXECUTION_ID=" + CURRENT,
        f"Task ID: zf-x\n{execution_token_line(CURRENT)}",
        "quoted ZEROFACTORY_WORKER_EXECUTION_ID=" + CURRENT,
        "ZEROFACTORY_WORKER_EXECUTION_ID= " + CURRENT,
        f"{INSTRUCTION}\nZEROFACTORY_WORKER_EXECUTION_ID=not-a-token",
        f"{INSTRUCTION}\nZEROFACTORY_WORKER_EXECUTION_ID={CURRENT} trailing",
        f"{INSTRUCTION}\nZEROFACTORY_WORKER_EXECUTION_ID={CURRENT.upper()}",
        f"{INSTRUCTION}\n",  # instruction line, then the prompt without a token line
        f"prefix {INSTRUCTION}\n{execution_token_line(CURRENT)}",
        f"{INSTRUCTION} \n{execution_token_line(CURRENT)}",
        f"Handoff token for this dispatch: run the move with --execution-id.\n"
        f"{execution_token_line(CURRENT)}",
        # The reviewer's finding: a header quoted inside a body is not a dispatch.
        f"Task ID: zf-x\nDescription:\n{INSTRUCTION}\n{execution_token_line(CURRENT)}\n",
        "Review of PR 152:\n```\n"
        f"{INSTRUCTION}\n{execution_token_line(CURRENT)}\n```\n",
    ],
)
def test_only_a_complete_header_at_the_top_counts(text):
    """Only the two-line header the spawner writes, first, is a dispatch."""
    assert token_from_prompt(text) is None


def test_header_tolerates_crlf_and_nothing_after_it():
    crlf = f"{INSTRUCTION}\r\n{execution_token_line(CURRENT)}\r\n\r\nTask ID: zf-x"
    assert token_from_prompt(crlf) == CURRENT
    assert (
        token_from_prompt(f"{INSTRUCTION}\n{execution_token_line(CURRENT)}") == CURRENT
    )


def test_newest_token_in_a_session_wins(tmp_path):
    db = _sessions_db(tmp_path / "state.db")
    assert token_from_session("s1", db) == CURRENT
    assert token_from_session("s2", db) == OTHER
    assert token_from_session("s3", db) is None
    assert token_from_session("s4", db) is None
    assert token_from_session("never-seen", db) is None
    assert token_from_session(None, db) is None


def test_header_below_many_steering_messages_is_still_found(tmp_path):
    """The scan bound counts dispatch prompts, not the messages between them."""
    db = tmp_path / "state.db"
    noise = [("s1", "user", f"steering note {n}") for n in range(80)]
    _state_db(db, [("s1", "user", with_execution_token("t", CURRENT)), *noise])
    assert token_from_session("s1", db) == CURRENT


def test_malformed_header_candidates_cannot_hide_the_dispatch(tmp_path):
    """A message that starts like a header but is not one still matches the SQL
    prefix; the scan pages past any number of them instead of stopping at a bound."""
    db = tmp_path / "state.db"
    malformed = [
        (
            "s1",
            "user",
            f"{INSTRUCTION}\nZEROFACTORY_WORKER_EXECUTION_ID=not-a-token {n}",
        )
        for n in range(120)
    ]
    _state_db(db, [("s1", "user", with_execution_token("t", CURRENT)), *malformed])
    assert token_from_session("s1", db) == CURRENT


def test_sentinel_actors_resolve_nothing_even_with_a_profile_layout(
    tmp_path, monkeypatch
):
    """``human``, ``unassigned`` and unknown actors are not profiles, however the
    directory layout looks: a ``profiles/<sentinel>/state.db`` holding the session's
    token is never opened, while a real profile in the same layout still resolves."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token_row = [("s1", "user", with_execution_token("t", CURRENT))]
    for name in ("unassigned", "human", "zf-builder"):
        profile = tmp_path / "profiles" / name
        profile.mkdir(parents=True)
        _state_db(profile / "state.db", token_row)
    env = {SESSION_ID_ENV: "s1"}
    for actor in ("somebody", "unassigned", "human"):
        assert resolve_worker_execution_id(actor=actor, env=env) is None, actor
    assert resolve_worker_execution_id(actor="zf-builder", env=env) == CURRENT


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
        (None, {SESSION_ID_ENV: "s4"}, None),
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


@pytest.mark.parametrize(
    "actor", ["zf-reviewer", "somebody", "unassigned", "human", ""]
)
def test_default_resolver_never_falls_back_to_the_global_database(
    tmp_path, monkeypatch, actor
):
    """An actor without a profile of its own has no token, even when the global
    ``state.db`` holds a session that carries one: the human operator's transcript
    is never read as a worker's."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _state_db(
        tmp_path / "state.db", [("s1", "user", with_execution_token("t", CURRENT))]
    )
    assert resolve_worker_execution_id(actor=actor, env={SESSION_ID_ENV: "s1"}) is None
