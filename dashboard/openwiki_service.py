"""Zero Factory OpenWiki Service — Status detection, prompt generation, and setup task management."""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
_DASHBOARD_ROOT = str(Path(__file__).resolve().parent)
if _DASHBOARD_ROOT not in sys.path:
    sys.path.insert(0, _DASHBOARD_ROOT)

try:
    from .setup_common import (
        check_board_setup_status,
        create_setup_task,
        get_repo_resolver,  # noqa: F401  (re-exported for API compatibility)
    )
except (ImportError, ValueError):
    from setup_common import (  # type: ignore
        check_board_setup_status,
        create_setup_task,
        get_repo_resolver,  # noqa: F401  (re-exported for API compatibility)
    )

OPENWIKI_RELATIVE_DIR = "openwiki"
# The openwiki CLI's canonical machine-readable artifact (its PAGE_MANIFEST_PATH
# constant). Detection and preview key off this JSON ledger, not the Markdown
# index.md the CLI also writes, so status reflects what the CLI actually produced.
OPENWIKI_PAGE_MANIFEST_RELPATH = f"{OPENWIKI_RELATIVE_DIR}/.page-manifest.json"
SETUP_OPENWIKI_TASK_TITLE = (
    "chore(repo): setup OpenWiki machine-readable agent documentation"
)
SETUP_OPENWIKI_TASK_DEDUP_KEY = "setup:openwiki"
SETUP_OPENWIKI_TASK_TITLE_PREFIX = "chore(repo): setup OpenWiki"


