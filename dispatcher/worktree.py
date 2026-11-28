"""Git worktree provisioning, teardown, and merge conflict routing for Zero Factory."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import (
    _REMOTE_BRANCH_DELETE_TIMEOUT,
    _WORKTREE_ADD_TIMEOUT,
    _WORKTREE_REMOVE_TIMEOUT,
    VALID_PROFILES,
    StepTracker,
    _d,
    _log,
    normalize_assignee,
)
from .git_ops import git_subprocess_env
from .state_machine import transition_task
from paths import resolve_local_git_checkout


def _find_checked_out_worktree(repo_path: Path, branch_name: str) -> Path | None:
    """Check git worktree list to find if a branch is already checked out."""
    if not repo_path or not repo_path.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        cur_wt = None
        target_ref = f"refs/heads/{branch_name}"
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("worktree "):
                cur_wt = line[len("worktree ") :].strip()
            elif line.startswith("branch "):
                ref = line[len("branch ") :].strip()
                if ref == target_ref or ref == branch_name:
                    if cur_wt:
                        p = Path(cur_wt)
                        if p.exists() and (p / ".git").exists():
                            return p
    except Exception:
        pass
    return None


def _validate_worktree_component(value: str, name: str) -> str:
    """Reject values that could escape a single worktree path component."""
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or PureWindowsPath(value).drive
    ):
        raise ValueError(f"{name} must be a single safe path component")
    return value


def _validate_worktree_destination(
    path: Path, task_root: Path, worktree_base: Path
) -> Path:
    """Resolve a worktree path and require it to remain in its task root."""
    resolved_base = worktree_base.resolve()
    expected_task_root = resolved_base / task_root.name
    resolved_path = path.resolve()
    if not expected_task_root.is_relative_to(
        resolved_base
    ) or not resolved_path.is_relative_to(expected_task_root):
        raise ValueError(
            "resolved worktree destination must remain beneath its task root"
        )
    return resolved_path


def _get_board_repos_dir(board_slug: str) -> Path:
    """Return a board's repository directory across package and flat imports."""
    try:
        from paths import get_board_repos_dir  # type: ignore
    except Exception:
        try:
            from ..paths import get_board_repos_dir  # type: ignore
        except Exception:
            return Path.home() / ".zerofactory" / "workspaces" / board_slug / "repos"
    return get_board_repos_dir(board_slug)


def resolve_board_repo_path(board: dict[str, Any]) -> Path | None:
    """Resolve the local repository path for a given Kanban board."""
    slug = (board.get("slug") or "").strip()
    git_url = (board.get("git_url") or "").strip()
    repo_alias = (board.get("repo_alias") or "").strip()
    if not git_url and board.get("repositories"):
        git_url = (board["repositories"][0].get("git_url") or "").strip()
        if not repo_alias and board["repositories"][0].get("repo_alias"):
            repo_alias = (board["repositories"][0].get("repo_alias") or "").strip()
    if not git_url and board.get("description"):
        match = re.search(r"(?:https?://|git@)[^\s)]+", board["description"])
        if match:
            git_url = match.group(0)

    repo = ""
    if git_url:
        from paths import repository_name_from_git_url

        repo = repository_name_from_git_url(git_url)
    elif "-" in slug:
        parts = slug.split("-", 1)
        repo = parts[1]

    candidate_names = set()
    for n in (repo_alias, slug, repo):
        if n:
            candidate_names.add(n)
            candidate_names.add(n.lower())

    # 0. Check if git_url is an existing local directory
    if git_url:
        local_checkout = resolve_local_git_checkout(git_url)
        if local_checkout:
            return local_checkout

    candidates: list[Path] = []
    if slug:
        board_repos_dir = _get_board_repos_dir(slug)
        for cname in candidate_names:
            candidates.append(board_repos_dir / cname)

    for cand in candidates:
        if cand.is_dir() and (cand / ".git").exists():
            return cand.resolve()

    return None


