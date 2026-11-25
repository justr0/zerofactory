"""Unit tests for dispatcher/worker_spawner.py: agent worker spawning, session isolation, and environment injection."""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from dispatcher.worker_spawner import (
    _inject_langfuse_env,
    spawn_agent_worker,
)


def test_inject_langfuse_env():
    """Verify Langfuse env injection when enabled vs disabled."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)"
    )
    conn.execute("INSERT INTO settings VALUES ('langfuse_enabled', 'true', 1000)")
    conn.execute("INSERT INTO settings VALUES ('langfuse_public_key', 'pk-123', 1000)")
    conn.execute("INSERT INTO settings VALUES ('langfuse_secret_key', 'sk-456', 1000)")
    conn.commit()

    env = {}
    _inject_langfuse_env(env, conn_or_cursor=conn)
    assert env["HERMES_LANGFUSE_PUBLIC_KEY"] == "pk-123"
    assert env["HERMES_LANGFUSE_SECRET_KEY"] == "sk-456"

    # Now disable
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'langfuse_enabled'")
    conn.commit()
    _inject_langfuse_env(env, conn_or_cursor=conn)
    assert "HERMES_LANGFUSE_PUBLIC_KEY" not in env
    assert "HERMES_LANGFUSE_SECRET_KEY" not in env


def test_spawn_agent_worker_skip_env():
    """When ZEROFACTORY_SKIP_WORKER_SPAWN is set, spawn returns None immediately."""
    with patch.dict(os.environ, {"ZEROFACTORY_SKIP_WORKER_SPAWN": "1"}):
        pid, sess = spawn_agent_worker(
            "t1", "Title", "Desc", "P1", "zf-builder", "/tmp", "task/t1"
        )
        assert pid is None
        assert sess is None


def test_spawn_agent_worker_cmd_and_session_isolation():
    """Verify hermes command structure, start_new_session=True, and session matching."""
    with (
        tempfile.NamedTemporaryFile(suffix=".db") as tmp_db,
        tempfile.TemporaryDirectory() as tmp_ws,
    ):
        conn = sqlite3.connect(tmp_db.name)
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                cwd TEXT,
                started_at REAL
            )
            """
        )
        now = time.time()
        # Concurrent session from other task
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-other', 'Task ID: zf-other', '/ws/other', ?)",
            (now + 0.1,),
        )
        # Session for target task
        conn.execute(
            "INSERT INTO sessions VALUES ('sess-mine', 'Task ID: zf-mine', ?, ?)",
            (tmp_ws, now),
        )
        conn.commit()
        conn.close()

        import dispatcher

        with (
            patch.object(
                dispatcher, "resolve_profile_state_db", return_value=Path(tmp_db.name)
            ),
            patch("subprocess.Popen") as mock_popen,
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            pid, sess = spawn_agent_worker(
                task_id="zf-mine",
                title="My Task",
                assignee="zf-reviewer",
                priority="P0",
                description="Test isolation",
                workspace_path=tmp_ws,
                branch_name="task/zf-mine",
            )

            assert pid == 99999
            assert sess == "sess-mine"
            assert mock_popen.called
            args, kwargs = mock_popen.call_args
            cmd = args[0]
            env = kwargs.get("env", {})

            assert "--yolo" not in cmd
            assert "--accept-hooks" not in cmd
            assert cmd[0] == "sh"
            assert cmd[1] == "-c"
            assert "zf-runner" in cmd
            assert kwargs.get("start_new_session") is True
            assert env.get("HERMES_PROFILE") == "zf-reviewer"


def test_spawn_agent_worker_cleans_artifacts():
    """Verify spawn_agent_worker calls cleanup_process_artifacts and builds wrapper."""
    with (
        tempfile.TemporaryDirectory() as tmp_ws,
        patch("dispatcher.worker_spawner.cleanup_process_artifacts") as mock_cleanup,
        patch(
            "dispatcher.worker_spawner.build_shell_wrapper",
            wraps=lambda cmd, prefix: ["wrapped"] + cmd,
        ) as mock_wrap,
        patch("subprocess.Popen") as mock_popen,
        patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)
        mock_proc = MagicMock()
        mock_proc.pid = 1111
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        spawn_agent_worker(
            task_id="t-clean",
            title="Clean test",
            assignee="zf-builder",
            priority="P1",
            description="desc",
            workspace_path=tmp_ws,
            branch_name="task/t-clean",
        )

        mock_cleanup.assert_called_once_with("t-clean")
        assert mock_wrap.called
        args, _ = mock_popen.call_args
        assert args[0][0] == "wrapped"


def _spawn_builder_with_comment(body: str, task_id: str) -> str:
    """Spawn zf-builder against a temp DB holding one task_comments row; return the prompt."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "kanban.db"
        conn = sqlite3.connect(db_path)
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
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'zf-reviewer', ?, 1)",
            (task_id, body),
        )
        conn.commit()
        conn.close()

        import dispatcher

        with (
            patch.object(
                dispatcher,
                "check_unresolved_conflicts_safe",
                return_value=(True, [], ""),
            ),
            patch("subprocess.Popen") as mock_popen,
            patch.dict(os.environ, {"ZEROFACTORY_DB": str(db_path)}),
        ):
            os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)
            mock_proc = MagicMock()
            mock_proc.pid = 42424
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            pid, _sess = spawn_agent_worker(
                task_id=task_id,
                title="Fix Task",
                description="desc",
                priority="P1",
                assignee="zf-builder",
                workspace_path=os.getcwd(),
                branch_name=f"task/{task_id}",
            )

        assert pid == 42424
        return mock_popen.call_args[0][0][-1]


def test_spawn_builder_precommit_failure_goal():
    """A precommit-failure comment routes the builder to 'Fix Precommit Failures'."""
    prompt = _spawn_builder_with_comment(
        "[github pr] deterministic precommit failed: test suite failed",
        "zf-prefail",
    )
    assert "Your goal as Builder (Fix Precommit Failures)" in prompt
    assert "./.zerofactory/precommit.sh" in prompt
    assert "Your goal as Builder (Fix Review Comments)" not in prompt


def test_spawn_builder_review_comments_goal():
    """A non-precommit review comment routes the builder to 'Fix Review Comments'."""
    prompt = _spawn_builder_with_comment(
        "[github pr] add type hints to the public API",
        "zf-review",
    )
    assert "Your goal as Builder (Fix Review Comments)" in prompt
    assert "Your goal as Builder (Fix Precommit Failures)" not in prompt


def _spawn_builder_with_comments(
    comments: list[tuple[str, str]], task_id: str, metadata: dict | None = None
) -> str:
    """Spawn zf-builder against a temp DB holding ``comments`` rows; return the prompt."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "kanban.db"
        conn = sqlite3.connect(db_path)
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
        for n, (author, body) in enumerate(comments, 1):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, author, body, n),
            )
        conn.commit()
        conn.close()

        import dispatcher

        with (
            patch.object(
                dispatcher,
                "check_unresolved_conflicts_safe",
                return_value=(True, [], ""),
            ),
            patch("subprocess.Popen") as mock_popen,
            patch.dict(os.environ, {"ZEROFACTORY_DB": str(db_path)}),
        ):
            os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)
            mock_proc = MagicMock()
            mock_proc.pid = 42424
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            spawn_agent_worker(
                task_id=task_id,
                title="Fix Task",
                description="desc",
                priority="P1",
                assignee="zf-builder",
                workspace_path=os.getcwd(),
                branch_name=f"task/{task_id}",
                metadata=json.dumps(metadata or {}),
            )

        return mock_popen.call_args[0][0][-1]