def _get_board_repository_rows(
    board_slug: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Read authoritative board membership rows; propagate lookup failures."""
    if not board_slug:
        return []
    import sqlite3

    try:
        from .db import get_db_path
    except (ImportError, ValueError):
        from db import get_db_path  # type: ignore

    eff_db = Path(db_path or os.environ.get("ZEROFACTORY_DB") or get_db_path())
    if not eff_db.exists():
        raise FileNotFoundError(f"Zero Factory database not found: {eff_db}")

    with closing(sqlite3.connect(str(eff_db), timeout=5.0)) as conn, conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, repo_alias, git_url, target_branch FROM board_repositories WHERE board_slug = ? ORDER BY id ASC",
            (board_slug,),
        ).fetchall()
    return [dict(row) for row in rows]


def _resolve_board_repo_path(board_slug: str, repo: dict[str, Any]) -> Path | None:
    """Resolve a linked repository locally without changing its DB membership."""
    resolver = get_repo_resolver()
    if resolver:
        try:
            resolved = resolver(
                {"git_url": repo["git_url"], "slug": repo["repo_alias"]}
            )
            if resolved and resolved.is_dir() and (resolved / ".git").exists():
                return resolved.resolve()
        except Exception:
            pass

    try:
        from paths import get_board_repos_dir  # type: ignore
    except Exception:
        try:
            from ..paths import get_board_repos_dir  # type: ignore
        except Exception:
            get_board_repos_dir = lambda b: (  # type: ignore
                Path.home() / ".zerofactory" / "workspaces" / b / "repos"
            )
    board_repo = get_board_repos_dir(board_slug) / repo["repo_alias"]
    if board_repo.is_dir() and (board_repo / ".git").exists():
        return board_repo.resolve()
    return None


def get_board_linked_repos(
    board_slug: str, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Retrieve locally resolved repositories belonging to a board."""
    try:
        rows = _get_board_repository_rows(board_slug, db_path=db_path)
    except Exception:
        return []

    linked_repos = []
    for repo in rows:
        repo_path = _resolve_board_repo_path(board_slug, repo)
        if repo_path:
            linked_repos.append(
                {"alias": repo["repo_alias"], "path": repo_path, "url": repo["git_url"]}
            )
    return linked_repos


def build_openwiki_setup_task_prompt(
    board_slug: str,
    repo_path: Path | None = None,
    repo_alias: str | None = None,
    sibling_repos: list[dict[str, Any]] | None = None,
) -> str:
    """Generate structured instructions for zf-builder to generate OpenWiki agent docs via OpenWiki MCP in repository code mode."""
    path_hint = f" (`{repo_path}`)" if repo_path else ""
    target_desc = f"repository `{repo_alias}`" if repo_alias else "this repository"

    # Discover sibling repositories if not explicitly passed
    if sibling_repos is None and board_slug:
        all_board_repos = get_board_linked_repos(board_slug)
        sibling_repos = [
            r for r in all_board_repos if not repo_alias or r.get("alias") != repo_alias
        ]

    multi_repo_section = ""
    if sibling_repos:
        sibling_lines = "\n".join(
            f"   - Sibling repo `{s.get('alias')}` (accessible at relative path `../{s.get('alias')}/`)"
            for s in sibling_repos
        )
        multi_repo_section = f"""
## Multi-Repository Workspace Integration
This repository is linked to a shared OpenWiki workspace (`{board_slug}`) alongside sibling repositories:
{sibling_lines}

- **Cross-Repo Linking & Dependencies**:
  - The board's OpenWiki workspace registry links these repositories together.
  - In your documentation taxonomy (especially in `/openwiki/architecture.md` or a dedicated `/openwiki/architecture/dependencies.md`), explicitly document how this repository interacts with its sibling repositories (shared contracts, schemas, API endpoints, or shared client libraries).
  - Use qualified OpenWiki cross-workspace references where applicable (e.g., `wiki:<sibling-repo-alias>/openwiki/...`).
  - Read sibling code at `../<sibling-repo-alias>/` as needed to accurately verify inter-service integration points and contracts.
"""

    return f"""Set up OpenWiki machine-readable agent documentation for {target_desc}{path_hint} on board `{board_slug}` in repository code mode.

## Target
Directory: `{OPENWIKI_RELATIVE_DIR}/` (managed via OpenWiki MCP in repository code mode)
Pointer: `AGENTS.md` (managed via OpenWiki)

## Goal
Generate a high-signal, machine-readable architectural knowledge base in `{OPENWIKI_RELATIVE_DIR}/` (Docs for Agents pattern) using the **OpenWiki MCP server** (in repository code mode) and its standard artifacts, so agents consume the wiki instead of re-reading source. This optimizes context windows, slashes exploratory token consumption by 30–40%, and prevents multi-agent hallucination across Zero Factory workers (`zf-builder`, `zf-reviewer`, `zf-orchestrator`).
{multi_repo_section}
## Instructions
1. **OpenWiki MCP Server & Skill (Repository Code Mode)**:
   - The `openwiki` MCP server is pre-configured in your `zf-builder` profile.
   - The MCP server operates exclusively in repository **code mode** (targeting `{OPENWIKI_RELATIVE_DIR}/` in the repository root, never personal mode).
   - It exposes native lifecycle tools: `openwiki_begin`, `openwiki_submit_plan`, `openwiki_next_page`, `openwiki_submit_page`, and `openwiki_finish`.
   - **Zero API keys or environment variables (`export OPENWIKI_*`) are needed!** OpenWiki runs purely as a local MCP tool provider over stdio, and Hermes provides the model intelligence.
   - Review your `openwiki` skill (`skills/openwiki/SKILL.md`) for detailed guidance on claims, taxonomy, and frontmatter.

2. **Generate the wiki via OpenWiki MCP Lifecycle — never manually**:
   - **CRITICAL: the wiki MUST be generated and finalized through the OpenWiki MCP lifecycle in repository code mode. Do NOT hand-write, hand-structure, or manually create `{OPENWIKI_RELATIVE_DIR}/` or `AGENTS.md`.** If MCP tools cannot complete a run in this environment, BLOCK the task (`hermes zerofactory block <task_id> --reason "<MCP error>"`) with the captured error output instead of falling back to manual wiki authoring.
   - **Step 1: Begin Run (Code Mode)**:
     Call `openwiki_begin({{"root": ".", "mode": "init"}})` (or `"mode": "update"` for updates).
     This runs the repository code documentation lifecycle for the target repository.
     If it returns `status: "noop"`, report that the wiki is up-to-date and complete the task.
   - **Step 2: Submit Plan**:
     Inspect repository manifests, entrypoints, and public interfaces to plan a logical documentation taxonomy. Keep the initial taxonomy focused and consolidated (4–6 pages, e.g. quickstart, architecture, subsystems, operations, conventions). Call `openwiki_submit_plan` with canonical page paths (always include `/openwiki/quickstart.md` for `init`).
   - **Step 3: Page Loop**:
     Repeatedly call `openwiki_next_page`. For each assigned page job:
     - Research that specific page topic using native repository tools.
     - Author the Markdown page with valid OKF frontmatter (`type`, `title`, `description`, `tags`).
     - Call `openwiki_submit_page` with your page decisions.
   - **Step 4: Finish Run**:
     When `openwiki_next_page` returns `status: "complete"`, call `openwiki_finish`. This finalizes the run, writes the machine-readable ledger at `{OPENWIKI_PAGE_MANIFEST_RELPATH}`, stamps `.last-update.json`, and links `AGENTS.md`.

3. **Verify Standard Artifacts on Disk & Clean Up**:
   - `{OPENWIKI_PAGE_MANIFEST_RELPATH}` — the machine-readable JSON page ledger (its `PAGE_MANIFEST_PATH` constant). Confirm it exists and parses as valid JSON with shape `{{"schemaVersion": 1, "pages": {{...}}}}`.
   - `{OPENWIKI_RELATIVE_DIR}/quickstart.md` — the canonical entry point, which must exist and must never be deleted.
   - Do NOT key off `{OPENWIKI_RELATIVE_DIR}/index.md` or hand-write any Markdown page in `{OPENWIKI_RELATIVE_DIR}/` — the machine-readable JSON ledger is the source of truth.
   - `AGENTS.md` — confirm the managed block (`<!-- OPENWIKI:START --> ... <!-- OPENWIKI:END -->`) points at `{OPENWIKI_RELATIVE_DIR}/quickstart.md`.
   - **Do NOT commit `.github/workflows/openwiki-update.yml` or `CLAUDE.md`**: Zero Factory manages wiki updates natively via Hermes background cron, NOT external GitHub Actions. If OpenWiki's `init` created them, delete them (`rm -rf .github CLAUDE.md`).

4. **Verify Precommit Checks**:
   - Run `./.zerofactory/precommit.sh` (or `git status`) in the repository to verify that format, build, and test checks execute cleanly.
   - Re-confirm the standard artifacts are present and well-formed: the machine-readable JSON ledger at `{OPENWIKI_PAGE_MANIFEST_RELPATH}` and canonical `{OPENWIKI_RELATIVE_DIR}/quickstart.md`.

5. **Complete Task**:
   - Mark task done via `hermes zerofactory move <task_id> done`.
   - (NOTE: Do NOT run git add/commit/push manually. The dispatcher automatically verifies precommit and stages/commits/opens PR upon task completion).
"""


def check_board_openwiki_status(
    board_slug: str, repo_alias: str | None = None
) -> dict[str, Any]:
    """Check if openwiki/ exists for a repository and check active setup task status."""
    return check_board_setup_status(
        board_slug,
        repo_alias=repo_alias,
        has_key="has_openwiki",
        path_key="openwiki_path",
        preview_key="wiki_index_preview",
        target_relpath=OPENWIKI_RELATIVE_DIR,
        title_prefix=SETUP_OPENWIKI_TASK_TITLE_PREFIX,
        dedup_substring=SETUP_OPENWIKI_TASK_DEDUP_KEY,
        preview_relpath=OPENWIKI_PAGE_MANIFEST_RELPATH,
        target_is_dir=True,
    )


def create_openwiki_setup_task(
    board_slug: str, repo_alias: str | None = None, actor: str = "user"
) -> dict[str, Any]:
    """Create or return an existing setup task to generate openwiki/ documentation for a repository."""
    res = create_setup_task(
        board_slug,
        repo_alias=repo_alias,
        status_checker=check_board_openwiki_status,
        title=SETUP_OPENWIKI_TASK_TITLE,
        prompt_builder=build_openwiki_setup_task_prompt,
        files=[
            OPENWIKI_RELATIVE_DIR,
            OPENWIKI_PAGE_MANIFEST_RELPATH,
            "AGENTS.md",
        ],
        dedup_key=SETUP_OPENWIKI_TASK_DEDUP_KEY,
        progress_label="OpenWiki",
        created_label="OpenWiki",
        actor=actor,
        tags=["automerge"],
    )
    # Automatically sync the board's OpenWiki workspace registry so sibling repos are linked
    try:
        sync_board_openwiki_workspace(board_slug)
    except Exception:
        pass
    return res


WIKI_WORKSPACES_FILE = "wiki-workspaces.json"


def get_openwiki_home_dir() -> Path:
    """Resolve the base directory for OpenWiki user configuration and registries (~/.openwiki)."""
    env_dir = os.environ.get("OPENWIKI_CONFIG_DIR")
    if env_dir and env_dir.strip():
        p = env_dir.strip()
        if p.startswith("~"):
            return Path(os.path.expanduser(p))
        return Path(p).resolve()
    return Path.home() / ".openwiki"


def sync_board_openwiki_workspace(
    board_slug: str, db_path: str | Path | None = None
) -> dict[str, Any]:
    """Sync repositories linked to board_slug into a named OpenWiki workspace (~/.openwiki/wiki-workspaces.json).

    This enables OpenWiki's workspace linking feature so openwiki_search and openwiki_read
    can query architecture and contracts across all sibling repositories on the board.
    """
    if not board_slug:
        return {"status": "skipped", "reason": "No board slug provided"}

    import json
    import hashlib
    import os
    import re
    import tempfile

    try:
        repository_rows = _get_board_repository_rows(board_slug, db_path=db_path)
    except Exception as e:
        return {"status": "error", "error": f"Failed to read board repositories: {e}"}

    linked_repos = []
    unresolved_repos = []
    for repo in repository_rows:
        path = _resolve_board_repo_path(board_slug, repo)
        if path:
            linked_repos.append(
                {
                    "id": repo.get("id"),
                    "alias": repo["repo_alias"],
                    "path": path,
                    "url": repo["git_url"],
                }
            )
        else:
            unresolved_repos.append(repo)

    openwiki_dir = get_openwiki_home_dir()
    registry_file = openwiki_dir / WIKI_WORKSPACES_FILE

    if not registry_file.exists() and not repository_rows:
        return {"status": "removed", "board_slug": board_slug, "member_wikis": []}

    registry: dict[str, Any] = {
        "version": 1,
        "wikis": [],
        "workspaces": [],
        "active": [],
    }

    if registry_file.exists():
        try:
            loaded = json.loads(registry_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {"status": "error", "error": f"Failed to read registry: {e}"}
        if not isinstance(loaded, dict) or loaded.get("version") != 1:
            return {"status": "error", "error": "Unsupported OpenWiki registry format"}
        registry = loaded

    existing_wikis: list[dict[str, Any]] = registry.setdefault("wikis", [])
    existing_workspaces: list[dict[str, Any]] = registry.setdefault("workspaces", [])
    existing_active: list[dict[str, Any]] = registry.setdefault("active", [])
    if not all(
        isinstance(items, list)
        for items in (existing_wikis, existing_workspaces, existing_active)
    ):
        return {"status": "error", "error": "Invalid OpenWiki registry entries"}

    def _slugify(val: str, prefix: str = "item") -> str:
        s = re.sub(r"[^a-z0-9-]", "-", val.lower().strip()).strip("-")
        s = re.sub(r"-+", "-", s)
        return s[:63] if s else prefix

    # The collision-resistant ID is the ownership boundary. Display names and
    # legacy slug IDs are user-controlled and cannot prove board ownership.
    ws_id = f"{_slugify(board_slug, 'workspace')[:63]}-{hashlib.sha256(board_slug.encode()).hexdigest()}"
    matching_workspaces = [ws for ws in existing_workspaces if ws.get("id") == ws_id]
    target_workspace_ids = {ws_id}
    target_ws = next((ws for ws in matching_workspaces if ws.get("id") == ws_id), None)
    if target_ws is None and matching_workspaces:
        target_ws = matching_workspaces[0]
    old_member_ids = {
        wiki_id
        for ws in matching_workspaces
        for wiki_id in ws.get("wikis", [])
        if isinstance(wiki_id, str)
    }

    def _repository_key(repo: dict[str, Any]) -> str:
        repo_id = repo.get("id")
        return f"id:{repo_id}" if repo_id is not None else f"alias:{repo['repo_alias']}"

    repository_wikis = (
        target_ws.get("repository_wikis", {})
        if target_ws and isinstance(target_ws.get("repository_wikis", {}), dict)
        else {}
    )
    current_repository_keys = {_repository_key(repo) for repo in repository_rows}
    repo_wiki_ids = {
        key: wiki_id
        for key, wiki_id in repository_wikis.items()
        if key in current_repository_keys
        and isinstance(wiki_id, str)
        and wiki_id in old_member_ids
    }

    # 1. Register or update resolved wikis, preserving a stable row-to-wiki
    # identity for linked repositories whose local checkout is unavailable.
    root_to_wiki_id: dict[str, str] = {}
    used_wiki_ids: set[str] = {w.get("id") for w in existing_wikis if w.get("id")}

    for w in existing_wikis:
        if w.get("root"):
            root_to_wiki_id[str(Path(w["root"]).resolve())] = w["id"]

    for lr in linked_repos:
        r_str = str(lr["path"])
        wiki_id = root_to_wiki_id.get(r_str)
        if wiki_id is None:
            base_id = _slugify(lr["alias"], "wiki")
            wiki_id = base_id
            counter = 1
            while wiki_id in used_wiki_ids:
                wiki_id = f"{base_id}-{counter}"
                counter += 1
            used_wiki_ids.add(wiki_id)
            existing_wikis.append(
                {
                    "id": wiki_id,
                    "name": lr["alias"],
                    "root": r_str,
                }
            )
            root_to_wiki_id[r_str] = wiki_id
        repo_wiki_ids[_repository_key({"id": lr["id"], "repo_alias": lr["alias"]})] = (
            wiki_id
        )

    # Keep prior membership for linked repositories whose local checkout is
    # unavailable. Their persisted repository identity prevents aliases of the
    # same checkout from being confused with detached repositories.
    unresolved_member_ids = set()
    unresolved_unmatched = False
    for repo in unresolved_repos:
        repo_key = _repository_key(repo)
        if repo_key in repo_wiki_ids:
            continue
        matching_ids = []
        for wiki in existing_wikis:
            wiki_id = wiki.get("id")
            if (
                isinstance(wiki_id, str)
                and wiki_id in old_member_ids
                and (
                    wiki.get("name") == repo["repo_alias"]
                    or (
                        wiki.get("root")
                        and Path(wiki["root"]).name == repo["repo_alias"]
                    )
                )
            ):
                matching_ids.append(wiki_id)
        if matching_ids:
            repo_wiki_ids[repo_key] = matching_ids[0]
        else:
            unresolved_unmatched = True
    if unresolved_unmatched:
        unresolved_member_ids.update(old_member_ids)
    member_wiki_ids = list(
        dict.fromkeys(
            [
                *(
                    repo_wiki_ids[key]
                    for key in map(_repository_key, repository_rows)
                    if key in repo_wiki_ids
                ),
                *unresolved_member_ids,
            ]
        )
    )

    # 2. Reconcile board workspace membership, leaving same-name custom
    # workspaces untouched.
    if not repository_rows:
        existing_workspaces[:] = [
            ws for ws in existing_workspaces if ws.get("id") not in target_workspace_ids
        ]
    elif target_ws:
        target_ws["id"] = ws_id
        target_ws["wikis"] = member_wiki_ids
        target_ws["repository_wikis"] = {
            key: repo_wiki_ids[key]
            for key in map(_repository_key, repository_rows)
            if key in repo_wiki_ids
        }
        target_ws["name"] = board_slug
        existing_workspaces[:] = [
            ws
            for ws in existing_workspaces
            if ws is target_ws or ws.get("id") not in target_workspace_ids
        ]
    else:
        existing_workspaces.append(
            {
                "id": ws_id,
                "name": board_slug,
                "wikis": member_wiki_ids,
                "repository_wikis": {
                    key: repo_wiki_ids[key]
                    for key in map(_repository_key, repository_rows)
                    if key in repo_wiki_ids
                },
            }
        )

    def _replacement_workspace(wiki_id: str) -> dict[str, Any] | None:
        return next(
            (
                ws
                for ws in existing_workspaces
                if ws.get("id") not in target_workspace_ids
                and wiki_id in ws.get("wikis", [])
            ),
            None,
        )

    workspace_by_id = {ws.get("id"): ws for ws in existing_workspaces}
    active_after_reconcile = []
    for act in existing_active:
        wiki_id = act.get("wiki")
        selected_id = act.get("workspace")
        if not isinstance(wiki_id, str):
            continue
        if selected_id in target_workspace_ids:
            if wiki_id in member_wiki_ids and repository_rows:
                act["workspace"] = ws_id
            else:
                replacement = _replacement_workspace(wiki_id)
                if replacement is None:
                    continue
                act["workspace"] = replacement["id"]
        else:
            selected_workspace = workspace_by_id.get(selected_id)
            if selected_workspace and wiki_id in selected_workspace.get("wikis", []):
                active_after_reconcile.append(act)
                continue
            if wiki_id in member_wiki_ids and repository_rows:
                act["workspace"] = ws_id
            else:
                replacement = _replacement_workspace(wiki_id)
                if replacement is None:
                    continue
                act["workspace"] = replacement["id"]
        active_after_reconcile.append(act)
    existing_active[:] = active_after_reconcile

    # 3. Add active selections only for current members that have none.
    active_wiki_ids = {act.get("wiki") for act in existing_active}
    if repository_rows:
        for wiki_id in member_wiki_ids:
            if wiki_id not in active_wiki_ids:
                existing_active.append({"wiki": wiki_id, "workspace": ws_id})
                active_wiki_ids.add(wiki_id)

    # 4. Atomic write
    try:
        openwiki_dir.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=str(openwiki_dir), prefix="workspaces_", suffix=".tmp"
        )
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, str(registry_file))
        return {
            "status": "synced" if repository_rows else "removed",
            "workspace_id": ws_id,
            "board_slug": board_slug,
            "member_wikis": member_wiki_ids,
        }
    except Exception as e:
        return {"status": "error", "error": f"Failed to persist registry: {e}"}
