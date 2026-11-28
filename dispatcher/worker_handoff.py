"""Per-dispatch handoff token: how a worker proves it is the dispatched execution.

The dispatcher tracks one ``worker_execution_id`` per dispatch and the move route
accepts a builder's ``done`` / ``running`` report only from the worker holding it.
The variable the spawner injects into the worker process
(``ZEROFACTORY_WORKER_EXECUTION_ID``) does not reach that worker's shell commands: a
Hermes CLI session's terminal tool is executed by the gateway process, so a command's
environment is the gateway's, never the spawned worker's. Every builder handoff was
rejected while the token travelled by env alone.

The dispatch prompt is the channel only the dispatched worker sees: a zombie from an
earlier dispatch holds an earlier prompt, so an earlier token. Worker commands do carry
their own session's ``HERMES_SESSION_ID``, which lets the CLI recover the token from
that session's transcript without any cooperation from the worker. Precedence when a
worker reports:

``--execution-id`` > ``ZEROFACTORY_WORKER_EXECUTION_ID`` > this session's newest token.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, Mapping

EXECUTION_ID_ENV = "ZEROFACTORY_WORKER_EXECUTION_ID"
SESSION_ID_ENV = "HERMES_SESSION_ID"

#: The instruction line of the two-line header every dispatched prompt starts with.
#: Wire format shared with the transcript parser below: the line is matched verbatim,
#: so reword it only together with a parser that still accepts the previous one.
HEADER_INSTRUCTION = (
    "Handoff token for this dispatch: pass it as `--execution-id` to every "
    "`hermes zerofactory move` you run."
)
_TOKEN_LINE_PREFIX = "ZEROFACTORY_WORKER_EXECUTION_ID="
# The dispatcher's token is a uuid4 hex — anything else in a transcript is not a token.
_TOKEN_LINE_RE = re.compile(r"ZEROFACTORY_WORKER_EXECUTION_ID=([0-9a-f]{32})")

# User messages scanned per session, newest first. A dispatch prompt is a session's
# first user message and a re-dispatch is its newest; only mid-run steering lands
# between, so the bound is never reached by a real session — it only caps the read.
_SESSION_SCAN_LIMIT = 50


def execution_token_line(token: str) -> str:
    """The prompt line that carries one dispatch's token."""
    return f"{_TOKEN_LINE_PREFIX}{token}"


def with_execution_token(prompt: str, token: str | None) -> str:
    """Prepend the token header to a worker prompt; ``prompt`` unchanged without a token."""
    if not token:
        return prompt
    return f"{HEADER_INSTRUCTION}\n{execution_token_line(token)}\n\n{prompt}"


def token_from_prompt(text: str | None) -> str | None:
    """The token of the dispatch header at the very top of ``text``; ``None`` without one.

    Only a complete header in its prescribed position counts. A token line quoted
    anywhere else — inside a task body, a review comment, a tool's output pasted into a
    prompt — is not a dispatch and must not authenticate one.
    """
    if not isinstance(text, str):
        return None
    instruction, newline, rest = text.partition("\n")
    if not newline or instruction.rstrip("\r") != HEADER_INSTRUCTION:
        return None
    token_line = rest.partition("\n")[0].rstrip("\r")
    match = _TOKEN_LINE_RE.fullmatch(token_line)
    return match.group(1) if match else None


def token_from_session(
    session_id: str | None, state_db: Path | str | None
) -> str | None:
    """Newest token among a session's dispatch prompts.

    ``None`` without a session or database, and on any read error: an unreadable
    transcript must degrade to "no token", never to a crash or a guessed one.
    """
    if not session_id or not state_db:
        return None
    try:
        # A profile transcript is only ever read here. URI form keeps a path that
        # contains '?' or '#' intact; WAL readers need the writer's -shm, present
        # while the worker's own session has the database open.
        uri = f"{Path(state_db).expanduser().resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, timeout=2, uri=True)
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return None
    try:
        # The prefix narrows the scan in SQL; the strict parser decides, so a header
        # quoted at the start of a longer message still does not count.
        rows = conn.execute(
            "SELECT content FROM messages "
            "WHERE session_id = ? AND role = 'user' AND content LIKE ? "
            f"ORDER BY rowid DESC LIMIT {_SESSION_SCAN_LIMIT}",
            (session_id, f"{HEADER_INSTRUCTION}%"),
        )
        for (content,) in rows:
            token = token_from_prompt(content)
            if token:
                return token
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None


def _own_profile_state_db(actor: str) -> Path | None:
    """``state.db`` of the reporting profile, or ``None`` when the profile has none.

    Only the actor's own profile database qualifies. The general profile resolver
    ends in the global ``~/.hermes/state.db`` — a file holding the human operator's
    sessions, which an unknown or unassigned actor must never be read as.
    """
    try:
        from paths import (  # type: ignore
            _plugin_profiles_root,
            get_hermes_profiles_root,
            normalize_assignee,
        )
    except ImportError:
        return None
    try:
        profile = normalize_assignee(actor)
        candidate = get_hermes_profiles_root() / profile / "state.db"
        if candidate.is_file():
            return candidate
        plugin_root = _plugin_profiles_root()
        if plugin_root is not None:
            candidate = plugin_root / profile / "state.db"
            if candidate.is_file():
                return candidate
    except Exception:  # noqa: BLE001 - profile resolution is best-effort here
        pass
    return None


def resolve_worker_execution_id(
    explicit: str | None = None,
    *,
    actor: str | None = None,
    env: Mapping[str, str] | None = None,
    state_db_resolver: Callable[[str], Path | str | None] | None = None,
) -> str | None:
    """The execution ID a worker reports with: flag, then env, then its session's token.

    ``actor`` is the reporting profile: its own ``state.db`` holds its sessions. The
    session lookup is skipped when the env already carries the ID (a worker spawned by
    a runner that does pass its env through) and when there is no session to look up.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    environ: Mapping[str, str] = os.environ if env is None else env
    from_env = environ.get(EXECUTION_ID_ENV)
    if from_env:
        return from_env
    session_id = environ.get(SESSION_ID_ENV)
    if not session_id or not actor:
        return None
    if state_db_resolver is None:
        state_db_resolver = _own_profile_state_db
    try:
        state_db = state_db_resolver(actor)
    except Exception:  # noqa: BLE001 - profile resolution is best-effort here
        return None
    return token_from_session(session_id, state_db)