PRIOR_FINDINGS = [
    (
        "zf-reviewer",
        "[github review] Round 1: rename `refresh()` to `reload()` and split the "
        "helper module; DEFERRED-FINDING-ONE.",
    ),
    (
        "zf-reviewer",
        "[github review] Round 2: add a retry wrapper around the network call; "
        "DEFERRED-FINDING-TWO.",
    ),
]


def _fix_round_metadata(verdict_body: str) -> dict:
    from dispatcher import review_cap_arbitration as arb

    return {
        arb.STATE_KEY: {
            "status": arb.STATUS_FIX_REQUESTED,
            "decision": "FIX",
            "attempts": 1,
            "decision_body": verdict_body,
        }
    }


def test_spawn_builder_arbitration_fix_round_prompt_is_the_verdict_only():
    """A FIX verdict is the builder's whole brief: prior findings it did not
    repeat are deferred and must not be presented as work to do."""
    from dispatcher import review_cap_arbitration as arb

    verdict = (
        "[AI:zf-gatekeeper] ARBITRATION VERDICT: FIX | REQUEST: arbitration|HEAD: x\n"
        "1. Race in `refresh()` when two cycles overlap - repro: run two dispatch "
    )
    prompt = _spawn_builder_with_comments(
        PRIOR_FINDINGS, "zf-arb-fix", _fix_round_metadata(verdict.rstrip() + " cycles.")
    )

    assert "Your goal as Builder (Arbitration Fix Round)" in prompt
    assert "ARBITRATION FIX ROUND - THE VERDICT BELOW IS YOUR ENTIRE SCOPE" in prompt
    # The verdict's own instructions are the brief, verbatim.
    assert "Race in `refresh()` when two cycles overlap" in prompt
    # Not the "address EVERY review comment" instruction, and none of the
    # findings the arbiter left out.
    assert "PULL REQUEST REVIEW COMMENTS TO ADDRESS" not in prompt
    assert "address every review comment" not in prompt
    assert "DEFERRED-FINDING-ONE" not in prompt
    assert "DEFERRED-FINDING-TWO" not in prompt
    assert "were DEFERRED by the arbiter" in prompt
    assert "Your goal as Builder (Fix Review Comments)" not in prompt
    assert "Your goal as Builder (Fix Precommit Failures)" not in prompt

    # Outside a live fix round (reviewer already answered the fix commit, or
    # a MERGE/ESCALATE outcome) the ordinary review-comment prompt is untouched.
    settled = _fix_round_metadata(verdict)
    settled[arb.STATE_KEY]["status"] = arb.STATUS_MERGE_APPROVED
    plain = _spawn_builder_with_comments(PRIOR_FINDINGS, "zf-arb-settled", settled)
    assert "Your goal as Builder (Fix Review Comments)" in plain
    assert "DEFERRED-FINDING-ONE" in plain
    assert "Arbitration Fix Round" not in plain

    answered = _fix_round_metadata(verdict)
    answered["review_round"] = 1
    re_reviewed = _spawn_builder_with_comments(
        PRIOR_FINDINGS, "zf-arb-answered", answered
    )
    assert "Your goal as Builder (Fix Review Comments)" in re_reviewed
    assert "Arbitration Fix Round" not in re_reviewed


