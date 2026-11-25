"""Review-cap arbitration (Option A): at the review cap the dispatcher hands the
decision to an arbiter profile instead of parking the task for a human.

Unit level: verdict parsing, per-task budget, staleness, opt-outs.
Cycle level: the request is posted exactly once at the cap; one verdict comment
routes the task (FIX re-opens a bounded round, MERGE waits for the arbiter's
merge, ESCALATE and timeouts leave it parked for the human).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dispatcher.scheduler import run_dispatch_cycle
from dispatcher import review_cap_arbitration as arb

PR = "https://github.com/acme/repo/pull/9"
HEAD = "sha_arb_head_1"


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


class TestArbitrationUnit:
    def test_parse_requires_arbiter_role_and_line_start(self):
        decision = arb.parse_arbitration_decision(
            [
                {
                    "comment_id": "v1",
                    "author": "justr0",
                    "body": "[AI:zf-gatekeeper] ARBITRATION VERDICT: FIX\n\n1. defect A (repro: run x)",
                }
            ],
            "zf-gatekeeper",
        )
        assert decision["verdict"] == "FIX"
        assert decision["comment_id"] == "v1"
        assert decision["rationale"].startswith("1. defect A")

        # A quoted verdict inside someone else's comment is not a verdict.
        assert (
            arb.parse_arbitration_decision(
                [
                    {
                        "comment_id": "q",
                        "author": "zf-reviewer",
                        "body": "[AI:zf-reviewer] Reviewer Feedback\n> [AI:zf-gatekeeper] ARBITRATION VERDICT: MERGE",
                    }
                ],
                "zf-gatekeeper",
            )
            is None
        )
        # Another bot's verdict does not count.
        assert (
            arb.parse_arbitration_decision(
                [
                    {
                        "comment_id": "o",
                        "author": "x",
                        "body": "[AI:zf-reviewer] ARBITRATION VERDICT: MERGE",
                    }
                ],
                "zf-gatekeeper",
            )
            is None
        )
        # Case-insensitive, and the latest verdict wins.
        latest = arb.parse_arbitration_decision(
            [
                {
                    "comment_id": "a",
                    "author": "x",
                    "body": "[AI:zf-gatekeeper] arbitration verdict: merge",
                },
                {
                    "comment_id": "b",
                    "author": "x",
                    "body": "[AI:zf-gatekeeper] ARBITRATION VERDICT: escalate",
                },
            ],
            "zf-gatekeeper",
        )
        assert latest["verdict"] == "ESCALATE"
        assert latest["comment_id"] == "b"

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

    def test_budget_is_bounded_per_task(self):
        first = arb.build_arbitration_request(
            {},
            arbiter="zf-gatekeeper",
            now=100,
            head_sha="h",
            rounds=2,
            max_arbitrations=2,
        )
        assert first["attempts"] == 1
        second = arb.build_arbitration_request(
            {"review_cap_arbitration": first},
            arbiter="zf-gatekeeper",
            now=200,
            head_sha="h2",
            rounds=2,
            max_arbitrations=2,
        )
        assert second["attempts"] == 2
        assert (
            arb.build_arbitration_request(
                {"review_cap_arbitration": second},
                arbiter="zf-gatekeeper",
                now=300,
                head_sha="h3",
                rounds=2,
                max_arbitrations=2,
            )
            is None
        )

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
        assert (
            arb.arbitration_is_stale({"status": "requested", "requested_at": 0}, now, 6)
            is False
        )
        assert (
            arb.arbitration_is_stale(
                {"status": "requested", "requested_at": now - 7 * 3600}, now, 6
            )
            is True
        )
        assert (
            arb.arbitration_is_stale(
                {"status": "requested", "requested_at": now - 3600}, now, 6
            )
            is False
        )
        assert (
            arb.arbitration_is_stale(
                {"status": "fix-requested", "requested_at": now - 99 * 3600}, now, 6
            )
            is False
        )


class TestArbitrationCycle:
    def _run(self, db_path: Path, comments, *, pr_state="OPEN", cycles=1):
        lock_file = db_path.parent / "dispatcher.lock"
        fake_pr_data = {
            "state": pr_state,
            "reviewDecision": "CHANGES_REQUESTED",
            "url": PR,
            "mergeable": "MERGEABLE",
            "headRefOid": HEAD,
        }

        def fake_subprocess_run(cmd, *args, **kwargs):
            if "view" in cmd:
                return MagicMock(returncode=0, stdout=json.dumps(fake_pr_data))
            return MagicMock(returncode=0, stdout="")

        import dispatcher

        with (
            patch.object(
                dispatcher, "get_dispatcher_lock_path", return_value=lock_file
            ),
            patch.dict(
                os.environ,
                {
                    "ZEROFACTORY_SKIP_GIT": "",
                    "ZEROFACTORY_MAX_REVIEW_ROUNDS": "2",
                    "ZEROFACTORY_REVIEW_CAP_ARBITRATION": "1",
                    "ZEROFACTORY_REVIEW_CAP_ARBITER": "zf-gatekeeper",
                    "ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS": "2",
                    "ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS": "6",
                },
            ),
            patch.object(
                dispatcher, "resolve_task_repo_path", return_value=db_path.parent
            ),
            patch(
                "dispatcher.scheduler.subprocess.run", side_effect=fake_subprocess_run
            ),
            patch.object(dispatcher, "fetch_pr_review_comments", return_value=comments),
            patch.object(dispatcher, "stop_task_worker"),
            patch.object(dispatcher, "_remove_worktree"),
            patch.object(dispatcher, "setup_worktree") as mock_setup,
            patch.object(arb, "arbiter_profile_available", return_value=True),
            patch.object(
                arb, "post_arbitration_request", return_value=True
            ) as mock_post,
        ):
            for _ in range(cycles):
                res = run_dispatch_cycle(db_path)
                assert res["ok"] is True
        return mock_post, mock_setup

    def test_cap_posts_one_request_and_parks(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-cap-arb"
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
            workspace=str(_managed_workspace(tmp_path, task_id)),
        )
        round_2 = [
            {
                "comment_id": "cmt_999",
                "type": "pr_comment",
                "author": "zf-reviewer",
                "state": "COMMENTED",
                "body": "[AI:zf-reviewer] [Reviewer Feedback] Round 2: Still seeing issues on commit.",
            }
        ]

        mock_post, _ = self._run(db_path, round_2, cycles=3)

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta["review_cap_reached"] is True
        assert meta["review_round"] == 2
        assert "Review cap reached (2 rounds on commit" in meta["blocked_reason"]
        assert "zf-gatekeeper" in meta["blocked_reason"]
        state = meta["review_cap_arbitration"]
        assert state["status"] == "requested"
        assert state["arbiter"] == "zf-gatekeeper"
        assert (state["attempts"], state["max_arbitrations"]) == (1, 2)
        assert state["request_head"] == HEAD

        # Posted exactly once, idempotent across further cycles.
        assert mock_post.call_count == 1
        args = mock_post.call_args.args
        assert args[0] == PR
        assert "attempt 1/2" in args[2]
        assert "ARBITRATION VERDICT: FIX" in args[2]
        assert (
            len(_activities(db_path, task_id, "review_cap_arbitration_requested")) == 1
        )
        assert any(
            "Review-cap arbitration requested" in body
            for body in _dispatcher_comments(db_path, task_id)
        )

    def _seed_parked_with_request(
        self, db_path: Path, task_id: str, requested_at: int | None = None
    ):
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
                "review_cap_arbitration": {
                    "status": "requested",
                    "arbiter": "zf-gatekeeper",
                    "attempts": 1,
                    "max_arbitrations": 2,
                    "requested_at": requested_at
                    if requested_at is not None
                    else int(time.time()),
                    "request_head": HEAD,
                    "rounds": 2,
                },
            },
        )

    def test_fix_verdict_reopens_one_bounded_round(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-fix"
        self._seed_parked_with_request(db_path, task_id)
        verdict = [
            {
                "comment_id": "v1",
                "type": "pr_comment",
                "author": "justr0",
                "state": "COMMENTED",
                "body": "[AI:zf-gatekeeper] ARBITRATION VERDICT: FIX\n\n1. race in refresh() - repro: run two cycles",
            }
        ]

        _, mock_setup = self._run(db_path, verdict)

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
        state = meta["review_cap_arbitration"]
        assert state["status"] == "fix-requested"
        assert state["decision"] == "FIX"
        assert state["decision_comment_id"] == "v1"
        assert state["attempts"] == 1
        assert "v1" in meta["processed_review_comment_ids"]
        mock_setup.assert_called_once()
        bodies = _dispatcher_comments(db_path, task_id)
        assert any(
            "Arbitration fix round (1/2)" in b
            and "=== ARBITRATION VERDICT: FIX ===" in b
            and "race in refresh()" in b
            for b in bodies
        )
        logged = _activities(db_path, task_id, "review_cap_arbitration")
        assert len(logged) == 1
        assert "verdict FIX (attempt 1/2)" in logged[0][0]

    def test_merge_verdict_keeps_task_parked_until_merged(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-merge"
        self._seed_parked_with_request(db_path, task_id)
        verdict = [
            {
                "comment_id": "v2",
                "type": "pr_comment",
                "author": "justr0",
                "state": "COMMENTED",
                "body": "[AI:zf-gatekeeper] ARBITRATION VERDICT: MERGE\n\nOnly a docstring nit remains.",
            }
        ]

        self._run(db_path, verdict)

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta["review_cap_arbitration"]["status"] == "merge-approved"
        assert "arbiter approved the merge" in meta["blocked_reason"]
        assert "docstring nit" in meta["blocked_reason"]
        assert len(_activities(db_path, task_id, "review_cap_arbitration")) == 1

        # The arbiter (or the human) merges -> the existing merged path finishes the task.
        self._run(db_path, verdict, pr_state="MERGED")
        assert _row(db_path, task_id)["status"] == "done"

    def test_escalate_verdict_leaves_task_for_human(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-escalate"
        self._seed_parked_with_request(db_path, task_id)
        verdict = [
            {
                "comment_id": "v3",
                "type": "pr_comment",
                "author": "justr0",
                "state": "COMMENTED",
                "body": "[AI:zf-gatekeeper] ARBITRATION VERDICT: ESCALATE\n\nSpec is ambiguous about auth.",
            }
        ]

        self._run(db_path, verdict)

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta["review_cap_arbitration"]["status"] == "escalated"
        assert "arbiter escalated to human" in meta["blocked_reason"]

    def test_unanswered_request_expires_into_human_park(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        task_id = "t-arb-expire"
        self._seed_parked_with_request(
            db_path, task_id, requested_at=int(time.time()) - 7 * 3600
        )

        mock_post, _ = self._run(db_path, [])

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert meta["review_cap_arbitration"]["status"] == "expired"
        assert "arbiter did not answer" in meta["blocked_reason"]
        assert len(_activities(db_path, task_id, "review_cap_arbitration_expired")) == 1
        mock_post.assert_not_called()

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
                "review_cap_arbitration": {
                    "status": "escalated",
                    "arbiter": "zf-gatekeeper",
                    "attempts": 2,
                    "max_arbitrations": 2,
                    "requested_at": int(time.time()) - 48 * 3600,
                    "request_head": "old",
                    "rounds": 2,
                },
            },
            assignee="zf-reviewer",
            workspace=str(_managed_workspace(tmp_path, task_id)),
        )
        round_2 = [
            {
                "comment_id": "cmt_777",
                "type": "pr_comment",
                "author": "zf-reviewer",
                "state": "COMMENTED",
                "body": "[AI:zf-reviewer] [Reviewer Feedback] Round 2: Still broken.",
            }
        ]

        mock_post, _ = self._run(db_path, round_2)

        row = _row(db_path, task_id)
        assert (row["status"], row["assignee"]) == ("blocked", "human")
        meta = json.loads(row["metadata"])
        assert "escalating to human review" in meta["blocked_reason"]
        assert (
            len(_activities(db_path, task_id, "review_cap_arbitration_requested")) == 0
        )
        mock_post.assert_not_called()
