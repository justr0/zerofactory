"""Review-cap arbitration (Option A): at the review cap the dispatcher hands the
decision to an arbiter profile instead of parking the task for a human.

Unit level: the request/verdict wire format and its strict first-line parser
(bound to the request key, the head, an authorized GitHub author and the
request window), request posting and adoption, the per-task budget, staleness,
opt-outs and the FIX-round brief.

Cycle level (``run_dispatch_cycle`` against a real SQLite board; only the
GitHub boundary is stubbed): the request is posted exactly once at the cap, a
failed post leaves no live request and a lost local state adopts the existing
comment; only a verdict bound to the active request and head routes the task
(FIX re-opens a bounded round and re-review, MERGE waits for the arbiter's
merge, ESCALATE and timeouts leave it parked for the human); a parked task is
never rerouted by conflicts or ordinary comments; disabling arbitration,
opting out or a late reply voids or expires the request.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dispatcher import review_cap_arbitration as arb
from dispatcher.github_pr import (
    fetch_pr_conversation_comments,
    fetch_pr_review_comments,
)
from dispatcher.scheduler import run_dispatch_cycle

PR = "https://github.com/acme/repo/pull/9"
HEAD = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12"
HEAD_2 = "ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00"
HEAD_3 = "0123456789abcdef0123456789abcdef01234567"
ARBITER = "zf-gatekeeper"
ALLOWED = "justr0"
ALLOWED_ENV = {"ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS": ALLOWED}

# Fixed clock for the parser tests: request opened 10 minutes before "now".
_NOW = 1_800_000_000
_STARTED = _NOW - 600


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(task_id: str, head: str = HEAD) -> str:
    return arb.request_key(task_id, head)


def _verdict_body(
    verdict: str,
    task_id: str,
    *,
    head: str = HEAD,
    key: str | None = None,
    role: str = ARBITER,
    detail: str = "1. race in refresh() - repro: run two cycles",
) -> str:
    line = arb.verdict_line(role, verdict, key or _key(task_id, head), head)
    return f"{line}\n\n{detail}"


def _convo(
    cid: int, body: str, *, author: str = ALLOWED, created_at: str | None = None
) -> dict:
    """A raw GitHub issue-comment item, as ``gh api .../issues/N/comments`` returns it.

    Defaults to the parser tests' fixed clock: ten minutes before ``_NOW``.
    """
    return {
        "id": cid,
        "body": body,
        "user": {"login": author},
        "created_at": created_at if created_at is not None else _iso(_NOW - 60),
        "author_association": "OWNER",
    }


def _std(
    cid: int,
    body: str,
    *,
    author: str = ALLOWED,
    type_: str = "pr_comment",
    created_at: str | None = None,
) -> dict:
    """The standardized shape ``fetch_pr_review_comments`` hands the scheduler."""
    return {
        "comment_id": f"issue_{cid}",
        "type": type_,
        "author": author,
        "state": "COMMENTED",
        "body": body,
        "path": None,
        "line": None,
        "diff_hunk": None,
        "suggestion": None,
        "created_at": created_at
        if created_at is not None
        else _iso(int(time.time()) - 60),
    }


def _request_body(key: str, head: str = HEAD) -> str:
    return (
        f"{arb.REQUEST_PREFIX}\nREQUEST: {key}\nHEAD: {head}\n\n"
        "Arbitration attempt 1/2.\n\n@zf-gatekeeper: decide this PR."
    )


def _init_db(db_path: Path):
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


def _seed(db_path: Path, task_id: str, meta: dict, *, assignee="human", workspace=None):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, pr_url, metadata, workspace_path, branch_name) "
            "VALUES (?, ?, 'blocked', ?, ?, ?, ?, ?)",
            (
                task_id,
                "feat: arbitrated",
                assignee,
                PR,
                json.dumps(meta),
                workspace,
                f"task/{task_id}",
            ),
        )
        conn.commit()


def _managed_workspace(repo_path: Path, task_id: str) -> Path:
    """Workspace under the managed ``<repo>-worktrees/<task>`` boundary the
    dispatcher now validates before touching a task's worktree."""
    workspace = repo_path.parent / f"{repo_path.name}-worktrees" / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _row(db_path: Path, task_id: str):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT status, assignee, metadata, workspace_path FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()


def _meta(db_path: Path, task_id: str) -> dict:
    return json.loads(_row(db_path, task_id)["metadata"])


def _activities(db_path: Path, task_id: str, action: str):
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT details FROM task_activity WHERE task_id = ? AND action = ?",
            (task_id, action),
        ).fetchall()


def _dispatcher_comments(db_path: Path, task_id: str):
    with sqlite3.connect(str(db_path)) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ? AND author = 'zf-dispatcher'",
                (task_id,),
            ).fetchall()
        ]


def _task_comments(db_path: Path, task_id: str):
    with sqlite3.connect(str(db_path)) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ?", (task_id,)
            ).fetchall()
        ]


def _update_task(
    db_path: Path,
    task_id: str,
    *,
    status: str,
    assignee: str,
    meta: dict,
    workspace: str | None = None,
):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, assignee = ?, metadata = ?, workspace_path = ? WHERE id = ?",
            (status, assignee, json.dumps(meta), workspace, task_id),
        )
        conn.commit()


# --------------------------------------------------------------------------------------
# Unit: wire format, parser, posting, brief
# --------------------------------------------------------------------------------------