def resolve_task_repo_path(
    cursor: sqlite3.Cursor | None,
    board_slug: str | None,
    tenant: str | None,
    repo_alias: str | None = None,
) -> Path:
    """Resolve the git repository root for a task given its board_slug, tenant, and repo_alias."""
    if repo_alias:
        repo_alias = _validate_worktree_component(repo_alias, "repo_alias")
        if not board_slug or cursor is None:
            raise ValueError("repository alias must be linked to a configured board")
    # 0. Check board_repositories if board_slug and cursor are provided
    if board_slug and cursor:
        try:
            repo_row = None
            if repo_alias:
                try:
                    cursor.execute(
                        "SELECT repo_alias, git_url, target_branch FROM board_repositories WHERE board_slug = ? AND repo_alias = ?",
                        (board_slug, repo_alias),
                    )
                    repo_row = cursor.fetchone()
                except Exception as exc:
                    raise ValueError(
                        f"repository alias '{repo_alias}' is not linked to board '{board_slug}'"
                    ) from exc
                if not repo_row:
                    raise ValueError(
                        f"repository alias '{repo_alias}' is not linked to board '{board_slug}'"
                    )
            else:
                try:
                    cursor.execute(
                        "SELECT repo_alias, git_url, target_branch FROM board_repositories WHERE board_slug = ? ORDER BY id ASC LIMIT 1",
                        (board_slug,),
                    )
                    repo_row = cursor.fetchone()
                except Exception:
                    pass
            if not repo_row:
                try:
                    cursor.execute(
                        "SELECT slug, git_url FROM boards WHERE slug = ?", (board_slug,)
                    )
                    b_row = cursor.fetchone()
                    if b_row and dict(b_row).get("git_url"):
                        repo_row = {
                            "repo_alias": dict(b_row)["slug"],
                            "git_url": dict(b_row)["git_url"],
                        }
                except Exception:
                    pass

            if repo_row:
                r_dict = dict(repo_row)
                alias = (r_dict.get("repo_alias") or "").strip()
                url = (r_dict.get("git_url") or "").strip()
                if url:
                    local_checkout = resolve_local_git_checkout(url)
                    if local_checkout:
                        return local_checkout
                if repo_alias:
                    alias_checkout = _get_board_repos_dir(board_slug) / repo_alias
                    if alias_checkout.is_dir() and (alias_checkout / ".git").exists():
                        return alias_checkout.resolve()
                    raise ValueError(
                        f"repository alias '{repo_alias}' is linked to board "
                        f"'{board_slug}' but has no usable checkout"
                    )
                resolved_r = resolve_board_repo_path(
                    {"git_url": url, "slug": board_slug or alias, "repo_alias": alias}
                )
                if (
                    resolved_r
                    and resolved_r.exists()
                    and (resolved_r / ".git").exists()
                ):
                    return resolved_r

                if board_slug and alias:
                    w_repo = _get_board_repos_dir(board_slug) / alias
                    if w_repo.exists() and (w_repo / ".git").exists():
                        return w_repo
        except ValueError:
            raise
        except Exception:
            pass

    # 2. If tenant path is provided
    if tenant:
        t_path = Path(os.path.expanduser(tenant))
        if t_path.is_absolute() and t_path.exists() and (t_path / ".git").exists():
            return t_path

    # 3. Check ~/.zerofactory/workspaces/<board_slug>/repos if board_slug provided
    if board_slug:
        b_repos = _get_board_repos_dir(board_slug)
        if repo_alias:
            w_repo = b_repos / repo_alias
            if w_repo.is_dir() and (w_repo / ".git").exists():
                return w_repo
        if b_repos.is_dir():
            for sub in b_repos.iterdir():
                if sub.is_dir() and (sub / ".git").exists():
                    return sub


