"""Unit tests for dashboard/openwiki_service.py."""

import json
import subprocess
from pathlib import Path

from dashboard import openwiki_service
from dashboard.openwiki_service import (
    OPENWIKI_RELATIVE_DIR,
    build_openwiki_setup_task_prompt,
    check_board_openwiki_status,
    create_openwiki_setup_task,
    sync_board_openwiki_workspace,
)
from dashboard.plugin_api import BoardCreate, create_board


def test_build_openwiki_setup_task_prompt():
    prompt = build_openwiki_setup_task_prompt("test-board")
    assert OPENWIKI_RELATIVE_DIR in prompt
    assert "openwiki" in prompt
    assert "AGENTS.md" in prompt
    assert "test-board" in prompt
    # The wiki is managed via native OpenWiki MCP lifecycle tools in code mode.
    assert "OpenWiki MCP" in prompt
    assert "code mode" in prompt
    assert "openwiki_begin" in prompt
    assert "openwiki_submit_plan" in prompt
    assert "openwiki_next_page" in prompt
    assert "openwiki_submit_page" in prompt
    assert "openwiki_finish" in prompt
    assert "never manually" in prompt
    assert "manually create" in prompt
    # On lifecycle failure the worker must block, not improvise.
    assert "BLOCK the task" in prompt
    # Detection/verification keys off the machine-readable JSON ledger,
    # not the Markdown index.md.
    assert ".page-manifest.json" in prompt
    assert "machine-readable JSON" in prompt
    # The prompt grounds verification in standard artifacts: the
    # machine-readable JSON page ledger plus the canonical quickstart.md
    # entry point, and explicitly bars keying off the Markdown index.md.
    assert "openwiki/quickstart.md" in prompt
    assert "standard" in prompt
    assert "Do NOT key off" in prompt
    assert "index.md" in prompt  # only inside the "Do NOT key off" prohibition


def test_build_openwiki_setup_task_prompt_multi_repo():
    siblings = [
        {"alias": "zerohub-shared", "path": "/path/to/zerohub-shared"},
        {"alias": "zerohub-worker", "path": "/path/to/zerohub-worker"},
    ]
    prompt = build_openwiki_setup_task_prompt(
        "test-board",
        repo_alias="zerohub",
        sibling_repos=siblings,
    )
    assert "Multi-Repository Workspace Integration" in prompt
    assert "zerohub-shared" in prompt
    assert "zerohub-worker" in prompt
    assert "Cross-Repo Linking & Dependencies" in prompt
    assert "wiki:<sibling-repo-alias>/openwiki/..." in prompt
    assert "wiki-workspaces.json" not in prompt  # internal detail kept clean


def test_openwiki_status_missing_board(initialized_db: Path):
    res = check_board_openwiki_status("nonexistent-board")
    assert res["ok"] is False
    assert res["has_openwiki"] is False


def test_openwiki_status_and_task_lifecycle(initialized_db: Path, tmp_path: Path):
    # Create a dummy repo with git
    repo_dir = tmp_path / "dummy_repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    # Create board pointing to this local directory
    b_res = create_board(
        BoardCreate(
            git_url=str(repo_dir),
            description="Local dummy repo",
            auto_setup_precommit=False,
        )
    )
    slug = b_res["slug"]

    # Initial check: no openwiki directory
    status = check_board_openwiki_status(slug)
    assert status["ok"] is True
    assert status["has_openwiki"] is False
    assert status["pending_task_id"] is None

    # Trigger openwiki setup task
    setup_res = create_openwiki_setup_task(slug)
    assert setup_res["ok"] is True
    assert setup_res["already_exists"] is False
    task_id = setup_res["task_id"]
    assert task_id is not None

    from dashboard.plugin_api import get_task
    from dispatcher.github_pr import is_task_automerge_enabled

    created_task = get_task(task_id)["task"]
    assert "automerge" in created_task["tags"]
    assert is_task_automerge_enabled(created_task, {"automerge": False}) is True

    # Check status again: pending task should now be detected
    status2 = check_board_openwiki_status(slug)
    assert status2["has_openwiki"] is False
    assert status2["pending_task_id"] == task_id
    assert status2["pending_task_status"] == "todo"

    # Calling create_openwiki_setup_task again should deduplicate
    setup_res2 = create_openwiki_setup_task(slug)
    assert setup_res2["ok"] is True
    assert setup_res2["already_exists"] is True
    assert setup_res2["task_id"] == task_id

    # Create openwiki/ directory with the CLI's machine-readable JSON ledger
    ow_dir = repo_dir / "openwiki"
    ow_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = ow_dir / ".page-manifest.json"
    manifest_file.write_text('{"schemaVersion": 1, "pages": {}}', encoding="utf-8")

    # Status check should now detect has_openwiki = True
    status3 = check_board_openwiki_status(slug)
    assert status3["has_openwiki"] is True
    assert status3["openwiki_path"] == str(ow_dir)
    # Preview is sourced from the JSON ledger, not the Markdown index.md
    assert "schemaVersion" in status3["wiki_index_preview"]