class TestWireFormat:
    def test_request_carries_key_full_head_and_paste_ready_verdict_lines(self):
        key = _key("t1")
        body = arb.format_arbitration_request(
            task_id="t1",
            title="feat: x",
            head_sha=HEAD,
            rounds=2,
            arbiter=ARBITER,
            attempt=1,
            max_arbitrations=2,
        )
        lines = body.splitlines()
        assert (
            lines[0] == arb.REQUEST_PREFIX == "[AI:zf-dispatcher] ARBITRATION REQUEST"
        )
        assert f"REQUEST: {key}" in lines
        assert f"HEAD: {HEAD}" in lines
        assert "Arbitration attempt 1/2." in lines
        for verdict in arb.VERDICTS:
            assert f"`{arb.verdict_line(ARBITER, verdict, key, HEAD)}`" in lines
        assert "Task: t1 - feat: x" in lines

    def test_request_needs_a_full_head_and_one_live_request_per_task(self):
        assert (
            arb.build_arbitration_request(
                {},
                task_id="t1",
                head_sha="short",
                arbiter=ARBITER,
                now=100,
                rounds=2,
                max_arbitrations=2,
            )
            is None
        )
        state = arb.build_arbitration_request(
            {},
            task_id="t1",
            head_sha=HEAD.upper(),
            arbiter=ARBITER,
            now=100,
            rounds=2,
            max_arbitrations=2,
        )
        assert state["status"] == arb.STATUS_REQUESTED
        assert state["request_head"] == HEAD
        assert state["request_key"] == "arbitration:t1:" + HEAD
        # A task never has two live requests.
        assert (
            arb.build_arbitration_request(
                {arb.STATE_KEY: state},
                task_id="t1",
                head_sha=HEAD_2,
                arbiter=ARBITER,
                now=100,
                rounds=2,
                max_arbitrations=2,
            )
            is None
        )

    def test_budget_is_bounded_per_task(self):
        def build(meta: dict, now: int, head: str):
            return arb.build_arbitration_request(
                meta,
                task_id="t1",
                arbiter=ARBITER,
                now=now,
                head_sha=head,
                rounds=2,
                max_arbitrations=2,
            )

        first = build({}, 100, HEAD)
        assert first["attempts"] == 1
        # A settled request (e.g. ESCALATE) counts against the budget.
        settled = {**first, "status": arb.STATUS_ESCALATED}
        second = build({arb.STATE_KEY: settled}, 200, HEAD_2)
        assert second["attempts"] == 2
        assert second["request_key"] == _key("t1", HEAD_2)
        assert build({arb.STATE_KEY: second}, 300, HEAD_3) is None

    def test_settings_come_from_env_like_max_review_rounds(self):
        with patch.dict(os.environ, {"ZEROFACTORY_REVIEW_CAP_ARBITRATION": "0"}):
            assert arb.arbitration_settings()["enabled"] is False
        with patch.dict(os.environ, {"ZEROFACTORY_REVIEW_CAP_ARBITRATION": ""}):
            assert arb.arbitration_settings()["enabled"] is True
        with patch.dict(
            os.environ,
            {
                "ZEROFACTORY_REVIEW_CAP_ARBITER": "zf-other",
                "ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS": "3",
                "ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS": "4",
            },
        ):
            settings = arb.arbitration_settings()
        assert settings["arbiter"] == "zf-other"
        assert settings["max_arbitrations"] == 3
        assert settings["timeout_hours"] == 4

    def test_default_deadline_is_24_hours(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS", None)
            os.environ.pop("ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS", None)
            settings = arb.arbitration_settings()
        assert settings["timeout_hours"] == 24
        assert settings["max_arbitrations"] == 2
        assert settings["arbiter"] == "zf-gatekeeper"
        # Garbage falls back to the default rather than to zero.
        with patch.dict(
            os.environ, {"ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS": "soon"}
        ):
            assert arb.arbitration_settings()["timeout_hours"] == 24

    def test_human_review_tasks_opt_out(self):
        assert arb.is_human_review_task({"permanently_blocked": True}) is True
        assert arb.is_human_review_task({}, ["hitl"]) is True
        assert arb.is_human_review_task({}, "manual-review") is True
        assert arb.is_human_review_task({}, json.dumps(["HITL"])) is True
        assert (
            arb.is_human_review_task({"external_issue": {"labels": ["HITL"]}}) is True
        )
        assert arb.is_human_review_task({}, ["refactor"]) is False
        assert arb.is_human_review_task(None, None) is False

    def test_only_open_requests_go_stale(self):
        now = 10_000_000
        # An open request without a request time has no deadline: unusable.
        assert (
            arb.arbitration_is_stale(
                {"status": "requested", "requested_at": 0}, now, 24
            )
            is True
        )
        assert arb.arbitration_is_stale({"status": "requested"}, now, 24) is True
        for settled in (
            "fix-requested",
            "merge-approved",
            "escalated",
            "expired",
            "void",
        ):
            assert (
                arb.arbitration_is_stale(
                    {"status": settled, "requested_at": now - 25 * 3600}, now, 24
                )
                is False
            )
        assert (
            arb.arbitration_is_stale(
                {"status": "requested", "requested_at": now - 25 * 3600}, now, 24
            )
            is True
        )
        assert (
            arb.arbitration_is_stale(
                {"status": "requested", "requested_at": now - 23 * 3600}, now, 24
            )
            is False
        )
        for settled in (
            "fix-requested",
            "merge-approved",
            "escalated",
            "expired",
            "void",
        ):
            assert (
                arb.arbitration_is_stale(
                    {"status": settled, "requested_at": now - 99 * 3600}, now, 24
                )
                is False
            )

    def test_park_covers_every_outcome_except_an_open_fix_round(self):
        for status in arb.PARKED_STATUSES:
            meta = {"review_cap_reached": True, arb.STATE_KEY: {"status": status}}
            assert arb.is_arbitration_park(meta, status="blocked", assignee="human")
            assert not arb.is_arbitration_park(meta, status="todo", assignee="human")
            assert not arb.is_arbitration_park(
                meta, status="blocked", assignee="zf-builder"
            )
        assert not arb.is_arbitration_park(
            {"review_cap_reached": True, arb.STATE_KEY: {"status": "fix-requested"}},
            status="blocked",
            assignee="human",
        )
        assert not arb.is_arbitration_park(
            {arb.STATE_KEY: {"status": "requested"}}, status="blocked", assignee="human"
        )
        void = arb.voided({"status": "requested", "request_key": "k"}, "pr-closed", 5)
        assert (void["status"], void["void_reason"], void["request_key"]) == (
            "void",
            "pr-closed",
            "k",
        )


def _find(
    comments,
    *,
    task_id="t1",
    key=None,
    request_head=HEAD,
    live_head=HEAD,
    allowed=None,
    requested_at=_STARTED,
    now=_NOW,
    timeout_hours=24,
):
    return arb.find_arbitration_verdict(
        comments,
        arbiter=ARBITER,
        key=key or _key(task_id, request_head),
        request_head=request_head,
        requested_at=requested_at,
        live_head=live_head,
        allowed_logins={ALLOWED} if allowed is None else allowed,
        now=now,
        timeout_hours=timeout_hours,
    )


class TestVerdictParser:
    def _reason(self, comment, **overrides) -> str:
        poll = _find([comment], **overrides)
        assert poll["decision"] is None, poll
        assert len(poll["rejected"]) == 1, poll
        return poll["rejected"][0]["reason"]

    def test_valid_verdict_is_accepted_with_its_identity(self):
        poll = _find(
            [_convo(501, _verdict_body("FIX", "t1"), created_at=_iso(_NOW - 60))]
        )
        decision = poll["decision"]
        assert decision["verdict"] == "FIX"
        assert decision["comment_id"] == "issue_501"
        assert decision["author"] == ALLOWED
        assert decision["first_line"].startswith(
            "[AI:zf-gatekeeper] ARBITRATION VERDICT: FIX"
        )
        assert decision["rationale"].startswith("1. race in refresh()")
        assert decision["created_at"] == _NOW - 60
        assert poll["rejected"] == []
        # Raw GitHub shape and the scheduler's standardized shape agree.
        standardized = _std(501, _verdict_body("FIX", "t1"), created_at=_iso(_NOW - 60))
        assert _find([standardized])["decision"]["comment_id"] == "issue_501"

    def test_verdict_on_a_later_line_is_rejected(self):
        quoted = "Looking at this PR now.\n" + _verdict_body("MERGE", "t1")
        assert self._reason(_convo(1, quoted)) == "not-a-first-line-verdict"
        # So is one hidden behind markdown or a quote marker on the first line.
        assert self._reason(_convo(2, "> " + _verdict_body("MERGE", "t1"))) == (
            "not-a-first-line-verdict"
        )

    def test_malformed_first_line_is_rejected(self):
        key = _key("t1")
        for first in (
            f"[AI:{ARBITER}] ARBITRATION VERDICT: MERGE",  # no request / head
            f"[AI:{ARBITER}] ARBITRATION VERDICT: MERGE | REQUEST: {key} | HEAD: ab12",
            f"[AI:{ARBITER}] ARBITRATION VERDICT: SHIP | REQUEST: {key} | HEAD: {HEAD}",
            f"[AI:{ARBITER}] ARBITRATION VERDICT: MERGE REQUEST: {key} HEAD: {HEAD}",
            f"[AI:{ARBITER}] ARBITRATION VERDICT: FIX | REQUEST: | HEAD: {HEAD}",
            f"{key} [AI:{ARBITER}] ARBITRATION VERDICT: FIX | REQUEST: {key} | HEAD: {HEAD}",
        ):
            assert self._reason(_convo(1, first)) == "not-a-first-line-verdict", first

    def test_wrong_role_tag_is_rejected(self):
        body = _verdict_body("MERGE", "t1", role="zf-reviewer")
        assert self._reason(_convo(1, body)) == "wrong-role-tag"

    def test_wrong_author_is_rejected_even_with_the_right_role_tag(self):
        body = _verdict_body("MERGE", "t1")
        assert self._reason(_convo(1, body, author="someone-else")) == "wrong-author"
        assert self._reason(_convo(1, body), allowed=set()) == "wrong-author"
        # Author matching is on the GitHub login, not on anything in the body.
        assert self._reason(_convo(1, body + "\n(authorized: justr0)", author="x")) == (
            "wrong-author"
        )

    def test_verdict_for_another_request_or_head_is_rejected(self):
        earlier = _verdict_body("FIX", "t1", key=_key("t1", HEAD_2))
        assert self._reason(_convo(1, earlier)) == "request-mismatch"
        other_head = arb.verdict_line(ARBITER, "FIX", _key("t1"), HEAD_2)
        assert self._reason(_convo(2, other_head)) == "head-mismatch"

    def test_verdict_needs_a_creation_time_inside_the_request_window(self):
        body = _verdict_body("FIX", "t1")
        assert self._reason(_convo(1, body, created_at="")) == "missing-timestamp"
        assert (
            self._reason(_convo(1, body, created_at="yesterday")) == "missing-timestamp"
        )
        assert (
            self._reason(_convo(1, body, created_at=_iso(_STARTED - 3600)))
            == "predates-request"
        )
        # Deadline = request + 24h; anything created at or after it is late.
        deadline = _STARTED + 24 * 3600
        assert (
            self._reason(_convo(1, body, created_at=_iso(deadline)))
            == "posted-after-deadline"
        )
        assert (
            self._reason(_convo(1, body, created_at=_iso(_NOW + 3600)))
            == "future-dated"
        )
        # A request whose deadline is just ahead of "now": a reply posted half
        # a minute before the deadline still counts, one exactly at it does not.
        requested = _NOW + 60 - 24 * 3600  # deadline = _NOW + 60
        edge = _find(
            [_convo(2, body, created_at=_iso(_NOW + 30))], requested_at=requested
        )
        assert edge["decision"]["verdict"] == "FIX"
        assert (
            self._reason(
                _convo(3, body, created_at=_iso(_NOW + 60)), requested_at=requested
            )
            == "posted-after-deadline"
        )

    def test_verdict_is_not_applied_when_the_live_head_moved(self):
        body = _verdict_body("FIX", "t1")
        assert self._reason(_convo(1, body), live_head=HEAD_2) == "live-head-changed"
        assert self._reason(_convo(1, body), live_head="") == "live-head-unknown"

    def test_only_conversation_comments_carry_a_verdict(self):
        body = _verdict_body("FIX", "t1")
        inline = _std(1, body, type_="inline_review")
        summary = _std(2, body, type_="review_summary")
        assert _find([inline, summary]) == {"decision": None, "rejected": []}

    def test_latest_valid_verdict_wins_and_an_invalid_one_does_not_override(self):
        comments = [
            _convo(1, _verdict_body("MERGE", "t1"), created_at=_iso(_NOW - 300)),
            _convo(
                2,
                _verdict_body("ESCALATE", "t1"),
                author="stranger",
                created_at=_iso(_NOW - 200),
            ),
            _convo(3, _verdict_body("FIX", "t1"), created_at=_iso(_NOW - 100)),
            _convo(
                4,
                _verdict_body("MERGE", "t1", key=_key("t1", HEAD_2)),
                created_at=_iso(_NOW - 50),
            ),
        ]
        poll = _find(comments)
        assert poll["decision"]["comment_id"] == "issue_3"
        assert [r["reason"] for r in poll["rejected"]] == [
            "wrong-author",
            "request-mismatch",
        ]

    def test_nothing_is_consumed_without_a_request_time_or_with_junk(self):
        poll = _find([_convo(1, _verdict_body("FIX", "t1"))], requested_at=0)
        assert poll == {"decision": None, "rejected": []}
        assert _find(None) == {"decision": None, "rejected": []}
        assert _find([None, "junk", {}]) == {"decision": None, "rejected": []}

    def test_rejections_are_remembered_once_per_comment(self):
        state: dict = {}
        rejected = [{"comment_id": "issue_1", "author": "x", "reason": "wrong-author"}]
        assert len(arb.record_rejections(state, rejected, 1)) == 1
        assert len(arb.record_rejections(state, rejected, 2)) == 0
        assert state["ignored_verdicts"]["issue_1"]["reason"] == "wrong-author"


class TestArbiterIdentity:
    def test_allowlist_comes_from_env_or_the_factory_identities(self):
        with patch.dict(
            os.environ,
            {"ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS": " Alice , @BOB,,"},
        ):
            assert arb.arbiter_github_logins(PR) == {"alice", "bob"}
        with (
            patch.dict(
                os.environ, {"ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS": ""}
            ),
            patch.object(arb, "gh_authenticated_login", return_value="Robot"),
        ):
            # Default: the gh account plus the repository owner.
            assert arb.arbiter_github_logins(PR) == {"robot", "acme"}
            assert arb.arbiter_github_logins(None) == {"robot"}
        # Skip modes never ask gh; only the owner remains.
        with (
            patch.dict(
                os.environ,
                {
                    "ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS": "",
                    "ZEROFACTORY_SKIP_GH_API": "1",
                },
            ),
            patch.object(arb.subprocess, "run") as run,
        ):
            assert arb.arbiter_github_logins(PR) == {"acme"}
            run.assert_not_called()

    def test_gh_authenticated_login_reads_gh_api_user_once(self):
        reply = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Justr0\n", stderr=""
        )
        with (
            patch.dict(
                os.environ, {"ZEROFACTORY_SKIP_GIT": "", "ZEROFACTORY_SKIP_GH_API": ""}
            ),
            patch.object(arb, "_gh_login_cache", {"login": None, "checked_at": 0.0}),
            patch.object(arb.subprocess, "run", return_value=reply) as run,
        ):
            assert arb.gh_authenticated_login(Path(".")) == "justr0"
            assert arb.gh_authenticated_login(Path(".")) == "justr0"
        assert run.call_count == 1
        with (
            patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": "1"}),
            patch.object(arb, "_gh_login_cache", {"login": None, "checked_at": 0.0}),
            patch.object(arb.subprocess, "run") as run,
        ):
            assert arb.gh_authenticated_login(Path(".")) is None
        run.assert_not_called()