def setup_worktree(
    cursor: sqlite3.Cursor,
    task_id: str,
    title: str,
    assignee: str,
    tenant: str | None,
    db_path: Path,
    board_slug: str | None = None,
    repo_path: Path | None = None,
    repo_alias: str | None = None,
) -> str | None:
    """Ensure git worktree and branch exist for task execution."""
    try:
        task_id = _validate_worktree_component(task_id, "task_id")
        if repo_alias:
            repo_alias = _validate_worktree_component(repo_alias, "repo_alias")
        if not repo_alias and cursor:
            try:
                cursor.execute("SELECT repo_alias FROM tasks WHERE id = ?", (task_id,))
                t_row = cursor.fetchone()
            except sqlite3.OperationalError:
                t_row = None
            if t_row and t_row[0]:
                repo_alias = _validate_worktree_component(
                    str(t_row[0]).strip(), "repo_alias"
                )
        if repo_alias:
            if not board_slug or cursor is None:
                raise ValueError(
                    "repository alias must be linked to a configured board"
                )
            cursor.execute(
                "SELECT 1 FROM board_repositories WHERE board_slug = ? AND repo_alias = ?",
                (board_slug, repo_alias),
            )
            if not cursor.fetchone():
                raise ValueError(
                    f"repository alias '{repo_alias}' is not linked to board '{board_slug}'"
                )
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _log.warning("Worktree setup rejected for task %s: %s", task_id, exc)
        return None

    valid_profiles = getattr(_d(), "VALID_PROFILES", VALID_PROFILES)
    if not assignee or assignee == "unassigned" or assignee not in valid_profiles:
        assignee = "zf-builder"
        cursor.execute(
            "UPDATE tasks SET assignee = ?, skills = '[]' WHERE id = ?",
            (assignee, task_id),
        )
    else:
        norm_assignee = normalize_assignee(assignee)
        if norm_assignee != assignee:
            assignee = norm_assignee
            cursor.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id)
            )
        else:
            assignee = norm_assignee

    if os.environ.get("ZEROFACTORY_SKIP_GIT"):
        return None

    steps = StepTracker(task_id)
    steps.start("worktree.setup", f"assignee={assignee}")

    # Resolve primary repo path
    try:
        if not repo_path:
            repo_path = _d().resolve_task_repo_path(
                cursor, board_slug, tenant, repo_alias=repo_alias
            )
    except (TypeError, ValueError) as exc:
        steps.end("fail", str(exc))
        _log.warning("Worktree setup rejected for task %s: %s", task_id, exc)
        return None
    if not repo_path or not repo_path.exists():
        steps.end("fail", f"repo path unresolvable: {repo_path}")
        return None

    target_name = _validate_worktree_component(
        repo_alias or repo_path.name, "repo_alias"
    )
    try:
        if board_slug:
            try:
                from paths import get_board_worktrees_dir  # type: ignore
            except Exception:
                try:
                    from ..paths import get_board_worktrees_dir  # type: ignore
                except Exception:
                    get_board_worktrees_dir = lambda b: (
                        Path.home() / ".zerofactory" / "workspaces" / b / "worktrees"
                    )  # type: ignore
            worktree_base = get_board_worktrees_dir(board_slug)
        else:
            worktree_base = repo_path.parent / f"{repo_path.name}-worktrees"
        task_root = worktree_base / str(task_id)
        worktree_base = worktree_base.resolve()
        task_root = _validate_worktree_destination(task_root, task_root, worktree_base)
        worktree_dir = _validate_worktree_destination(
            task_root / target_name, task_root, worktree_base
        )
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, TypeError, ValueError) as exc:
        steps.end("fail", str(exc))
        _log.warning("Worktree setup rejected for task %s: %s", task_id, exc)
        return None

    branch_name = f"task/{task_id}"

    # If worktree_dir does not exist yet, check if branch is already checked out elsewhere or under task_root
    if not worktree_dir.exists():
        checked_out = _find_checked_out_worktree(repo_path, branch_name)
        if checked_out:
            try:
                checked_out = _validate_worktree_destination(
                    checked_out, task_root, worktree_base
                )
            except (OSError, ValueError) as exc:
                steps.end("fail", str(exc))
                _log.warning("Worktree setup rejected for task %s: %s", task_id, exc)
                return None
        if checked_out and checked_out.exists():
            worktree_dir = checked_out
        elif task_root.exists():
            cand_dirs = [task_root / repo_path.name, task_root]
            try:
                for sub in task_root.iterdir():
                    if sub.is_dir() and sub not in cand_dirs:
                        cand_dirs.append(sub)
            except Exception:
                pass
            for cand in cand_dirs:
                try:
                    cand = _validate_worktree_destination(
                        cand, task_root, worktree_base
                    )
                except (OSError, ValueError) as exc:
                    steps.end("fail", str(exc))
                    _log.warning(
                        "Worktree setup rejected for task %s: %s", task_id, exc
                    )
                    return None
                if cand.exists() and (
                    (cand / ".git").is_file() or (cand / ".git").is_dir()
                ):
                    try:
                        chk = subprocess.run(
                            ["git", "rev-parse", "--git-dir"],
                            cwd=str(cand),
                            capture_output=True,
                            timeout=5,
                        )
                        if chk.returncode == 0:
                            worktree_dir = cand
                            break
                    except Exception:
                        pass
    try:
        target_branch = ""
        if cursor and board_slug:
            try:
                if repo_alias:
                    cursor.execute(
                        "SELECT target_branch FROM board_repositories WHERE board_slug = ? AND repo_alias = ?",
                        (board_slug, repo_alias),
                    )
                    br_row = cursor.fetchone()
                    if br_row and br_row[0]:
                        target_branch = str(br_row[0]).strip()
                if not target_branch:
                    cursor.execute(
                        "SELECT target_branch FROM board_repositories WHERE board_slug = ? ORDER BY id ASC LIMIT 1",
                        (board_slug,),
                    )
                    br_row = cursor.fetchone()
                    if br_row and br_row[0]:
                        target_branch = str(br_row[0]).strip()
                if not target_branch:
                    cursor.execute(
                        "SELECT target_branch FROM boards WHERE slug = ?", (board_slug,)
                    )
                    b_row = cursor.fetchone()
                    if b_row and b_row[0]:
                        target_branch = str(b_row[0]).strip()
            except Exception:
                pass

        default_branch = _d().sync_repo_main(
            repo_path, default_branch=target_branch or None
        )
        base_ref = f"origin/{default_branch}"
        verify_ref = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{base_ref}"],
            cwd=repo_path,
            timeout=5,
        )
        if verify_ref.returncode != 0:
            verify_local = subprocess.run(
                [
                    "git",
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{default_branch}",
                ],
                cwd=repo_path,
                timeout=5,
            )
            base_ref = default_branch if verify_local.returncode == 0 else "HEAD"

        # Ensure worktree_dir is healthy if it exists on disk
        if worktree_dir.exists():
            rev_check = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(worktree_dir),
                capture_output=True,
                timeout=5,
            )
            if rev_check.returncode != 0:
                _log.warning(
                    "Worktree dir %s has invalid/dangling git pointer; removing to re-create",
                    worktree_dir,
                )
                import shutil

                try:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree_dir)],
                        cwd=str(repo_path),
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:
                    pass
                try:
                    subprocess.run(
                        ["git", "worktree", "prune"],
                        cwd=str(repo_path),
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:
                    pass
                if worktree_dir.exists():
                    shutil.rmtree(str(worktree_dir), ignore_errors=True)

        res = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=repo_path,
            timeout=5,
        )
        if res.returncode == 0:
            if not worktree_dir.exists():
                try:
                    subprocess.run(
                        ["git", "worktree", "add", str(worktree_dir), branch_name],
                        check=True,
                        cwd=repo_path,
                        timeout=_WORKTREE_ADD_TIMEOUT,
                    )
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                ) as add_err:
                    err_msg = ""
                    if isinstance(add_err, subprocess.CalledProcessError):
                        err_bytes = add_err.stderr or add_err.stdout or b""
                        err_msg = (
                            err_bytes.decode("utf-8", errors="replace")
                            if isinstance(err_bytes, bytes)
                            else str(err_bytes)
                        )
                    m = re.search(r"is already checked out at '([^']+)'", err_msg)
                    if m:
                        existing_path = _validate_worktree_destination(
                            Path(m.group(1)), task_root, worktree_base
                        )
                    else:
                        existing_path = None
                    if existing_path and existing_path.exists():
                        worktree_dir = existing_path
                    else:
                        existing_wt = _find_checked_out_worktree(repo_path, branch_name)
                        if existing_wt:
                            worktree_dir = _validate_worktree_destination(
                                existing_wt, task_root, worktree_base
                            )
                        else:
                            subprocess.run(
                                ["git", "worktree", "prune"],
                                check=False,
                                cwd=repo_path,
                                capture_output=True,
                                timeout=10,
                            )
                            try:
                                subprocess.run(
                                    [
                                        "git",
                                        "worktree",
                                        "add",
                                        str(worktree_dir),
                                        branch_name,
                                    ],
                                    check=True,
                                    cwd=repo_path,
                                    timeout=_WORKTREE_ADD_TIMEOUT,
                                )
                            except subprocess.CalledProcessError as add_err2:
                                err_bytes2 = add_err2.stderr or add_err2.stdout or b""
                                err_msg2 = (
                                    err_bytes2.decode("utf-8", errors="replace")
                                    if isinstance(err_bytes2, bytes)
                                    else str(err_bytes2)
                                )
                                m2 = re.search(
                                    r"is already checked out at '([^']+)'", err_msg2
                                )
                                if m2:
                                    existing_path = _validate_worktree_destination(
                                        Path(m2.group(1)), task_root, worktree_base
                                    )
                                else:
                                    existing_path = None
                                if existing_path and existing_path.exists():
                                    worktree_dir = existing_path
                                else:
                                    raise
            # Sync existing worktree with latest default branch if assignee is builder
            if assignee == "zf-builder" and worktree_dir.exists():
                worktree_dir = _validate_worktree_destination(
                    worktree_dir, task_root, worktree_base
                )
                _d().pull_and_merge_main(worktree_dir, repo_path, default_branch)
        else:
            if not worktree_dir.exists():
                try:
                    subprocess.run(
                        [
                            "git",
                            "worktree",
                            "add",
                            str(worktree_dir),
                            "-b",
                            branch_name,
                            base_ref,
                        ],
                        check=True,
                        cwd=repo_path,
                        timeout=_WORKTREE_ADD_TIMEOUT,
                    )
                except Exception as add_err:
                    err_msg = ""
                    if isinstance(add_err, subprocess.CalledProcessError):
                        err_bytes = add_err.stderr or add_err.stdout or b""
                        err_msg = (
                            err_bytes.decode("utf-8", errors="replace")
                            if isinstance(err_bytes, bytes)
                            else str(err_bytes)
                        )
                    m = re.search(r"is already checked out at '([^']+)'", err_msg)
                    if m:
                        existing_path = _validate_worktree_destination(
                            Path(m.group(1)), task_root, worktree_base
                        )
                    else:
                        existing_path = None
                    if existing_path and existing_path.exists():
                        worktree_dir = existing_path
                    else:
                        existing_wt = _find_checked_out_worktree(repo_path, branch_name)
                        if existing_wt:
                            worktree_dir = _validate_worktree_destination(
                                existing_wt, task_root, worktree_base
                            )
                        else:
                            subprocess.run(
                                [
                                    "git",
                                    "worktree",
                                    "add",
                                    str(worktree_dir),
                                    "-b",
                                    branch_name,
                                    "HEAD",
                                ],
                                check=True,
                                cwd=repo_path,
                                timeout=_WORKTREE_ADD_TIMEOUT,
                            )
            # Sync newly created worktree with latest default branch if assignee is builder
            if assignee == "zf-builder" and worktree_dir.exists():
                worktree_dir = _validate_worktree_destination(
                    worktree_dir, task_root, worktree_base
                )
                _d().pull_and_merge_main(worktree_dir, repo_path, default_branch)

        # Provision side-by-side sibling repositories
        if cursor and board_slug:
            try:
                cursor.execute(
                    "SELECT repo_alias, git_url, target_branch FROM board_repositories WHERE board_slug = ? AND repo_alias != ?",
                    (board_slug, target_name),
                )
                sibling_rows = [dict(r) for r in cursor.fetchall()]
                for s in sibling_rows:
                    s_alias = (s.get("repo_alias") or "").strip()
                    if not s_alias:
                        continue
                    s_alias = _validate_worktree_component(s_alias, "repo_alias")
                    s_dir = _validate_worktree_destination(
                        task_root / s_alias, task_root, worktree_base
                    )
                    if not s_dir.exists():
                        s_repo = _d().resolve_task_repo_path(
                            cursor, board_slug, tenant, repo_alias=s_alias
                        )
                        if s_repo and s_repo.exists():
                            s_branch = (s.get("target_branch") or "main").strip()
                            try:
                                _d().sync_repo_main(s_repo, default_branch=s_branch)
                            except Exception:
                                pass
                            s_ref = f"origin/{s_branch}"
                            v = subprocess.run(
                                [
                                    "git",
                                    "show-ref",
                                    "--verify",
                                    "--quiet",
                                    f"refs/remotes/{s_ref}",
                                ],
                                cwd=s_repo,
                                timeout=5,
                            )
                            if v.returncode != 0:
                                v2 = subprocess.run(
                                    [
                                        "git",
                                        "show-ref",
                                        "--verify",
                                        "--quiet",
                                        f"refs/heads/{s_branch}",
                                    ],
                                    cwd=s_repo,
                                    timeout=5,
                                )
                                s_ref = s_branch if v2.returncode == 0 else "HEAD"
                            try:
                                subprocess.run(
                                    [
                                        "git",
                                        "worktree",
                                        "add",
                                        "--detach",
                                        str(s_dir),
                                        s_ref,
                                    ],
                                    check=True,
                                    cwd=s_repo,
                                    timeout=10,
                                    capture_output=True,
                                )
                            except Exception as sib_err:
                                _log.debug(
                                    "Could not add detached worktree for sibling %s: %s",
                                    s_alias,
                                    sib_err,
                                )
            except Exception as e:
                _log.debug(
                    "Error provisioning sibling worktrees for %s: %s", board_slug, e
                )

        worktree_dir = _validate_worktree_destination(
            worktree_dir, task_root, worktree_base
        )
        cursor.execute(
            "UPDATE tasks SET workspace_kind = 'dir', workspace_path = ?, branch_name = ? WHERE id = ?",
            (str(worktree_dir), branch_name, task_id),
        )
        steps.end("ok", f"{worktree_dir} branch={branch_name}")
        return str(worktree_dir)
    except Exception as e:
        steps.end("error", f"{type(e).__name__}: {e}")
        _log.warning(
            "Worktree setup skipped or failed for task %s (%s): %s",
            task_id,
            repo_path,
            e,
        )
        if (
            "task_root" in locals()
            and isinstance(task_root, Path)
            and task_root.exists()
        ):
            try:
                if not any(task_root.iterdir()):
                    task_root.rmdir()
            except Exception:
                pass
        return None