def test_openwiki_status_check_does_not_auto_clone(initialized_db: Path, monkeypatch):
    """Read-only openwiki status check must never spawn a git clone subprocess."""
    import shutil

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=b"", stderr=b""
        )

    # Clean up any stale directory a previous (unfixed) run may have created,
    # so the end-of-test home-dir assertion stays hermetic.
    stale = Path.home() / "git" / "no-clone-org"
    if stale.exists():
        shutil.rmtree(stale, ignore_errors=True)

    # Create the board while conftest's ZEROFACTORY_SKIP_GIT is still set so
    # board creation itself does not enter the auto-clone branch.
    b_res = create_board(
        BoardCreate(
            git_url="https://github.com/no-clone-org/no-clone-repo-ow.git",
            description="Remote board without a local clone",
            auto_setup_precommit=False,
        )
    )
    slug = b_res["slug"]

    # Exercise the read-only status check with auto-clone fully enabled.
    monkeypatch.delenv("ZEROFACTORY_SKIP_GIT", raising=False)
    monkeypatch.delenv("ZEROFACTORY_AUTO_CLONE", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)

    res = check_board_openwiki_status(slug)

    assert res["ok"] is True
    assert res["has_openwiki"] is False
    clones = [c for c in calls if len(c) >= 2 and c[1] == "clone"]
    assert not clones, f"status check triggered git clone: {clones}"
    assert not (Path.home() / "git" / "no-clone-org").exists()


def test_openwiki_workspace_ids_do_not_collide_for_normalized_board_slugs(
    tmp_path: Path, monkeypatch
):
    openwiki_home = tmp_path / "openwiki-home"
    monkeypatch.setattr(
        openwiki_service, "get_openwiki_home_dir", lambda: openwiki_home
    )
    repos = {}
    rows = {}
    for board, alias in (("ops_team", "team-repo"), ("ops-team", "service-repo")):
        repo = tmp_path / board
        repo.mkdir()
        repos[board] = [{"path": repo, "alias": alias}]
        rows[board] = [{"repo_alias": alias, "git_url": f"https://example/{alias}.git"}]
    monkeypatch.setattr(
        openwiki_service,
        "_get_board_repository_rows",
        lambda board_slug, db_path=None: rows[board_slug],
    )
    monkeypatch.setattr(
        openwiki_service,
        "_resolve_board_repo_path",
        lambda board_slug, repo: repos[board_slug][0]["path"],
    )

    team_result = sync_board_openwiki_workspace("ops_team")
    service_result = sync_board_openwiki_workspace("ops-team")
    repeated_result = sync_board_openwiki_workspace("ops_team")

    assert team_result["workspace_id"] != service_result["workspace_id"]
    assert repeated_result["workspace_id"] == team_result["workspace_id"]
    registry = json.loads((openwiki_home / "wiki-workspaces.json").read_text())
    workspaces = {workspace["name"]: workspace for workspace in registry["workspaces"]}
    assert set(workspaces) == {"ops_team", "ops-team"}
    team_wikis = {
        wiki["id"] for wiki in registry["wikis"] if wiki["name"] == "team-repo"
    }
    service_wikis = {
        wiki["id"] for wiki in registry["wikis"] if wiki["name"] == "service-repo"
    }
    assert workspaces["ops_team"]["wikis"] == list(team_wikis)
    assert workspaces["ops-team"]["wikis"] == list(service_wikis)
    assert not (team_wikis & service_wikis)
    active_workspace = {
        entry["wiki"]: entry["workspace"] for entry in registry["active"]
    }
    assert {active_workspace[wiki] for wiki in team_wikis} == {
        team_result["workspace_id"]
    }
    assert {active_workspace[wiki] for wiki in service_wikis} == {
        service_result["workspace_id"]
    }
    assert len(registry["workspaces"]) == 2
    assert len(registry["wikis"]) == 2
    assert len(registry["active"]) == 2