class TestRequestPosting:
    def _post(self, **overrides):
        kwargs = dict(
            pr_url=PR, repo_path=Path("."), body="body", key=_key("t1"), head_sha=HEAD
        )
        kwargs.update(overrides)
        return arb.post_arbitration_request(**kwargs)

    def _env(self):
        return patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": "", **ALLOWED_ENV})

    def test_find_request_comment_matches_key_head_and_first_line(self):
        key = _key("t1")
        mine = _convo(10, _request_body(key))
        found = arb.find_request_comment([mine], key=key, head_sha=HEAD)
        assert found["comment_id"] == "issue_10"
        assert (
            arb.find_request_comment([mine], key=key, head_sha=HEAD, authors={ALLOWED})[
                "comment_id"
            ]
            == "issue_10"
        )
        assert (
            arb.find_request_comment([mine], key=_key("t1", HEAD_2), head_sha=HEAD)
            is None
        )
        assert arb.find_request_comment([mine], key=key, head_sha=HEAD_2) is None
        quoted = _convo(11, "FYI:\n" + _request_body(key))
        assert arb.find_request_comment([quoted], key=key, head_sha=HEAD) is None
        # The request may sit on any page of a long thread.
        thread = [_convo(i, "noise") for i in range(150)] + [mine]
        assert arb.find_request_comment(thread, key=key, head_sha=HEAD)[
            "comment_id"
        ] == ("issue_10")

    def test_find_request_comment_honours_the_author_allowlist(self):
        key = _key("t1")
        foreign = _convo(12, _request_body(key), author="stranger")
        assert (
            arb.find_request_comment(
                [foreign], key=key, head_sha=HEAD, authors={ALLOWED}
            )
            is None
        )
        assert (
            arb.find_request_comment(
                [foreign], key=key, head_sha=HEAD, authors={ALLOWED, "stranger"}
            )["comment_id"]
            == "issue_12"
        )

    def test_post_adopts_an_existing_request_comment_instead_of_posting(self):
        key, posted_at = _key("t1"), _NOW - 7200
        existing = _convo(777, _request_body(key), created_at=_iso(posted_at))
        with (
            self._env(),
            patch.object(
                arb, "fetch_pr_conversation_comments", return_value=[existing]
            ) as fetch,
            patch.object(arb, "gh_authenticated_login", return_value=ALLOWED),
            patch.object(arb.subprocess, "run") as run,
        ):
            result = self._post()
        assert result == {
            "comment_id": "issue_777",
            "author": ALLOWED,
            "created_at": posted_at,
            "adopted": True,
        }
        fetch.assert_called_once()
        run.assert_not_called()

    def test_post_adopts_the_dispatchers_own_comment_whatever_the_arbiter_allowlist(
        self,
    ):
        """The request is owned by the account gh posts as. A retry after a
        lost local update must find that comment even when the arbiter
        allowlist names somebody else; a look-alike by a stranger is ignored."""
        key = _key("t1")
        mine = _convo(
            778, _request_body(key), author="dispatcher-bot", created_at=_iso(_NOW - 60)
        )
        foreign = _convo(779, _request_body(key), author=ALLOWED)
        with (
            patch.dict(
                os.environ,
                {
                    "ZEROFACTORY_SKIP_GIT": "",
                    "ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS": ALLOWED,
                },
            ),
            patch.object(arb, "gh_authenticated_login", return_value="Dispatcher-Bot"),
            patch.object(
                arb, "fetch_pr_conversation_comments", return_value=[foreign, mine]
            ) as fetch,
            patch.object(arb.subprocess, "run") as run,
        ):
            result = self._post()
        assert result == {
            "comment_id": "issue_778",
            "author": "dispatcher-bot",
            "created_at": _NOW - 60,
            "adopted": True,
        }
        fetch.assert_called_once()
        run.assert_not_called()

    def test_post_publishes_once_and_returns_the_comment_id(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=f"{PR}#issuecomment-555\n", stderr=""
            )

        with (
            self._env(),
            patch.object(
                arb, "fetch_pr_conversation_comments", return_value=[]
            ) as fetch,
            patch.object(arb.subprocess, "run", side_effect=fake_run) as run,
            patch.object(arb, "gh_authenticated_login", return_value=ALLOWED),
        ):
            result = self._post(body="please decide")
        assert result == {
            "comment_id": "issue_555",
            "author": ALLOWED,
            "created_at": None,
            "adopted": False,
        }
        assert run.call_count == 1
        assert calls[0][:4] == ["gh", "pr", "comment", PR]
        # Reconciled against GitHub before and after the POST.
        assert fetch.call_count == 2
        sent = Path(calls[0][calls[0].index("--body-file") + 1])
        assert not sent.exists()  # temp body file cleaned up

    def test_post_failure_without_a_reconcilable_comment_is_no_live_request(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="gh: network down"
            )

        with (
            self._env(),
            patch.object(arb, "fetch_pr_conversation_comments", return_value=[]),
            patch.object(arb.subprocess, "run", side_effect=fake_run),
            patch.object(arb, "gh_authenticated_login", return_value=ALLOWED),
        ):
            assert self._post() is None

    @pytest.mark.parametrize(
        "outcome",
        [
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="timeout"
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            TimeoutError("gh pr comment timed out"),
        ],
    )
    def test_post_that_landed_but_reported_failure_is_reconciled(self, outcome):
        """A POST GitHub accepted followed by a gh error must not become a duplicate."""
        key = _key("t1")
        existing = _convo(900, _request_body(key), created_at=_iso(_NOW - 60))
        if isinstance(outcome, Exception):
            run_patch = patch.object(arb.subprocess, "run", side_effect=outcome)
        else:
            run_patch = patch.object(arb.subprocess, "run", return_value=outcome)
        with (
            self._env(),
            patch.object(
                arb, "fetch_pr_conversation_comments", side_effect=[[], [existing]]
            ),
            run_patch as run,
            patch.object(arb, "gh_authenticated_login", return_value=ALLOWED),
        ):
            result = self._post()
        assert result == {
            "comment_id": "issue_900",
            "author": ALLOWED,
            "created_at": _NOW - 60,
            "adopted": True,
        }
        assert run.call_count == 1

    def test_post_is_skipped_in_skip_mode_without_a_pr_or_without_an_identity(self):
        with patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": "1", **ALLOWED_ENV}):
            assert self._post() is None
        with self._env():
            assert self._post(pr_url=None) is None
        # The dispatcher's own GitHub account is unknown (gh unavailable, no
        # authenticated login) -> nobody could author or own the comment -> do not post.
        with (
            patch.dict(
                os.environ,
                {
                    "ZEROFACTORY_SKIP_GIT": "",
                    "ZEROFACTORY_SKIP_GH_API": "1",
                    "ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS": ALLOWED,
                },
            ),
            patch.object(arb, "fetch_pr_conversation_comments") as fetch,
            patch.object(arb.subprocess, "run") as run,
        ):
            assert self._post(pr_url="not-a-pr-url") is None
        fetch.assert_not_called()
        run.assert_not_called()