def _handle_local_merge_conflict(
    cursor: sqlite3.Cursor,
    task_id: str,
    conflict_files: list[str],
    now: int,
) -> None:
    """Handle a local merge conflict when syncing task branch with main before push."""
    max_conflict_retries = int(os.environ.get("ZEROFACTORY_MAX_CONFLICT_RETRIES", "3"))

    cursor.execute("SELECT metadata FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    meta = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except Exception:
            meta = {}

    retries = int(meta.get("conflict_retries", 0))
    if retries > max_conflict_retries:
        _log.debug(
            "Task %s has already reached conflict retries limit (%d > %d); skipping duplicate conflict failure handling",
            task_id,
            retries,
            max_conflict_retries,
        )
        return
    retries += 1
    meta["conflict_retries"] = retries
    meta.pop("awaiting_pr", None)

    file_msg = f" in: {', '.join(conflict_files)}" if conflict_files else ""

    if retries > max_conflict_retries:
        meta["blocked_reason"] = (
            f"Merge conflict resolution exceeded {max_conflict_retries} attempts{file_msg}"
        )
        if int(meta.get("orchestrator_recovery_count", 0)) < 1:
            meta.pop("awaiting_pr", None)
            meta["triage_phase"] = "recovery"
            transition_task(
                cursor.connection,
                task_id,
                "todo",
                target_assignee="zf-orchestrator",
                blocked_reason=meta["blocked_reason"],
                metadata_patch=meta,
                actor="dispatcher",
            )
        else:
            meta["blocked_reason_type"] = "stuck"
            transition_task(
                cursor.connection,
                task_id,
                "blocked",
                target_assignee="human",
                blocked_reason=meta["blocked_reason"],
                metadata_patch=meta,
                actor="dispatcher",
            )
        cursor.execute(
            "INSERT INTO task_activity (task_id, actor, action, details, created_at) VALUES (?, 'dispatcher', 'pr_conflict_failed', ?, ?)",
            (
                task_id,
                f"Merge conflict resolution exceeded {max_conflict_retries} attempts{file_msg}. Moved to blocked.",
                now,
            ),
        )
        try:
            cursor.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "dispatcher",
                    f"🚨 **Merge Conflict Resolution Failed**: Pulling latest main branch encountered conflicts{file_msg}. "
                    f"Automatic resolution was attempted {retries - 1} times without success. "
                    f"Task has been moved to **blocked** for manual review and resolution.",
                    now,
                ),
            )
        except Exception as e:
            _log.debug("Failed to record task comment for conflict limit: %s", e)
        return

    transition_task(
        cursor.connection,
        task_id,
        "todo",
        target_assignee="zf-builder",
        metadata_patch=meta,
        actor="dispatcher",
    )
    cursor.execute(
        "INSERT INTO task_activity (task_id, actor, action, details, created_at) VALUES (?, 'dispatcher', 'pr_conflict', ?, ?)",
        (
            task_id,
            f"Merge conflict with main branch detected{file_msg}. Routed back to zf-builder for resolution.",
            now,
        ),
    )
    try:
        cursor.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (
                task_id,
                "dispatcher",
                f"🚨 **Merge Conflict Detected**: Pulling latest main branch encountered conflicts{file_msg}. "
                f"Worktree has been left with conflict markers for resolution. "
                f"Please reconcile conflict markers, verify tests pass, and commit.",
                now,
            ),
        )
    except Exception as e:
        _log.debug("Failed to record task comment for conflict: %s", e)


