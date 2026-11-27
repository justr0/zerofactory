"""Reviewer prompt contract: the dispatcher owns the review-round budget.

A reviewer worker must be able to resolve every round statement on the PR from the
``Review Round`` header alone, and must have a verdict for a clean PR that does not
send the task back to the builder when a human has to authorise the merge.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from dispatcher.worker_spawner import spawn_agent_worker


def _reviewer_prompt(
    tmp_path, monkeypatch, *, automerge, metadata=None, max_rounds=None
):
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE boards (slug TEXT PRIMARY KEY, automerge INTEGER)")
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, tags TEXT, metadata TEXT, repo_alias TEXT)"
    )
    conn.execute(
        "INSERT INTO boards VALUES ('test-board', ?)", (1 if automerge else 0,)
    )
    conn.execute("INSERT INTO tasks VALUES ('t1', '[]', '{}', 'main-repo')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("ZEROFACTORY_DB", str(db_file))
    monkeypatch.delenv("ZEROFACTORY_SKIP_WORKER_SPAWN", raising=False)
    monkeypatch.setenv("ZEROFACTORY_SKIP_GIT", "1")
    if max_rounds is None:
        monkeypatch.delenv("ZEROFACTORY_MAX_REVIEW_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("ZEROFACTORY_MAX_REVIEW_ROUNDS", str(max_rounds))

    with (
        patch("subprocess.Popen") as mock_popen,
        patch("dispatcher.worker_spawner.resolve_profile_state_db", return_value=None),
        patch(
            "dispatcher.check_unresolved_conflicts_safe", return_value=(True, [], "")
        ),
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 1234
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        spawn_agent_worker(
            task_id="t1",
            title="Fix bug",
            description="Fixing something",
            priority="P1",
            assignee="zf-reviewer",
            workspace_path=str(tmp_path),
            branch_name="task/t1",
            board_slug="test-board",
            metadata=__import__("json").dumps(metadata or {}),
        )
    cmd = mock_popen.call_args[0][0]
    return cmd[cmd.index("-q") + 1]


def test_header_is_the_only_round_count(tmp_path, monkeypatch):
    """The prompt tells the reviewer that older round statements on the PR are superseded."""
    prompt = _reviewer_prompt(
        tmp_path, monkeypatch, automerge=False, metadata={"review_round": 1}
    )
    assert "Review Round: Round 2 of 2\n" in prompt
    assert (
        "The `Review Round` header above is the only round count that applies" in prompt
    )
    assert "superseded" in prompt


def test_default_budget_is_two_rounds(tmp_path, monkeypatch):
    """No env override: the header reports a budget of two rounds."""
    prompt = _reviewer_prompt(tmp_path, monkeypatch, automerge=False)
    assert "Review Round: Round 1 of 2\n" in prompt


def test_changes_requested_in_any_round(tmp_path, monkeypatch):
    """Defects are recorded as changes-requested in every round, the last one included."""
    prompt = _reviewer_prompt(tmp_path, monkeypatch, automerge=False)
    assert (
        "Defects remain (in ANY round, including Round 2): run `hermes zerofactory block t1 "
        "--reason 'changes-requested'`" in prompt
    )
    assert "Never hand a defect to a human yourself" in prompt


@pytest.mark.parametrize("automerge", [True, False])
def test_human_merge_authorization_verdict(tmp_path, monkeypatch, automerge):
    """A clean PR in a repo that requires human merge authorisation gets human-gate, not approved."""
    prompt = _reviewer_prompt(tmp_path, monkeypatch, automerge=automerge)
    assert "requires a human's explicit merge authorization" in prompt
    assert "hermes zerofactory block t1 --reason 'human-gate'" in prompt
    assert "No code changes requested." in prompt


def test_no_automerge_parks_a_clean_pr_for_human_merge(tmp_path, monkeypatch):
    """With auto-merge off, 'approved' no longer means the dispatcher will merge."""
    prompt = _reviewer_prompt(tmp_path, monkeypatch, automerge=False)
    assert "🚨 CRITICAL: AUTO-MERGE ACTIVE" not in prompt
    assert "Auto-Merge is ACTIVE" not in prompt
    assert (
        "run `hermes zerofactory block t1 --reason 'approved'` (the task will be assigned to "
        "human for review/merge" in prompt
    )


def test_automerge_on_announces_that_approval_merges(tmp_path, monkeypatch):
    """With auto-merge on, the reviewer is told its approval merges the PR."""
    prompt = _reviewer_prompt(tmp_path, monkeypatch, automerge=True)
    assert "🚨 CRITICAL: AUTO-MERGE ACTIVE" in prompt
    assert (
        "Auto-Merge is ACTIVE: the dispatcher will automatically merge this PR"
        in prompt
    )


def test_exactly_one_verdict_line_per_outcome(tmp_path, monkeypatch):
    """Three outcomes, three verdict lines; nothing else tells the reviewer to block."""
    prompt = _reviewer_prompt(tmp_path, monkeypatch, automerge=False)
    assert (
        prompt.count("hermes zerofactory block t1 --reason") == 4
    )  # header + 3 outcomes
    assert prompt.count("record ONE verdict") == 1