class TestFixRoundBrief:
    def test_brief_is_the_verdict_body_only_while_the_fix_round_is_open(self):
        verdict = _verdict_body("FIX", "t1", detail="1. fix the race in refresh()")
        state = {"status": arb.STATUS_FIX_REQUESTED, "decision_body": verdict}
        assert arb.fix_round_brief({arb.STATE_KEY: state}) == verdict
        # Once the reviewer answered the fix commit, ordinary routing owns the prompt.
        assert arb.fix_round_brief({arb.STATE_KEY: state, "review_round": 1}) is None
        assert (
            arb.fix_round_brief({arb.STATE_KEY: state, "commit_review_count": 1})
            is None
        )
        assert (
            arb.fix_round_brief({arb.STATE_KEY: {**state, "status": "merge-approved"}})
            is None
        )

    def test_a_recorded_review_round_closes_the_fix_round_for_good(self):
        verdict = _verdict_body("FIX", "t1", detail="1. fix the race in refresh()")
        state = {"status": arb.STATUS_FIX_REQUESTED, "decision_body": verdict}
        meta = {arb.STATE_KEY: dict(state)}

        assert arb.close_fix_round(meta, 1234) is True
        assert meta[arb.STATE_KEY]["fix_round_closed_at"] == 1234
        assert arb.fix_round_brief(meta) is None
        # Already closed, or never a fix round: nothing changes.
        assert arb.close_fix_round(meta, 9999) is False
        assert meta[arb.STATE_KEY]["fix_round_closed_at"] == 1234
        assert arb.close_fix_round({arb.STATE_KEY: {"status": "requested"}}, 1) is False
        assert arb.close_fix_round({}, 1) is False
        # Only a reviewer round ends it: an operator reset of review_round on a
        # round that was never reviewed leaves the brief live (it is still the
        # builder's open pass), but never resurrects a closed one.
        unreviewed = {arb.STATE_KEY: dict(state), "review_round": 1}
        unreviewed.pop("review_round")
        assert arb.fix_round_brief(unreviewed) == verdict
        arb.close_fix_round(unreviewed, 5)
        unreviewed.pop("review_round", None)
        assert arb.fix_round_brief(unreviewed) is None
        assert arb.fix_round_brief({arb.STATE_KEY: {"status": "fix-requested"}}) is None
        assert arb.fix_round_brief({}) is None
        assert arb.fix_round_brief(None) is None


class TestConversationPagination:
    def test_fetch_pr_conversation_comments_walks_every_page(self, tmp_path):
        pages = {
            1: [_convo(i, f"c{i}") for i in range(100)],
            2: [_convo(100 + i, f"c{i}") for i in range(3)],
        }
        seen_pages: list[int] = []

        def fake_run(cmd, **kwargs):
            page = int(cmd[2].rsplit("page=", 1)[1])
            seen_pages.append(page)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(pages[page]), stderr=""
            )

        with (
            patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": ""}),
            patch("dispatcher.github_pr.subprocess.run", side_effect=fake_run),
        ):
            items = fetch_pr_conversation_comments(repo_path=tmp_path, pr_url=PR)
        assert seen_pages == [1, 2]
        assert len(items) == 103
        assert items[-1]["id"] == 102

    def test_fetch_pr_conversation_comments_fails_closed(self, tmp_path):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="rate limited"
            )

        with (
            patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": ""}),
            patch("dispatcher.github_pr.subprocess.run", side_effect=fake_run),
        ):
            assert fetch_pr_conversation_comments(repo_path=tmp_path, pr_url=PR) is None
        with patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": "1"}):
            assert fetch_pr_conversation_comments(repo_path=tmp_path, pr_url=PR) is None
        with patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": ""}):
            assert (
                fetch_pr_conversation_comments(repo_path=tmp_path, pr_url="not-a-url")
                is None
            )

    def test_review_fetch_sees_conversation_comments_beyond_the_first_page(
        self, tmp_path
    ):
        page1 = [_convo(i, "noise", author="zf-reviewer") for i in range(100)]
        page2 = [
            _convo(
                500,
                "[AI:zf-reviewer] [Reviewer Feedback] Round 2: a late comment",
                author="zf-reviewer",
            )
        ]

        def fake_run(cmd, *args, **kwargs):
            if "pulls/" in cmd[2]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="[]", stderr=""
                )
            page = int(cmd[2].rsplit("page=", 1)[1])
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps(page1 if page == 1 else page2),
                stderr="",
            )

        with (
            patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": ""}),
            patch("dispatcher.github_pr.subprocess.run", side_effect=fake_run),
        ):
            comments = fetch_pr_review_comments(repo_path=tmp_path, pr_url=PR)
        assert "issue_500" in {c["comment_id"] for c in comments}