def test_spawn_builder_arbitration_fix_round_keeps_deterministic_gate_failures():
    """A failing deterministic gate is not a deferred opinion: it stays in the prompt."""
    verdict = "[AI:zf-gatekeeper] ARBITRATION VERDICT: FIX | REQUEST: k | HEAD: x\nFix the lock leak."
    gate = (
        "zf-dispatcher",
        "[github pr] Deterministic precommit failed: ruff E501 in dispatcher/x.py",
    )
    prompt = _spawn_builder_with_comments(
        PRIOR_FINDINGS + [gate], "zf-arb-gate", _fix_round_metadata(verdict)
    )

    assert "Your goal as Builder (Arbitration Fix Round)" in prompt
    assert "Precommit failure output" in prompt
    assert "ruff E501 in dispatcher/x.py" in prompt
    assert "Fix the lock leak." in prompt
    assert "DEFERRED-FINDING-ONE" not in prompt


def test_spawn_agent_worker_orchestrator_grill_with_docs():
    """Verify zf-orchestrator spawns with Grill-with-Docs triage protocol prompt."""
    with (
        patch("subprocess.Popen") as mock_popen,
        patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pid, sess = spawn_agent_worker(
            task_id="zf-triage-1",
            title="[Triage] [Feature] Implement Webhook Support",
            assignee="zf-orchestrator",
            priority="P1",
            description="Add webhook dispatch on task complete",
            workspace_path="/tmp",
            branch_name="task/zf-triage-1",
        )

        assert pid == 12345
        assert mock_popen.called
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        prompt = cmd[-1]

        assert "Grill-with-Docs Triage Protocol" in prompt
        assert "stateless workers, stateful substrate" in prompt
        assert "### 🎯 Grill-with-Docs: Decision Required" in prompt
        assert "Option A:" in prompt
        assert kwargs.get("env", {}).get("HERMES_PROFILE") == "zf-orchestrator"


def test_spawn_agent_worker_with_repo_alias_and_architecture():
    """Verify repo_alias, architecture context, and dependency context are injected into prompt."""
    import dispatcher

    with (
        patch("subprocess.Popen") as mock_popen,
        patch.object(
            dispatcher,
            "digest_board_architecture_context",
            return_value="🌐 SYSTEM ARCHITECTURE & MULTI-REPO TOPOLOGY:\n- Active Target Repository: `api-gw`",
        ),
        patch.object(
            dispatcher,
            "digest_parent_and_peer_tasks_context",
            return_value="🔗 TASK DEPENDENCIES & RELATED FEATURES:\n- Upstream Completed Dependencies:\n  * #t-1 [common-lib]: Done",
        ),
        patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)
        mock_proc = MagicMock()
        mock_proc.pid = 54321
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pid, sess = spawn_agent_worker(
            task_id="t-99",
            title="Implement Gateway Route",
            assignee="zf-builder",
            priority="P1",
            description="Add new route",
            workspace_path="/tmp",
            branch_name="task/t-99",
            board_slug="my-board",
            repo_alias="api-gw",
        )

        assert pid == 54321
        assert mock_popen.called
        args, _ = mock_popen.call_args
        prompt = args[0][-1]

        assert "Repository: api-gw" in prompt
        assert "SYSTEM ARCHITECTURE & MULTI-REPO TOPOLOGY" in prompt
        assert "TASK DEPENDENCIES & RELATED FEATURES" in prompt


def test_spawn_agent_worker_refuses_without_valid_workspace_path():
    """Verify that zf-builder and zf-reviewer fail-closed if workspace_path is missing or nonexistent."""
    with (
        patch("subprocess.Popen") as mock_popen,
        patch.dict(os.environ, {"ZEROFACTORY_SKIP_GIT": ""}, clear=False),
    ):
        os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)

        # 1. zf-builder with None workspace_path
        pid, sess = spawn_agent_worker(
            task_id="t-fail-1",
            title="Fail Task",
            assignee="zf-builder",
            priority="P1",
            description="No ws",
            workspace_path=None,
            branch_name="task/t-fail-1",
        )
        assert pid is None
        assert sess is None
        assert not mock_popen.called

        # 2. zf-builder with nonexistent workspace_path
        pid, sess = spawn_agent_worker(
            task_id="t-fail-2",
            title="Fail Task",
            assignee="zf-builder",
            priority="P1",
            description="Bad ws",
            workspace_path="/path/does/not/exist/ever/12345",
            branch_name="task/t-fail-2",
        )
        assert pid is None
        assert sess is None
        assert not mock_popen.called

        # 3. zf-reviewer with None workspace_path
        pid, sess = spawn_agent_worker(
            task_id="t-fail-3",
            title="Fail Review",
            assignee="zf-reviewer",
            priority="P1",
            description="No ws",
            workspace_path=None,
            branch_name="task/t-fail-3",
        )
        assert pid is None
        assert sess is None
        assert not mock_popen.called