def _delete_remote_branch(task_id: str, repo_path: Path) -> None:
    """Best-effort deletion of the remote ``task/<task_id>`` branch on origin."""
    if not task_id:
        return
    if not repo_path or not Path(repo_path).exists():
        return
    branch = f"task/{task_id}"
    try:
        res = subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            check=False,
            cwd=str(repo_path),
            capture_output=True,
            timeout=_REMOTE_BRANCH_DELETE_TIMEOUT,
            env=git_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        _log.warning(
            "Remote branch delete timed out after %ss for %s; leaving remote branch for manual cleanup",
            _REMOTE_BRANCH_DELETE_TIMEOUT,
            branch,
        )
        return
    except Exception as e:
        _log.warning("Remote branch delete failed for %s: %s", branch, e)
        return
    if res.returncode != 0:
        _log.warning(
            "Remote branch delete failed for %s (rc=%s): %s",
            branch,
            res.returncode,
            (res.stderr or res.stdout or "").strip(),
        )
    else:
        _log.info("Deleted remote branch %s (PR archived)", branch)


def _remove_worktree(workspace_path: str | None, repo_path: Path) -> None:
    """Safely remove a git worktree and any sibling worktrees without hanging the dispatch cycle."""
    if not workspace_path or not Path(workspace_path).exists():
        return

    ws_path = Path(workspace_path)
    task_root = ws_path.parent

    # 1. Clean up any sibling worktrees in task_root if under a -worktrees or workspaces/.../worktrees directory
    try:
        is_managed_worktree = (
            "-worktrees" in str(task_root) or "worktrees" in task_root.parts
        )
        if task_root.exists() and is_managed_worktree:
            for item in task_root.iterdir():
                if item.is_dir() and item != ws_path and (item / ".git").is_file():
                    try:
                        rev_res = subprocess.run(
                            [
                                "git",
                                "rev-parse",
                                "--path-format=absolute",
                                "--git-common-dir",
                            ],
                            cwd=str(item),
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        sib_main = None
                        if rev_res.returncode == 0:
                            c_dir = Path(rev_res.stdout.strip())
                            sib_main = c_dir.parent if c_dir.name == ".git" else c_dir
                        if sib_main and sib_main.exists():
                            subprocess.run(
                                ["git", "worktree", "remove", str(item), "--force"],
                                cwd=str(sib_main),
                                capture_output=True,
                                timeout=_WORKTREE_REMOVE_TIMEOUT,
                            )
                            subprocess.run(
                                ["git", "worktree", "prune"],
                                cwd=str(sib_main),
                                capture_output=True,
                                timeout=_WORKTREE_REMOVE_TIMEOUT,
                            )
                    except Exception as e:
                        _log.debug(
                            "Could not cleanly remove sibling worktree %s: %s", item, e
                        )
    except Exception as e:
        _log.debug("Could not inspect siblings in task_root %s: %s", task_root, e)

    # 2. Remove primary worktree
    try:
        subprocess.run(
            ["git", "worktree", "remove", workspace_path, "--force"],
            check=False,
            cwd=str(repo_path),
            capture_output=True,
            timeout=_WORKTREE_REMOVE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _log.warning(
            "Worktree remove timed out after %ss for %s; falling back to 'git worktree prune'",
            _WORKTREE_REMOVE_TIMEOUT,
            workspace_path,
        )
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                check=False,
                cwd=str(repo_path),
                capture_output=True,
                timeout=_WORKTREE_REMOVE_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            _log.warning(
                "Worktree prune also timed out after %ss for %s: %s",
                _WORKTREE_REMOVE_TIMEOUT,
                workspace_path,
                e.cmd,
            )
        return
    except Exception as e:
        _log.warning("Worktree remove failed for %s: %s", workspace_path, e)

    if Path(workspace_path).exists():
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                check=False,
                cwd=str(repo_path),
                capture_output=True,
                timeout=_WORKTREE_REMOVE_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            _log.warning(
                "Worktree prune timed out after %ss for %s: %s",
                _WORKTREE_REMOVE_TIMEOUT,
                workspace_path,
                e.cmd,
            )
        if Path(workspace_path).exists():
            _log.warning(
                "Worktree directory still present after cleanup attempts: %s",
                workspace_path,
            )

    # 3. Clean up task_root directory if empty or under -worktrees / workspaces/.../worktrees
    try:
        import shutil

        is_managed_task_root = "-worktrees" in str(task_root) or (
            "worktrees" in task_root.parts
            and task_root.name == str(ws_path.parent.name)
        )
        if (
            task_root.exists()
            and is_managed_task_root
            and task_root != repo_path.parent
            and task_root != Path.home()
            and task_root != Path.home() / "git"
            and task_root != Path.home() / ".zerofactory"
        ):
            shutil.rmtree(str(task_root), ignore_errors=True)
    except Exception:
        pass


def _handle_pr_conflict_from_github(
    cursor: sqlite3.Cursor,
    task_id: str,
    title: str,
    workspace_path: str | None,
    repo_path: Path,
    tenant: str | None,
    db_path: Path,
    board_slug: str | None,
    now: int,
) -> None:
    """Handle a PR that has merge conflicts on GitHub by routing back to builder."""
    max_conflict_retries = int(os.environ.get("ZEROFACTORY_MAX_CONFLICT_RETRIES", "3"))

    cursor.execute("SELECT metadata FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    meta = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except Exception:
            meta = {}

    retries = int(meta.get("conflict_retries", 0))
    if retries > max_conflict_retries:
        _log.debug(
            "Task %s has already reached conflict retries limit (%d > %d); skipping duplicate PR conflict failure handling",
            task_id,
            retries,
            max_conflict_retries,
        )
        return
    retries += 1
    meta["conflict_retries"] = retries
    meta.pop("awaiting_pr", None)

    _d().stop_task_worker(task_id, cursor)
    if workspace_path and Path(workspace_path).exists():
        _d()._remove_worktree(workspace_path, repo_path)

    # PR author lives in metadata (set at packaging); titles carry no state.
    author = normalize_assignee(meta.get("packaged_by") or "zf-builder")
    if author == "zf-reviewer":
        author = "zf-builder"

    if retries > max_conflict_retries:
        meta["blocked_reason"] = (
            f"GitHub PR conflict resolution exceeded {max_conflict_retries} attempts"
        )
        if int(meta.get("orchestrator_recovery_count", 0)) < 1:
            meta.pop("awaiting_pr", None)
            meta["triage_phase"] = "recovery"
            transition_task(
                cursor.connection,
                task_id,
                "todo",
                target_assignee="zf-orchestrator",
                blocked_reason=meta["blocked_reason"],
                metadata_patch=meta,
                actor="dispatcher",
            )
        else:
            meta["blocked_reason_type"] = "stuck"
            transition_task(
                cursor.connection,
                task_id,
                "blocked",
                target_assignee="human",
                blocked_reason=meta["blocked_reason"],
                metadata_patch=meta,
                actor="dispatcher",
            )
        cursor.execute(
            "INSERT INTO task_activity (task_id, actor, action, details, created_at) VALUES (?, 'dispatcher', 'pr_conflict_failed', ?, ?)",
            (
                task_id,
                f"GitHub PR conflict resolution exceeded {max_conflict_retries} attempts. Moved to blocked.",
                now,
            ),
        )
        try:
            cursor.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "dispatcher",
                    f"🚨 **PR Conflict Resolution Failed**: GitHub reports mergeable state is CONFLICTING. "
                    f"Automatic resolution was attempted {retries - 1} times without success. "
                    f"Task has been moved to **blocked** for manual review and resolution.",
                    now,
                ),
            )
        except Exception as e:
            _log.debug("Failed to record task comment for conflict limit: %s", e)
        return

    wt_path = _d().setup_worktree(
        cursor,
        task_id,
        title,
        author,
        tenant,
        db_path,
        board_slug=board_slug,
        repo_path=repo_path,
    )
    conflict_files = []
    if wt_path and Path(wt_path).exists():
        _verified, conflict_files, _err = _d().check_unresolved_conflicts_safe(
            Path(wt_path)
        )

    file_msg = f" in {', '.join(conflict_files)}" if conflict_files else ""
    transition_task(
        cursor.connection,
        task_id,
        "todo",
        target_assignee=author,
        metadata_patch=meta,
        actor="dispatcher",
    )
    cursor.execute(
        "INSERT INTO task_activity (task_id, actor, action, details, created_at) VALUES (?, 'dispatcher', 'pr_conflict', ?, ?)",
        (
            task_id,
            f"GitHub PR is conflicting with main branch{file_msg}. Routed to {author} to resolve conflicts.",
            now,
        ),
    )
    try:
        cursor.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (
                task_id,
                "dispatcher",
                f"🚨 **PR Conflict Detected**: GitHub reports mergeable state is CONFLICTING. "
                f"The worktree has been synced with latest main branch{file_msg}. "
                f"Please resolve all conflict markers, verify tests pass, and commit.",
                now,
            ),
        )
    except Exception as e:
        _log.debug("Failed to record task comment for conflict: %s", e)


