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

# The line the dispatcher puts at the top of every worker prompt. Fixed prefix, and the
# dispatcher's token is a uuid4 hex — anything else in a transcript is not a token.
_TOKEN_LINE_TEMPLATE = "ZEROFACTORY_WORKER_EXECUTION_ID={token}"
_TOKEN_LINE_RE = re.compile(
    r"^ZEROFACTORY_WORKER_EXECUTION_ID=([0-9a-f]{32})$", re.MULTILINE
)


def execution_token_line(token: str) -> str:
    """The prompt line that carries one dispatch's token."""
    return _TOKEN_LINE_TEMPLATE.format(token=token)


def with_execution_token(prompt: str, token: str | None) -> str:
    """Prepend the token header to a worker prompt; ``prompt`` unchanged without a token."""
    if not token:
        return prompt
    return (
        "Handoff token for this dispatch: pass it as `--execution-id` to every "
        "`hermes zerofactory move` you run.\n"
        f"{execution_token_line(token)}\n\n{prompt}"
    )


def token_from_prompt(text: str) -> str | None:
    """The newest token line in a prompt — a resumed session's latest dispatch wins."""
    found = _TOKEN_LINE_RE.findall(text)
    return found[-1] if found else None


def token_from_session(
    session_id: str | None, state_db: Path | str | None
) -> str | None:
    """Newest token among a session's user messages.

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
        rows = conn.execute(
            "SELECT content FROM messages "
            "WHERE session_id = ? AND role = 'user' ORDER BY rowid DESC",
            (session_id,),
        )
        for (content,) in rows:
            if isinstance(content, str):
                token = token_from_prompt(content)
                if token:
                    return token
    except sqlite3.Error:
        return None
    finally:
        conn.close()
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
        try:
            from paths import resolve_profile_state_db  # type: ignore
        except ImportError:
            return None
        state_db_resolver = resolve_profile_state_db
    try:
        state_db = state_db_resolver(actor)
    except Exception:  # noqa: BLE001 - profile resolution is best-effort here
        return None
    return token_from_session(session_id, state_db)
