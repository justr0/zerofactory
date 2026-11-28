"""Unit tests for dispatcher/worker_spawner.py worker spawning and session isolation."""

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

import dispatcher.process_manager as process_manager
import dispatcher.worker_spawner as worker_spawner
from dispatcher.worker_spawner import spawn_agent_worker
from dispatcher.worker_handoff import execution_token_line, token_from_prompt
import zf_wincompat
from zf_wincompat import detached_popen_kwargs


TRIAGE_COMMENT_HEADINGS = [
    ("zf-orchestrator", "### 💬 Previous Discussion & Human Responses:"),
    ("zf-goldfish", "### 💬 Previous Discussion & Revision History:"),
]


def _spawn_triage_prompt(
    tmp_path: Path,
    role: str,
    monkeypatch: pytest.MonkeyPatch,
    comments: list[tuple[str, str, int]] | None = None,
    *,
    create_comments_table: bool = True,
) -> tuple[str, MagicMock]:
    """Spawn a triage worker using an isolated comment database and return its prompt/log."""
    db_path = tmp_path / f"{role}.db"
    monkeypatch.delenv("ZEROFACTORY_WORKER_COMMAND", raising=False)
    monkeypatch.delenv("ZEROFACTORY_WORKER_ARGS", raising=False)
    with sqlite3.connect(db_path) as conn:
        if create_comments_table:
            conn.execute(
                "CREATE TABLE task_comments "
                "(task_id TEXT, author TEXT, body TEXT, created_at INTEGER)"
            )
            conn.executemany(
                "INSERT INTO task_comments VALUES ('task-context', ?, ?, ?)",
                comments or [],
            )
            conn.execute(
                "INSERT INTO task_comments VALUES ('other-task', 'human', ?, 5)",
                ("Unrelated task comment",),
            )

    digest = SimpleNamespace(
        digest_board_memories_context=lambda *args, **kwargs: "",
        digest_board_architecture_context=lambda *args, **kwargs: "",
        digest_parent_and_peer_tasks_context=lambda *args, **kwargs: "",
    )
    with (
        patch.object(worker_spawner, "_d", return_value=digest),
        patch.object(worker_spawner, "resolve_profile_state_db", return_value=None),
        patch.object(worker_spawner, "resolve_hermes_bin", return_value="hermes"),
        patch.object(
            worker_spawner, "is_unattended_approvals_enabled", return_value=False
        ),
        patch.object(worker_spawner, "build_minimal_child_env", return_value={}),
        patch.object(
            worker_spawner, "build_shell_wrapper", side_effect=lambda cmd, _prefix: cmd
        ),
        patch.object(worker_spawner, "cleanup_process_artifacts"),
        patch.object(worker_spawner._log, "debug") as debug_log,
        patch("paths.get_hermes_home", return_value=tmp_path / "hermes"),
        patch("subprocess.Popen") as mock_popen,
        patch.dict(os.environ, {"ZEROFACTORY_DB": str(db_path)}, clear=False),
        patch.dict(worker_spawner._active_workers),
    ):
        os.environ.pop("ZEROFACTORY_SKIP_WORKER_SPAWN", None)
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        spawn_agent_worker(
            task_id="task-context",
            title="Context task",
            description="Task description",
            priority="P1",
            assignee=role,
            workspace_path=str(tmp_path),
            branch_name="task/context",
            metadata=json.dumps(
                {"specification": "Draft specification", "revision_count": 1}
            ),
        )
        prompt = mock_popen.call_args.args[0][
            mock_popen.call_args.args[0].index("-q") + 1
        ]
    return prompt, debug_log


@pytest.mark.parametrize(("role", "heading"), TRIAGE_COMMENT_HEADINGS)
def test_triage_prompts_include_ordered_task_comments(
    tmp_path, monkeypatch, role, heading
):
    prompt, _ = _spawn_triage_prompt(
        tmp_path,
        role,
        monkeypatch,
        [
            ("human", "Later response", 20),
            ("zf-orchestrator", "Earlier question", 10),
        ],
    )

    expected = (
        f"{heading}\n"
        "### Comment #1 (by @zf-orchestrator):\nEarlier question\n\n"
        "### Comment #2 (by @human):\nLater response\n\n"
    )
    assert expected in prompt
    assert "Unrelated task comment" not in prompt


@pytest.mark.parametrize(("role", "heading"), TRIAGE_COMMENT_HEADINGS)
def test_triage_prompts_omit_context_for_empty_comment_set(
    tmp_path, monkeypatch, role, heading
):
    prompt, _ = _spawn_triage_prompt(tmp_path, role, monkeypatch)

    assert heading not in prompt
    # No worker_execution_id in the task metadata: the prompt goes out untouched.
    assert token_from_prompt(prompt) is None


@pytest.mark.parametrize(("role", "_heading"), TRIAGE_COMMENT_HEADINGS)
def test_triage_comment_lookup_failure_is_nonfatal_and_role_logged(
    tmp_path, monkeypatch, role, _heading
):
    prompt, debug_log = _spawn_triage_prompt(
        tmp_path, role, monkeypatch, create_comments_table=False
    )

    assert f"Assigned Role: {role}" in prompt
    debug_log.assert_any_call(
        f"Could not inspect task_comments for {role.removeprefix('zf-')} prompt: %s",
        ANY,
    )