# --------------------------------------------------------------------------------------
# Cycle: real run_dispatch_cycle against SQLite, GitHub boundary stubbed
# --------------------------------------------------------------------------------------

_POST_DEFAULT = object()
_ROUND_2 = _std(
    999,
    "[AI:zf-reviewer] [Reviewer Feedback] Round 2: Still seeing issues on commit.",
    author="zf-reviewer",
)


def _parked_state(task_id: str, **overrides) -> dict:
    """A live request on ``task_id``'s current head, posted ten minutes ago."""
    now = int(time.time())
    state = {
        "status": arb.STATUS_REQUESTED,
        "arbiter": ARBITER,
        "attempts": 1,
        "max_arbitrations": 2,
        "requested_at": now - 600,
        "request_head": HEAD,
        "request_key": _key(task_id),
        "rounds": 2,
        "comment_id": "issue_1",
    }
    state.update(overrides)
    return state


class TestArbitrationCycle:
    POSTED = {
        "comment_id": "issue_1",
        "author": ALLOWED,
        "created_at": None,
        "adopted": False,
    }

    def _run(
        self,
        db_path: Path,
        comments: list[dict],
        *,
        conversation=None,
        pr_state="OPEN",
        cycles=1,
        post=_POST_DEFAULT,
        real_post=False,
        head=HEAD,
        mergeable="MERGEABLE",
        env=None,
        unset=(),
        package=None,
        arbiter_available=True,
    ) -> SimpleNamespace:
        """Drive ``run_dispatch_cycle`` with gh, the GitHub comment fetch and the
        worktree helpers stubbed. ``conversation`` is what the paginated
        conversation fetch returns (default: the raw form of ``comments``);
        ``package`` replaces ``_package_and_open_pr`` (the commit/push/reviewer
        handoff boundary)."""
        lock_file = db_path.parent / "dispatcher.lock"
        fake_pr_data = {
            "state": pr_state,
            "reviewDecision": "CHANGES_REQUESTED",
            "url": PR,
            "mergeable": mergeable,
            "headRefOid": head,
        }
        gh_calls: list[list[str]] = []

        def fake_subprocess_run(cmd, *args, **kwargs):
            if "view" in cmd:
                return MagicMock(returncode=0, stdout=json.dumps(fake_pr_data))
            gh_calls.append(list(cmd))
            # git lookups run against a plain temp dir: not a repository.
            return MagicMock(returncode=1 if cmd and cmd[0] == "git" else 0, stdout="")

        if conversation is None:
            conversation = []
            for c in comments:
                try:
                    cid = int(str(c["comment_id"]).split("_", 1)[1])
                except (KeyError, ValueError, IndexError):
                    cid = len(conversation) + 1
                conversation.append(
                    _convo(
                        cid,
                        c["body"],
                        author=c.get("author") or ALLOWED,
                        created_at=(
                            c["created_at"]
                            if "created_at" in c
                            else _iso(int(time.time()) - 60)
                        ),
                    )
                )

        import dispatcher

        environ = {
            "ZEROFACTORY_SKIP_GIT": "",
            "ZEROFACTORY_MAX_REVIEW_ROUNDS": "2",
            "ZEROFACTORY_REVIEW_CAP_ARBITRATION": "1",
            "ZEROFACTORY_REVIEW_CAP_ARBITER": ARBITER,
            "ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS": "2",
            "ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS": "24",
            **ALLOWED_ENV,
            **(env or {}),
        }
        mock_post = MagicMock(return_value=self.POSTED)
        if post is not _POST_DEFAULT:
            mock_post = MagicMock(return_value=post)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    dispatcher, "get_dispatcher_lock_path", return_value=lock_file
                )
            )
            stack.enter_context(patch.dict(os.environ, environ))
            for name in unset:
                os.environ.pop(name, None)
            stack.enter_context(
                patch.object(
                    dispatcher, "resolve_task_repo_path", return_value=db_path.parent
                )
            )
            stack.enter_context(
                patch(
                    "dispatcher.scheduler.subprocess.run",
                    side_effect=fake_subprocess_run,
                )
            )
            stack.enter_context(
                patch.object(
                    dispatcher, "fetch_pr_review_comments", return_value=list(comments)
                )
            )
            stack.enter_context(
                patch.object(
                    dispatcher,
                    "fetch_pr_conversation_comments",
                    return_value=list(conversation),
                )
            )
            stack.enter_context(
                patch.object(
                    arb,
                    "fetch_pr_conversation_comments",
                    return_value=list(conversation),
                )
            )
            stack.enter_context(
                patch.object(
                    arb,
                    "gh_authenticated_login",
                    return_value=ALLOWED if real_post else None,
                )
            )
            stack.enter_context(patch.object(dispatcher, "stop_task_worker"))
            stack.enter_context(patch.object(dispatcher, "_remove_worktree"))
            mock_spawn = stack.enter_context(
                patch.object(
                    dispatcher, "spawn_agent_worker", return_value=(4242, "sess")
                )
            )

            def fake_setup(
                cursor, task_id, title, assignee, tenant, db, *args, **kwargs
            ):
                # The real helper provisions <repo>-worktrees/<task>; mirror that.
                return str(_managed_workspace(db.parent, task_id))

            mock_setup = stack.enter_context(
                patch.object(dispatcher, "setup_worktree", side_effect=fake_setup)
            )
            mock_conflict = stack.enter_context(
                patch.object(dispatcher, "_handle_pr_conflict_from_github")
            )
            stack.enter_context(
                patch.object(
                    arb,
                    "arbiter_profile_available",
                    return_value=arbiter_available,
                )
            )
            if not real_post:
                stack.enter_context(
                    patch.object(arb, "post_arbitration_request", mock_post)
                )
            pkg = None
            if package is not None:
                pkg = stack.enter_context(
                    patch(
                        "dispatcher.scheduler._package_and_open_pr", side_effect=package
                    )
                )
            results = [run_dispatch_cycle(db_path) for _ in range(cycles)]

        assert all(r["ok"] for r in results), results
        return SimpleNamespace(
            post=mock_post,
            setup=mock_setup,
            spawn=mock_spawn,
            conflict=mock_conflict,
            gh_calls=gh_calls,
            package=pkg,
        )

    def _seed_reviewer_at_round_1(self, db_path: Path, task_id: str):
        _seed(
            db_path,
            task_id,
            {
                "last_reviewed_commit": HEAD,
                "review_round": 1,
                "commit_review_count": 1,
                "processed_review_comment_ids": [],
                "blocked_reason_type": "changes-requested",
                "packaged_by": "zf-builder",
            },
            assignee="zf-reviewer",
            workspace=str(_managed_workspace(db_path.parent, task_id)),
        )

    def _seed_parked_with_request(self, db_path: Path, task_id: str, state=None):
        _seed(
            db_path,
            task_id,
            {
                "last_reviewed_commit": HEAD,
                "review_round": 2,
                "commit_review_count": 2,
                "processed_review_comment_ids": [],
                "blocked_reason": "Review cap reached (2 rounds on commit); delegated",
                "blocked_reason_type": "human-gate",
                "review_cap_reached": True,
                "packaged_by": "zf-builder",
                arb.STATE_KEY: state or _parked_state(task_id),
            },
        )

    def _assert_still_parked(self, db_path: Path, task_id: str, status: str):
        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta["review_cap_arbitration"]["status"] == status
        assert meta["review_round"] == 2
        return meta

    # -- request (findings 4, 5) -------------------------------------------------------

    def test_cap_posts_one_request_and_parks(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-cap-arb"
        self._seed_reviewer_at_round_1(db_path, task_id)

        result = self._run(db_path, [_ROUND_2], conversation=[], cycles=3)

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta["review_cap_reached"] is True
        assert meta["review_round"] == 2
        assert "Review cap reached (2 rounds on commit" in meta["blocked_reason"]
        assert ARBITER in meta["blocked_reason"]
        state = meta[arb.STATE_KEY]
        assert state["status"] == arb.STATUS_REQUESTED
        assert state["arbiter"] == ARBITER
        assert (state["attempts"], state["max_arbitrations"]) == (1, 2)
        assert state["request_head"] == HEAD
        assert state["request_key"] == _key(task_id)
        assert state["comment_id"] == "issue_1"
        assert state["posted_by"] == ALLOWED
        assert "adopted" not in state

        # Posted exactly once, idempotent across further cycles.
        assert result.post.call_count == 1
        kwargs = result.post.call_args.kwargs
        assert kwargs["pr_url"] == PR
        assert kwargs["key"] == _key(task_id)
        assert kwargs["head_sha"] == HEAD
        body = kwargs["body"]
        assert body.splitlines()[0] == arb.REQUEST_PREFIX
        assert "Arbitration attempt 1/2." in body
        assert f"HEAD: {HEAD}" in body
        for verdict in arb.VERDICTS:
            assert arb.verdict_line(ARBITER, verdict, _key(task_id), HEAD) in body
        requested = _activities(db_path, task_id, "review_cap_arbitration_requested")
        assert len(requested) == 1
        assert "comment issue_1" in requested[0][0]
        assert any(
            body.splitlines()[0] == arb.REQUEST_PREFIX
            for body in _dispatcher_comments(db_path, task_id)
        )
        result.conflict.assert_not_called()

    def test_missing_arbiter_profile_leaves_a_plain_human_park(self, tmp_path: Path):
        """No arbiter profile on this host -> nobody would answer -> do not ask."""
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-cap-noarb"
        self._seed_reviewer_at_round_1(db_path, task_id)

        result = self._run(
            db_path, [_ROUND_2], conversation=[], cycles=2, arbiter_available=False
        )

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta["review_cap_reached"] is True
        assert arb.STATE_KEY not in meta
        assert "escalating to human review" in meta["blocked_reason"]
        result.post.assert_not_called()

    def test_failed_post_leaves_no_live_request_and_a_later_cycle_retries(
        self, tmp_path: Path, caplog
    ):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-postfail"
        self._seed_reviewer_at_round_1(db_path, task_id)

        with caplog.at_level(logging.WARNING, logger="dispatcher.scheduler"):
            self._run(db_path, [_ROUND_2], conversation=[], post=None)

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        # Plain human park: no request state, no attempt consumed, nothing recorded as posted.
        assert arb.STATE_KEY not in meta
        assert meta["review_cap_reached"] is True
        assert "escalating to human review" in meta["blocked_reason"]
        assert not _activities(db_path, task_id, "review_cap_arbitration_requested")
        assert not _activities(db_path, task_id, "review_cap_arbitration")
        assert not any(
            arb.REQUEST_PREFIX in body
            for body in _dispatcher_comments(db_path, task_id)
        )
        assert any("was not posted" in rec.getMessage() for rec in caplog.records)

        # The next reviewer comment retries; the retry is the first attempt, posted once.
        round_3 = _std(
            1000,
            "[AI:zf-reviewer] [Reviewer Feedback] Round 3: still open.",
            author="zf-reviewer",
        )
        result = self._run(db_path, [round_3], conversation=[], cycles=2)
        state = _meta(db_path, task_id)[arb.STATE_KEY]
        assert state["status"] == arb.STATUS_REQUESTED
        assert state["attempts"] == 1
        assert state["comment_id"] == "issue_1"
        assert result.post.call_count == 1

    def test_lost_local_state_adopts_the_existing_comment_without_duplicate(
        self, tmp_path: Path
    ):
        """The POST landed but the task update was lost: the next cycle must not post again."""
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-adopt"
        self._seed_reviewer_at_round_1(db_path, task_id)
        key = _key(task_id)
        posted_at = int(time.time()) - 1800
        existing = _convo(777, _request_body(key), created_at=_iso(posted_at))

        result = self._run(db_path, [_ROUND_2], conversation=[existing], real_post=True)

        state = _meta(db_path, task_id)[arb.STATE_KEY]
        assert state["status"] == arb.STATUS_REQUESTED
        assert state["comment_id"] == "issue_777"
        assert state["adopted"] is True
        assert state["requested_at"] == posted_at  # deadline anchored to the real post
        assert state["attempts"] == 1
        assert not any("comment" in cmd for cmd in result.gh_calls), result.gh_calls
        (requested,) = _activities(db_path, task_id, "review_cap_arbitration_requested")
        assert "adopted existing comment" in requested[0]

    # -- verdict routing (findings 1, 2, 6) -------------------------------------------

    def test_fix_verdict_reopens_one_bounded_round_then_re_review(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-fix"
        self._seed_parked_with_request(db_path, task_id)
        verdict = _std(501, _verdict_body("FIX", task_id))

        result = self._run(db_path, [verdict])

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("todo", "zf-builder")
        meta = json.loads(row["metadata"])
        # Same reset a human "recover" performs, plus a fresh review budget.
        for key in (
            "review_cap_reached",
            "review_round",
            "commit_review_count",
            "blocked_reason",
            "blocked_reason_type",
        ):
            assert key not in meta, key
        state = meta[arb.STATE_KEY]
        assert state["status"] == arb.STATUS_FIX_REQUESTED
        assert state["decision"] == "FIX"
        assert state["decision_comment_id"] == "issue_501"
        assert state["decision_head"] == HEAD
        assert "race in refresh()" in state["decision_body"]
        assert state["attempts"] == 1
        assert "issue_501" in meta["processed_review_comment_ids"]
        result.setup.assert_called_once()
        bodies = _dispatcher_comments(db_path, task_id)
        assert any(
            "Arbitration fix round (1/2)" in b
            and "=== ARBITRATION VERDICT: FIX ===" in b
            and "race in refresh()" in b
            for b in bodies
        )
        (logged,) = _activities(db_path, task_id, "review_cap_arbitration")
        assert "verdict FIX (attempt 1/2)" in logged[0]
        result.post.assert_not_called()
        # The spawn pass runs before verdict polling, so the builder is
        # dispatched by the next cycle, not this one.
        result.spawn.assert_not_called()

        # Next cycle: the builder is spawned for the one bounded fix round,
        # carrying the verdict (and only the verdict) as its brief.
        spawned = self._run(db_path, [], conversation=[])

        spawned.post.assert_not_called()
        (builder_call,) = spawned.spawn.call_args_list
        assert builder_call.args[4] == "zf-builder"
        brief_meta = json.loads(builder_call.kwargs["metadata"])[arb.STATE_KEY]
        assert brief_meta["status"] == arb.STATUS_FIX_REQUESTED
        assert "race in refresh()" in brief_meta["decision_body"]
        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("running", "zf-builder")

        # The builder commits the fix and its worker exits cleanly: the reaper
        # stamps awaiting_pr, and the next cycle packages the fix commit (push,
        # PR sync, reviewer handoff) - i.e. re-review is dispatched for the task.
        from dispatcher.config import _active_workers
        from dispatcher.reaper import reap_active_workers

        now = int(time.time())
        meta = _meta(db_path, task_id)
        meta.update(
            worker_pid=4321,
            session_id="sess-fix",
            started_at=now - 60,
            sessions=[
                {"session_id": "sess-fix", "status": "ongoing", "started_at": now - 60}
            ],
        )
        _update_task(
            db_path,
            task_id,
            status="running",
            assignee="zf-builder",
            meta=meta,
            workspace=str(_managed_workspace(db_path.parent, task_id)),
        )
        worker = MagicMock()
        worker.pid = 4321
        worker.poll.return_value = 0
        _active_workers[task_id] = worker
        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                assert reap_active_workers(conn.cursor(), now=now) == 1
                conn.commit()
        finally:
            _active_workers.pop(task_id, None)
        meta = _meta(db_path, task_id)
        assert meta["awaiting_pr"] is True
        assert meta[arb.STATE_KEY]["status"] == arb.STATUS_FIX_REQUESTED

        pkg_calls: list[str] = []

        def fake_package(conn, cursor, row, meta, workspace_path, *args, **kwargs):
            """The real packaging's tail: PR synced, task routed to the reviewer
            (commit/push/gh are outside this test's GitHub boundary)."""
            pkg_calls.append(row["id"])
            cursor.execute(
                "UPDATE tasks SET assignee = 'zf-reviewer', pr_url = ?, status = 'todo' WHERE id = ?",
                (PR, row["id"]),
            )
            conn.commit()
            return True

        second = self._run(db_path, [], conversation=[], package=fake_package)

        assert pkg_calls == [task_id]
        second.post.assert_not_called()
        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("todo", "zf-reviewer")
        # The fix commit is reviewed on a fresh budget: no round recorded
        # against it yet, and the request was not re-opened.
        meta = json.loads(row["metadata"])
        assert meta[arb.STATE_KEY]["status"] == arb.STATUS_FIX_REQUESTED
        assert "review_round" not in meta and "commit_review_count" not in meta

        # Next cycle: the reviewer is dispatched for the fix commit.
        third = self._run(db_path, [], conversation=[])

        third.post.assert_not_called()
        (review_call,) = third.spawn.call_args_list
        assert review_call.args[4] == "zf-reviewer"
        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("running", "zf-reviewer")

        # The reviewer requests changes on the fix commit: round 1 of the fresh
        # budget goes back to the builder the ordinary way. The verdict's round
        # is over, and no operator reset of review_round may revive its brief.
        meta = _meta(db_path, task_id)
        meta.update(blocked_reason_type="changes-requested", blocked_reason="nit")
        _update_task(
            db_path, task_id, status="blocked", assignee="zf-reviewer", meta=meta
        )
        nit = _std(
            601,
            "[AI:zf-reviewer] [Reviewer Feedback] rename the new helper.",
            author="zf-reviewer",
        )

        fourth = self._run(db_path, [nit])

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("todo", "zf-builder")
        meta = json.loads(row["metadata"])
        assert meta["review_round"] == 1
        assert meta[arb.STATE_KEY]["status"] == arb.STATUS_FIX_REQUESTED
        assert meta[arb.STATE_KEY]["fix_round_closed_at"]
        assert arb.fix_round_brief(meta) is None
        reset = {
            k: v
            for k, v in meta.items()
            if k not in ("review_round", "commit_review_count")
        }
        assert arb.fix_round_brief(reset) is None
        fourth.post.assert_not_called()

    def test_merge_verdict_keeps_task_parked_until_merged(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-merge"
        self._seed_parked_with_request(db_path, task_id)
        verdict = _std(
            502, _verdict_body("MERGE", task_id, detail="Only a docstring nit remains.")
        )

        self._run(db_path, [verdict])

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        state = meta[arb.STATE_KEY]
        assert state["status"] == arb.STATUS_MERGE_APPROVED
        assert state["decision_head"] == HEAD
        assert "arbiter approved the merge" in meta["blocked_reason"]
        assert "docstring nit" in meta["blocked_reason"]
        assert len(_activities(db_path, task_id, "review_cap_arbitration")) == 1

        # The arbiter (or the human) merges -> the existing merged path finishes the task.
        self._run(db_path, [verdict], pr_state="MERGED")
        row = _row(db_path, task_id)
        assert row["status"] == "done"
        assert json.loads(row["metadata"])[arb.STATE_KEY]["status"] == (
            arb.STATUS_MERGE_APPROVED
        )

    def test_pr_merged_while_still_waiting_voids_the_request(self, tmp_path: Path):
        """A human merging before the arbiter answers ends the request."""
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-merged-waiting"
        self._seed_parked_with_request(db_path, task_id)

        self._run(db_path, [], pr_state="MERGED")

        row = _row(db_path, task_id)
        assert row["status"] == "done"
        state = json.loads(row["metadata"])[arb.STATE_KEY]
        assert state["status"] == arb.STATUS_VOID
        assert state["void_reason"] == "pr-merged"
        assert len(_activities(db_path, task_id, "review_cap_arbitration_void")) == 1

    def test_escalate_verdict_leaves_task_for_human(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-escalate"
        self._seed_parked_with_request(db_path, task_id)
        verdict = _std(
            503,
            _verdict_body("ESCALATE", task_id, detail="Spec is ambiguous about auth."),
        )

        result = self._run(db_path, [verdict])

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta[arb.STATE_KEY]["status"] == arb.STATUS_ESCALATED
        assert "arbiter escalated to human" in meta["blocked_reason"]
        assert "Spec is ambiguous" in meta["blocked_reason"]
        result.setup.assert_not_called()

    def test_head_change_after_the_request_ignores_the_verdict(self, tmp_path: Path):
        """A verdict for head A must not reopen work once the PR moved to head B."""
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-headmoved"
        self._seed_parked_with_request(db_path, task_id)
        verdict = _std(504, _verdict_body("FIX", task_id))

        result = self._run(db_path, [verdict], head=HEAD_2)

        meta = self._assert_still_parked(db_path, task_id, arb.STATUS_REQUESTED)
        assert "decision" not in meta[arb.STATE_KEY]
        result.setup.assert_not_called()
        (ignored,) = _activities(db_path, task_id, "review_cap_arbitration_ignored")
        assert "live-head-changed" in ignored[0]
        # Logged once, not every cycle.
        self._run(db_path, [], head=HEAD_2)
        assert len(_activities(db_path, task_id, "review_cap_arbitration_ignored")) == 1

    def test_verdict_carried_over_from_an_earlier_request_is_ignored(
        self, tmp_path: Path
    ):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-oldverdict"
        self._seed_parked_with_request(db_path, task_id)
        old_key = _key(task_id, HEAD_2)
        stale = _std(505, _verdict_body("FIX", task_id, key=old_key))

        result = self._run(db_path, [stale])

        meta = self._assert_still_parked(db_path, task_id, arb.STATUS_REQUESTED)
        assert "decision" not in meta[arb.STATE_KEY]
        result.setup.assert_not_called()
        (ignored,) = _activities(db_path, task_id, "review_cap_arbitration_ignored")
        assert "request-mismatch" in ignored[0]
        assert (
            "issue_505" in meta["processed_review_comment_ids"]
        )  # recorded for visibility only

    @pytest.mark.parametrize(
        "label, body, convo_kwargs, expected",
        [
            (
                "later-line",
                "Verdict below.\n" + _verdict_body("FIX", "t-arb-reject"),
                {},
                "not-a-first-line-verdict",
            ),
            (
                "malformed",
                f"[AI:{ARBITER}] ARBITRATION VERDICT: FIX",
                {},
                "not-a-first-line-verdict",
            ),
            (
                "wrong-role",
                _verdict_body("FIX", "t-arb-reject", role="zf-reviewer"),
                {},
                "wrong-role-tag",
            ),
            (
                "wrong-author",
                _verdict_body("FIX", "t-arb-reject"),
                {"author": "stranger"},
                "wrong-author",
            ),
        ],
    )
    def test_polling_boundary_rejects_untrusted_verdicts(
        self, tmp_path: Path, label, body, convo_kwargs, expected
    ):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-reject"
        self._seed_parked_with_request(db_path, task_id)
        raw = _convo(510, body, **convo_kwargs)
        standardized = _std(510, body, author=convo_kwargs.get("author", ALLOWED))

        result = self._run(db_path, [standardized], conversation=[raw])

        self._assert_still_parked(db_path, task_id, arb.STATUS_REQUESTED)
        result.setup.assert_not_called()
        result.post.assert_not_called()
        (ignored,) = _activities(db_path, task_id, "review_cap_arbitration_ignored")
        assert expected in ignored[0]

    def test_inline_review_body_never_counts_as_a_verdict(self, tmp_path: Path):
        """The verdict line inside an inline review comment is the wrong source."""
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-inline"
        self._seed_parked_with_request(db_path, task_id)
        body = _verdict_body("MERGE", task_id)
        inline = _std(511, body, type_="inline_review")
        inline.update(path="dispatcher/x.py", line=3)

        result = self._run(db_path, [inline], conversation=[inline])

        self._assert_still_parked(db_path, task_id, arb.STATUS_REQUESTED)
        result.setup.assert_not_called()
        # Not a verdict, but still an ordinary recorded review comment.
        assert "issue_511" in _meta(db_path, task_id)["processed_review_comment_ids"]

    def test_exhausted_budget_falls_back_to_plain_human_park(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-budget"
        _seed(
            db_path,
            task_id,
            {
                "last_reviewed_commit": HEAD,
                "review_round": 1,
                "commit_review_count": 1,
                "processed_review_comment_ids": [],
                "blocked_reason_type": "changes-requested",
                "packaged_by": "zf-builder",
                arb.STATE_KEY: _parked_state(
                    task_id,
                    status=arb.STATUS_ESCALATED,
                    attempts=2,
                    requested_at=int(time.time()) - 48 * 3600,
                    request_head=HEAD_2,
                ),
            },
            assignee="zf-reviewer",
            workspace=str(_managed_workspace(tmp_path, task_id)),
        )

        result = self._run(db_path, [_ROUND_2], conversation=[])

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert "escalating to human review" in meta["blocked_reason"]
        assert meta[arb.STATE_KEY]["status"] == arb.STATUS_ESCALATED  # untouched
        assert not _activities(db_path, task_id, "review_cap_arbitration_requested")
        result.post.assert_not_called()

    # -- parked stays parked (finding 3) ----------------------------------------------

    def test_conflict_while_waiting_stays_parked(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-conflict"
        self._seed_parked_with_request(db_path, task_id)

        result = self._run(db_path, [], mergeable="CONFLICTING")

        meta = self._assert_still_parked(db_path, task_id, arb.STATUS_REQUESTED)
        assert "conflict_retries" not in meta
        result.conflict.assert_not_called()
        result.setup.assert_not_called()

    @pytest.mark.parametrize(
        "status, requested_at_ago",
        [
            (arb.STATUS_MERGE_APPROVED, 600),
            (arb.STATUS_ESCALATED, 600),
            (arb.STATUS_EXPIRED, 30 * 3600),
            (arb.STATUS_VOID, 600),
        ],
    )
    def test_ordinary_comments_after_a_settled_outcome_never_reroute(
        self, tmp_path: Path, status, requested_at_ago
    ):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-settled"
        self._seed_parked_with_request(
            db_path,
            task_id,
            _parked_state(
                task_id,
                status=status,
                requested_at=int(time.time()) - requested_at_ago,
                void_reason="arbitration disabled"
                if status == arb.STATUS_VOID
                else None,
            ),
        )
        follow_ups = [
            _std(
                600 + n,
                f"[AI:zf-reviewer] [Reviewer Feedback] Round {3 + n}: one more thing.",
                author="zf-reviewer",
            )
            for n in range(2)
        ]

        result = self._run(db_path, follow_ups, conversation=[], cycles=3)

        meta = self._assert_still_parked(db_path, task_id, status)
        assert "issue_600" in meta["processed_review_comment_ids"]
        assert "issue_601" in meta["processed_review_comment_ids"]
        result.post.assert_not_called()
        result.setup.assert_not_called()
        result.conflict.assert_not_called()
        assert (
            len(_activities(db_path, task_id, "review_cap_arbitration_requested")) == 0
        )
        assert len(_activities(db_path, task_id, "changes_requested")) == 0
        recorded = _task_comments(db_path, task_id)
        assert sum("one more thing" in body for body in recorded) == 2

    def test_pr_closed_voids_the_request(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-closed"
        self._seed_parked_with_request(db_path, task_id)

        self._run(db_path, [], pr_state="CLOSED")

        assert _row(db_path, task_id)["status"] == "done"
        meta = _meta(db_path, task_id)
        assert meta[arb.STATE_KEY]["status"] == arb.STATUS_VOID
        assert meta[arb.STATE_KEY]["void_reason"] == "pr-closed"
        assert len(_activities(db_path, task_id, "review_cap_arbitration_void")) == 1

    def test_operator_recovery_lifts_the_park_and_voids_the_request(
        self, tmp_path: Path
    ):
        """The recover CLI's reset (cap markers dropped, task handed back to the
        builder) is the one move besides a merged/closed PR that lifts a park."""
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-recovered"
        self._seed_parked_with_request(db_path, task_id)
        meta = _meta(db_path, task_id)
        for key in (
            "review_cap_reached",
            "review_round",
            "commit_review_count",
            "blocked_reason",
            "blocked_reason_type",
        ):
            meta.pop(key, None)
        meta["awaiting_pr"] = True
        _update_task(
            db_path,
            task_id,
            status="running",
            assignee="zf-builder",
            meta=meta,
            workspace=str(_managed_workspace(db_path.parent, task_id)),
        )

        result = self._run(db_path, [], conversation=[], package=lambda *a, **k: True)

        state = _meta(db_path, task_id)[arb.STATE_KEY]
        assert state["status"] == arb.STATUS_VOID
        assert state["void_reason"] == "task-left-park"
        assert len(_activities(db_path, task_id, "review_cap_arbitration_void")) == 1
        result.package.assert_called_once()
        result.post.assert_not_called()

    # -- consumption-time guards (finding 5) ------------------------------------------

    def test_disabling_arbitration_while_pending_voids_the_request(
        self, tmp_path: Path
    ):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-disabled"
        self._seed_parked_with_request(db_path, task_id)
        verdict = _std(520, _verdict_body("FIX", task_id))

        result = self._run(
            db_path, [verdict], env={"ZEROFACTORY_REVIEW_CAP_ARBITRATION": "0"}
        )

        meta = self._assert_still_parked(db_path, task_id, arb.STATUS_VOID)
        assert meta[arb.STATE_KEY]["void_reason"] == "arbitration disabled"
        assert (
            "arbitration request voided (arbitration disabled)"
            in meta["blocked_reason"]
        )
        assert meta["blocked_reason_type"] == "human-gate"
        assert "decision" not in meta[arb.STATE_KEY]
        result.setup.assert_not_called()
        result.post.assert_not_called()
        (voided,) = _activities(db_path, task_id, "review_cap_arbitration_void")
        assert "stays parked for human review" in voided[0]

    def test_newly_reserved_task_voids_the_request(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-optout"
        self._seed_parked_with_request(db_path, task_id)
        meta = _meta(db_path, task_id)
        meta["permanently_blocked"] = True
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE tasks SET metadata = ? WHERE id = ?",
                (json.dumps(meta), task_id),
            )
            conn.commit()
        verdict = _std(521, _verdict_body("MERGE", task_id))

        result = self._run(db_path, [verdict])

        meta = self._assert_still_parked(db_path, task_id, arb.STATUS_VOID)
        assert meta[arb.STATE_KEY]["void_reason"] == "task reserved for human review"
        assert "decision" not in meta[arb.STATE_KEY]
        result.setup.assert_not_called()

    def test_reply_after_the_deadline_is_not_applied(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-late"
        requested_at = int(time.time()) - 30 * 3600
        self._seed_parked_with_request(
            db_path, task_id, _parked_state(task_id, requested_at=requested_at)
        )
        posted_after_deadline = requested_at + 24 * 3600 + 600
        verdict = _std(
            522, _verdict_body("FIX", task_id), created_at=_iso(posted_after_deadline)
        )

        result = self._run(db_path, [verdict])

        meta = self._assert_still_parked(db_path, task_id, arb.STATUS_EXPIRED)
        assert "decision" not in meta[arb.STATE_KEY]
        assert "arbiter did not answer within 24h" in meta["blocked_reason"]
        result.setup.assert_not_called()
        (ignored,) = _activities(db_path, task_id, "review_cap_arbitration_ignored")
        assert "posted-after-deadline" in ignored[0]
        assert len(_activities(db_path, task_id, "review_cap_arbitration_expired")) == 1

    def test_unanswered_request_expires_on_the_24h_default(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-expire"
        self._seed_parked_with_request(
            db_path,
            task_id,
            _parked_state(task_id, requested_at=int(time.time()) - 25 * 3600),
        )

        result = self._run(
            db_path, [], unset=("ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS",)
        )

        meta = self._assert_still_parked(db_path, task_id, arb.STATUS_EXPIRED)
        assert "arbiter did not answer within 24h" in meta["blocked_reason"]
        assert meta["blocked_reason_type"] == "human-gate"
        assert len(_activities(db_path, task_id, "review_cap_arbitration_expired")) == 1
        result.post.assert_not_called()

    def test_request_inside_the_24h_window_is_still_open(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-open"
        self._seed_parked_with_request(
            db_path,
            task_id,
            _parked_state(task_id, requested_at=int(time.time()) - 23 * 3600),
        )

        result = self._run(
            db_path, [], unset=("ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS",)
        )

        self._assert_still_parked(db_path, task_id, arb.STATUS_REQUESTED)
        assert not _activities(db_path, task_id, "review_cap_arbitration_expired")
        result.post.assert_not_called()