def run_deterministic_precommit(workspace_path: Path) -> tuple[bool, str, int]:
    """Execute .zerofactory/precommit.sh in the workspace if present.

    Returns:
        (passed, output_or_error, exit_code)
    """
    if os.environ.get("ZEROFACTORY_SKIP_PRECOMMIT"):
        return True, "ZEROFACTORY_SKIP_PRECOMMIT enabled", 0

    script_path = workspace_path / ".zerofactory" / "precommit.sh"
    if not script_path.is_file():
        return True, "No .zerofactory/precommit.sh found", 0

    try:
        current_mode = script_path.stat().st_mode
        script_path.chmod(current_mode | 0o755)
    except Exception:
        pass

    timeout = int(os.environ.get("ZEROFACTORY_PRECOMMIT_TIMEOUT_SECONDS", "300"))
    # Hermetic gate: drop interpreter-path overrides leaked from the host
    # process (e.g. hermes runtime PYTHONPATH). The precommit script's own
    # dependency check would otherwise pass via the leaked paths while the
    # test suite's subprocesses (which reset PYTHONPATH) fail to import deps.
    env = {
        **{
            k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")
        },
        "CI": "1",
        "ZEROFACTORY_PRECOMMIT": "1",
    }

    try:
        script_arg = str(script_path)
        bash_exe = "bash"
        if os.name == "nt":
            # git-bash (MSYS) strips backslashes from Windows paths passed as
            # argv ("C:\x\y" arrives as "C:xy"); hand it a POSIX-style path.
            normalized = script_arg.replace("\\", "/")
            drive, _, rest = normalized.partition("/")
            script_arg = f"/{drive[:1].lower()}/{rest}"
            # Bare "bash" on PATH is often the WSL launcher (System32\bash.exe),
            # which cannot see /c/... paths; prefer Git for Windows' bash.
            import shutil as _shutil

            _git = _shutil.which("git")
            if _git:
                for _gdir in Path(_git).resolve().parents[:3]:
                    _cand = _gdir / "bin" / "bash.exe"
                    if not _cand.exists():
                        _cand = _gdir / "usr" / "bin" / "bash.exe"
                    if _cand.exists():
                        bash_exe = str(_cand)
        res = subprocess.run(
            [bash_exe, script_arg],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        out = res.stdout or ""
        if res.stderr:
            out += "\n" + res.stderr if out else res.stderr
        # Diagnostic: persist the full precommit output so repeated gate
        # failures can be root-caused (DB fields truncate the output).
        try:
            task_root = workspace_path.parent
            log_path = task_root / f"precommit-gate-{int(time.time())}.log"
            hdr = (
                f"# task_root={task_root}\n# returncode={res.returncode}\n"
                f"# CI={env.get('CI')} ZEROFACTORY_PRECOMMIT={env.get('ZEROFACTORY_PRECOMMIT')}\n\n"
            )
            log_path.write_text(hdr + out, encoding="utf-8")
        except Exception:
            pass
        if res.returncode == 0:
            return True, out.strip(), 0
        return (
            False,
            out.strip() or f"Precommit script exited with code {res.returncode}",
            res.returncode,
        )
    except subprocess.TimeoutExpired as te:
        out = te.stdout or ""
        if te.stderr:
            out += "\n" + te.stderr if out else te.stderr
        return False, f"Precommit script timed out after {timeout}s.\n{out}".strip(), -1
    except Exception as e:
        return False, f"Precommit script execution failed: {e}", -1


def _handle_precommit_failure(
    cursor: sqlite3.Cursor,
    task_id: str,
    workspace_path: str,
    err_output: str,
    now: int,
    code: int = 0,
) -> None:
    """Handle deterministic precommit check failure before git commit/push.

    ``code`` is the runner's exit code: a pure timeout is surfaced as ``-1``
    (see ``run_deterministic_precommit``), distinct from a genuine test/lint
    failure (a real non-zero exit). On a timeout we clear any stale build
    output before routing back to the builder so the next retry does not trip
    over a half-written artifact (e.g. astro's ``renderers.mjs`` error on a
    leftover ``build/`` dir from a killed run).
    """
    max_retries = int(os.environ.get("ZEROFACTORY_MAX_PRECOMMIT_RETRIES", "3"))

    cursor.execute("SELECT metadata FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    meta = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0])
        except Exception:
            meta = {}

    retries = int(meta.get("precommit_retries", 0)) + 1
    meta["precommit_retries"] = retries
    meta["last_precommit_error"] = err_output[:2500]

    snippet = err_output.strip()
    if len(snippet) > 1500:
        snippet = snippet[-1500:]

    if retries > max_retries:
        meta["blocked_reason"] = (
            f"Deterministic precommit failed after {max_retries} attempts"
        )
        if int(meta.get("orchestrator_recovery_count", 0)) < 1:
            meta.pop("awaiting_pr", None)
            meta["triage_phase"] = "recovery"
            transition_task(
                cursor.connection,
                task_id,
                "todo",
                target_assignee="zf-orchestrator",
                blocked_reason=meta["blocked_reason"],
                metadata_patch=meta,
                actor="dispatcher",
            )
        else:
            meta["blocked_reason_type"] = "stuck"
            transition_task(
                cursor.connection,
                task_id,
                "blocked",
                target_assignee="human",
                blocked_reason=meta["blocked_reason"],
                metadata_patch=meta,
                actor="dispatcher",
            )
        cursor.execute(
            "INSERT INTO task_activity (task_id, actor, action, details, created_at) VALUES (?, 'dispatcher', 'precommit_failed', ?, ?)",
            (
                task_id,
                f"Deterministic precommit failed after {max_retries} attempts. Moved to blocked.",
                now,
            ),
        )
        try:
            cursor.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "dispatcher",
                    f"🚨 **Deterministic Precommit Failed**: Execution of `.zerofactory/precommit.sh` failed after {max_retries} attempts.\n\n```\n{snippet}\n```\nMoved task to **blocked** for inspection.",
                    now,
                ),
            )
        except Exception as e:
            _log.warning(
                "Failed to insert precommit failure comment for task %s: %s", task_id, e
            )
    else:
        # Route back to the builder as a claimable 'todo' retry (the precommit
        # failure output is injected into the respawned worker's prompt); clear
        # awaiting_pr — this is builder work again, not the packaging phase.
        meta.pop("awaiting_pr", None)
        if code == -1:
            # Pure timeout: a killed run may have left stale build output
            # (astro's renderers.mjs error on a leftover build/ dir) that would
            # fail the next retry deterministically. Clear it so the re-run
            # starts clean. Guarded by existence check; single minimal step,
            # no new config surface.
            stale_build = Path(workspace_path) / "build"
            if stale_build.exists():
                try:
                    shutil.rmtree(stale_build)
                    _log.info(
                        "Task %s: cleared stale %s after precommit timeout",
                        task_id,
                        stale_build,
                    )
                except Exception as e:
                    _log.warning(
                        "Task %s: failed to clear stale build dir %s: %s",
                        task_id,
                        stale_build,
                        e,
                    )
        transition_task(
            cursor.connection,
            task_id,
            "todo",
            target_assignee="zf-builder",
            metadata_patch=meta,
            actor="dispatcher",
        )
        cursor.execute(
            "INSERT INTO task_activity (task_id, actor, action, details, created_at) VALUES (?, 'dispatcher', 'precommit_failed_retry', ?, ?)",
            (
                task_id,
                f"Deterministic precommit failed (attempt {retries}/{max_retries}); routing back to zf-builder",
                now,
            ),
        )
        try:
            cursor.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    "dispatcher",
                    f"🚨 **Deterministic Precommit Failed** (Attempt {retries}/{max_retries})\n\nExecution of `.zerofactory/precommit.sh` failed with output:\n```\n{snippet}\n```\nPlease fix the formatting, build, or test failures and mark done.",
                    now,
                ),
            )
        except Exception as e:
            _log.warning(
                "Failed to insert precommit retry comment for task %s: %s", task_id, e
            )