def test_detached_popen_kwargs_platform_contract():
    """Verify both native detached-process options without requiring Windows."""
    with patch.object(zf_wincompat, "IS_WINDOWS", False):
        assert detached_popen_kwargs() == {"start_new_session": True}

    with (
        patch.object(zf_wincompat, "IS_WINDOWS", True),
        patch.object(zf_wincompat.subprocess, "CREATE_NO_WINDOW", 0x01, create=True),
        patch.object(
            zf_wincompat.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x02, create=True
        ),
    ):
        assert detached_popen_kwargs() == {"creationflags": 0x03}


def test_spawn_agent_worker_skip_env():
    """When ZEROFACTORY_SKIP_WORKER_SPAWN is set, spawn returns None immediately."""
    with patch.dict(os.environ, {"ZEROFACTORY_SKIP_WORKER_SPAWN": "1"}):
        pid, sess = spawn_agent_worker(
            "t1", "Title", "Desc", "P1", "zf-builder", "/tmp", "task/t1"
        )
        assert pid is None
        assert sess is None


def test_spawn_agent_worker_cmd_and_session_isolation(tmp_path):
    """Verify platform-specific argv, detach options, and session matching."""
    tmp_ws = tmp_path / "workspace"
    tmp_ws.mkdir()
    hermes_home = tmp_path / "hermes"

    for windows in (False, True):
        db_path = tmp_path / f"profile-state-{windows}.db"
        conn = sqlite3.connect(db_path)
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
            (str(tmp_ws), now),
        )
        conn.commit()
        conn.close()

        import dispatcher

        with (
            patch.object(zf_wincompat, "IS_WINDOWS", windows),
            patch.object(process_manager, "IS_WINDOWS", windows),
            patch.object(
                zf_wincompat.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
            ),
            patch.object(
                zf_wincompat.subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
                create=True,
            ),
            patch.object(dispatcher, "resolve_profile_state_db", return_value=db_path),
            patch(
                "dispatcher.worker_spawner.build_worker_command", return_value=None
            ) as mock_build_command,
            patch.object(
                worker_spawner, "resolve_hermes_bin", return_value="hermes-test"
            ),
            patch.object(
                worker_spawner, "is_unattended_approvals_enabled", return_value=False
            ),
            patch("paths.get_hermes_home", return_value=hermes_home),
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
                workspace_path=str(tmp_ws),
                branch_name="task/zf-mine",
                metadata=json.dumps({"worker_execution_id": "exec-current"}),
            )

            assert pid == 99999
            assert sess == "sess-mine"
            assert mock_popen.called
            args, kwargs = mock_popen.call_args
            prompt = mock_build_command.call_args.args[0]
            # The prompt is the other half of the handoff token (worker_handoff): a
            # worker's shell commands never see the env injected below.
            assert prompt.splitlines()[:2] == [
                "Handoff token for this dispatch: pass it as `--execution-id` to every "
                "`hermes zerofactory move` you run.",
                execution_token_line("exec-current"),
            ]
            native_cmd = [
                "hermes-test",
                "-p",
                "zf-reviewer",
                "--cli",
                "chat",
                "-q",
                prompt,
            ]
            exit_prefix = hermes_home / "logs" / "worker_zf-mine"
            cmd = args[0]
            if windows:
                assert cmd == [
                    sys.executable,
                    str(Path(worker_spawner.__file__).with_name("_worker_wrapper.py")),
                    str(exit_prefix),
                    *native_cmd,
                ]
                expected_isolation = {"creationflags": 0x08000200}
            else:
                expected_wrapper = process_manager.build_shell_wrapper(
                    native_cmd, exit_prefix
                )
                assert cmd == expected_wrapper
                assert cmd[:2] == ["sh", "-c"]
                assert cmd[3:5] == ["zf-runner", str(exit_prefix)]
                assert cmd[5:] == native_cmd
                expected_isolation = {"start_new_session": True}

            env = kwargs.get("env", {})

            isolation_kwargs = {
                key: kwargs[key]
                for key in ("start_new_session", "creationflags")
                if key in kwargs
            }
            assert isolation_kwargs == expected_isolation
            assert isolation_kwargs == detached_popen_kwargs()
            assert env.get("HERMES_PROFILE") == "zf-reviewer"
            assert env.get("ZEROFACTORY_WORKER_EXECUTION_ID") == "exec-current"


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


def test_spawn_builder_closes_the_board_connections_it_opens():
    """Every board connection the spawn path opens is closed by the time the
    spawn returns. `with sqlite3.connect(...)` commits but never closes, and an
    open handle keeps the board file from being deleted or replaced on Windows."""
    opened = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    with patch.object(sqlite3, "connect", recording_connect):
        # While connections leak, Windows refuses to delete the board file, so
        # the helper's cleanup raises first; let the assertions name the cause.
        with contextlib.suppress(PermissionError):
            _spawn_builder_with_comments(PRIOR_FINDINGS, "zf-close-probe", None)

    # The helper's own writer, plus at least one reader on the spawn path.
    assert len(opened) >= 2
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


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
