"""Review-cap arbitration: a delegate decides when builder and reviewer disagree.

Without this module a task whose reviewer requests changes in
``ZEROFACTORY_MAX_REVIEW_ROUNDS`` consecutive rounds is parked for a human
(``assignee='human'``, ``status='blocked'``) and nothing moves until a human
answers. Arbitration adds one step before that park becomes final: the
dispatcher requests a decision on the PR from an arbiter profile (default
``zf-gatekeeper``, the profile that already runs the merge sweeps).

Protocol - every message is a PR conversation comment, so the decision trail
stays on the PR. The dispatcher's request:

    [AI:zf-dispatcher] ARBITRATION REQUEST
    REQUEST: arbitration:<task_id>:<full_head_sha>
    HEAD: <full_head_sha>

and the arbiter's answer, of which only the FIRST line is read:

    [AI:<arbiter>] ARBITRATION VERDICT: MERGE | REQUEST: <same key> | HEAD: <same sha>
    [AI:<arbiter>] ARBITRATION VERDICT: FIX | REQUEST: <same key> | HEAD: <same sha>
    [AI:<arbiter>] ARBITRATION VERDICT: ESCALATE | REQUEST: <same key> | HEAD: <same sha>

A verdict is consumed only when every one of these holds; anything else fails
closed (logged, ignored, routes nothing):

  * it is a PR conversation comment - never an inline review body or a review
    summary - and its FIRST line matches the grammar above exactly;
  * the ``[AI:<role>]`` tag names the configured arbiter AND GitHub says the
    author is an account allowed to speak for it
    (``ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS``; default: the account
    ``gh`` is authenticated as on this host plus the repository owner). The
    role tag is text anyone can type, so it is never sufficient on its own;
  * ``REQUEST`` is the task's active pending request key, ``HEAD`` is that
    request's head, and the PR's live head is still that head;
  * the comment was created after the request and before its deadline.

    MERGE      only minor findings remain. The arbiter merges the PR under its
               own merge rules and files follow-up tasks; the task stays parked
               until the PR is actually merged and is never re-arbitrated.
    FIX        the verdict body is the authoritative brief. The dispatcher
               grants ONE bounded fix round scoped to that brief with a fresh
               review budget, instead of re-queuing the builder on every prior
               comment.
    ESCALATE   a genuine human decision is needed; the task stays parked.

Bounds, so this can never turn into a longer loop:
  * at most ``ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS`` handoffs per task
  * an unanswered request older than
    ``ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS`` is left parked, and a
    reply that arrives after that deadline is not applied
  * tasks flagged for human review are never arbitrated, including when the
    flag appears while a request is pending
  * no arbiter profile on this host -> no request is ever posted
  * a request comment is posted at most once per request key: an existing one
    is adopted, a failed POST leaves no live request

Every knob is an environment variable, exactly like ``ZEROFACTORY_MAX_REVIEW_ROUNDS``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .github_pr import extract_gh_repo_info, fetch_pr_conversation_comments

_log = logging.getLogger(__name__)

STATE_KEY = "review_cap_arbitration"
DEFAULT_ARBITER = "zf-gatekeeper"

STATUS_REQUESTED = "requested"
STATUS_FIX_REQUESTED = "fix-requested"
STATUS_MERGE_APPROVED = "merge-approved"
STATUS_ESCALATED = "escalated"
STATUS_EXPIRED = "expired"
STATUS_VOID = "void"

# A task parked at the review cap (blocked / assignee=human / review_cap_reached)
# whose request is in one of these statuses is an arbitration park. Conflict
# handling, ordinary review-comment routing and auto-merge leave it alone until
# the PR is merged or closed or an operator recovers the task.
PARKED_STATUSES = frozenset(
    {
        STATUS_REQUESTED,
        STATUS_MERGE_APPROVED,
        STATUS_ESCALATED,
        STATUS_EXPIRED,
        STATUS_VOID,
    }
)

VERDICT_MERGE = "MERGE"
VERDICT_FIX = "FIX"
VERDICT_ESCALATE = "ESCALATE"
VERDICTS = (VERDICT_MERGE, VERDICT_FIX, VERDICT_ESCALATE)

VERDICT_PREFIX = "ARBITRATION VERDICT:"
REQUEST_PREFIX = "[AI:zf-dispatcher] ARBITRATION REQUEST"

# A verdict must be the FIRST line and carry the request key and full head, so
# it can be bound to exactly one request on exactly one commit.
_VERDICT_LINE_RE = re.compile(
    r"^\[AI:(?P<role>[A-Za-z0-9_.-]+)\]\s*ARBITRATION\s+VERDICT:\s*"
    r"(?P<verdict>FIX|MERGE|ESCALATE)\s*\|\s*REQUEST:\s*(?P<request>\S+)\s*\|\s*HEAD:\s*"
    r"(?P<head>[0-9A-Fa-f]{40})\s*$",
    re.IGNORECASE,
)
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_COMMENT_URL_RE = re.compile(r"#issuecomment-(\d+)")

# Tolerated clock skew between this host and GitHub timestamps.
_CLOCK_SLACK_SECONDS = 300
_MAX_TRACKED_REJECTIONS = 50

# Same opt-out vocabulary as github_pr.is_task_automerge_enabled.
_OPT_OUT_TAGS = {"hitl", "manual-review", "tag:hitl", "tag:manual-review"}

_GH_LOGIN_TTL_SECONDS = 300.0
_gh_login_cache: dict[str, Any] = {"login": None, "checked_at": 0.0}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_login(value: Any) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _split_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            raw = parsed
        else:
            return [t.strip().lower() for t in raw.split(",") if t.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(t).strip().lower() for t in raw if str(t).strip()]
    return []


def arbitration_settings() -> dict[str, Any]:
    """Env-driven knobs; nothing here needs dashboard plumbing."""
    arbiter = (os.environ.get("ZEROFACTORY_REVIEW_CAP_ARBITER") or "").strip()
    return {
        "enabled": _env_flag("ZEROFACTORY_REVIEW_CAP_ARBITRATION", True),
        "arbiter": arbiter or DEFAULT_ARBITER,
        "timeout_hours": _env_int(
            "ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS", 24, minimum=1
        ),
        "max_arbitrations": _env_int(
            "ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS", 2, minimum=1
        ),
    }


def gh_authenticated_login(repo_path: Any = None) -> str | None:
    """The GitHub account ``gh`` is authenticated as on this host (cached).

    Every ``[AI:*]`` comment the dispatcher and its profiles post is authored
    by this account, so it is a default identity allowed to speak for the
    arbiter. None when GitHub cannot be asked (skip modes, not logged in).
    """
    if os.environ.get("ZEROFACTORY_SKIP_GIT") or os.environ.get(
        "ZEROFACTORY_SKIP_GH_API"
    ):
        return None
    now = time.monotonic()
    cached = _gh_login_cache["login"]
    if cached and now - _gh_login_cache["checked_at"] < _GH_LOGIN_TTL_SECONDS:
        return cached
    try:
        res = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            cwd=str(repo_path) if repo_path else None,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    login = _normalize_login(res.stdout) if res.returncode == 0 else ""
    if not login:
        return None
    _gh_login_cache.update(login=login, checked_at=now)
    return login


def arbiter_github_logins(pr_url: str | None = None, repo_path: Any = None) -> set[str]:
    """GitHub accounts whose conversation comments may count as the verdict.

    ``ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS`` (comma-separated) replaces
    the default. Default: the account ``gh`` is authenticated as on this host
    plus the repository owner taken from the PR URL - the two identities that
    operate the factory. An empty result authorizes nobody.
    """
    raw = (os.environ.get("ZEROFACTORY_REVIEW_CAP_ARBITER_GITHUB_LOGINS") or "").strip()
    if raw:
        return {
            login for login in (_normalize_login(p) for p in raw.split(",")) if login
        }
    logins: set[str] = set()
    login = gh_authenticated_login(repo_path)
    if login:
        logins.add(_normalize_login(login))
    info = extract_gh_repo_info(pr_url or "") if pr_url else None
    if info:
        logins.add(_normalize_login(info[0]))
    return logins


def is_human_review_task(task_meta: dict[str, Any] | None, tags: Any = None) -> bool:
    """Tasks the owner reserved for themselves are not handed to an arbiter."""
    meta = task_meta or {}
    if meta.get("permanently_blocked"):
        return True
    if any(t in _OPT_OUT_TAGS for t in _split_tags(tags)):
        return True
    labels = (
        (meta.get("external_issue") or {})
        if isinstance(meta.get("external_issue"), dict)
        else {}
    ).get("labels")
    if any(str(l).strip().lower() in ("hitl", "manual-review") for l in labels or []):
        return True
    return False


def profiles_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / "profiles"


def arbiter_profile_available(arbiter: str) -> bool:
    """Only hand off to an arbiter that exists on this host. Without one the
    request would never be answered, so the task is better left for the human."""
    arbiter = (arbiter or "").strip()
    return bool(arbiter) and (profiles_dir() / arbiter).is_dir()


def state_of(task_meta: dict[str, Any] | None) -> dict[str, Any]:
    state = (task_meta or {}).get(STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


def is_arbitration_park(
    task_meta: dict[str, Any] | None, *, status: Any, assignee: Any
) -> bool:
    """True while a task sits on an arbitration request, whatever its outcome.

    Such a task is not routed by conflict handling, ordinary review-comment
    routing or auto-merge: only a merged/closed PR or an operator moves it.
    """
    meta = task_meta or {}
    return (
        bool(meta.get("review_cap_reached"))
        and (status or "") == "blocked"
        and (assignee or "") == "human"
        and state_of(meta).get("status") in PARKED_STATUSES
    )


def voided(state: dict[str, Any] | None, reason: str, now: Any) -> dict[str, Any]:
    """A copy of ``state`` marked void: no verdict may be consumed from it."""
    return {
        **(state or {}),
        "status": STATUS_VOID,
        "void_reason": reason,
        "voided_at": _coerce_int(now),
    }


def request_key(task_id: str, head_sha: str) -> str:
    """Deterministic identity of one arbitration request."""
    return f"arbitration:{task_id}:{str(head_sha or '').lower()}"


def verdict_line(arbiter: str, verdict: str, key: str, head_sha: str) -> str:
    """The exact first line the arbiter pastes to answer ``verdict``."""
    return f"[AI:{arbiter}] ARBITRATION VERDICT: {verdict} | REQUEST: {key} | HEAD: {head_sha}"


def parse_github_timestamp(value: Any) -> int | None:
    """Epoch seconds for a GitHub ISO-8601 timestamp; None when unusable."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def build_arbitration_request(
    task_meta: dict[str, Any] | None,
    *,
    task_id: str,
    arbiter: str,
    now: int,
    head_sha: str,
    rounds: int,
    max_arbitrations: int,
) -> dict[str, Any] | None:
    """State for a new handoff, or None when one must not be opened.

    Refuses a second live request for the same task, an exhausted per-task
    budget, and anything but a full 40-char head SHA: a request that cannot
    name the exact commit under review cannot be bound to a verdict.
    """
    prev = state_of(task_meta)
    if prev.get("status") == STATUS_REQUESTED:
        return None
    attempts = _coerce_int(prev.get("attempts"))
    if attempts >= max_arbitrations:
        return None
    head = str(head_sha or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(head):
        return None
    return {
        "status": STATUS_REQUESTED,
        "arbiter": arbiter,
        "attempts": attempts + 1,
        "max_arbitrations": max_arbitrations,
        "requested_at": int(now),
        "request_head": head,
        "request_key": request_key(task_id, head),
        "rounds": rounds,
    }


def arbitration_is_stale(state: dict[str, Any], now: int, timeout_hours: int) -> bool:
    if not state or state.get("status") != STATUS_REQUESTED:
        return False
    requested_at = _coerce_int(state.get("requested_at"))
    if requested_at <= 0:
        # No request time means no deadline a verdict could be held to and
        # none the park could expire on: such a request is unusable.
        return True
    return (int(now) - requested_at) >= timeout_hours * 3600


def format_arbitration_request(
    *,
    task_id: str,
    title: str,
    head_sha: str,
    rounds: int,
    arbiter: str,
    attempt: int,
    max_arbitrations: int,
) -> str:
    key = request_key(task_id, head_sha)
    head = str(head_sha or "").strip().lower()
    lines = [
        REQUEST_PREFIX,
        f"REQUEST: {key}",
        f"HEAD: {head}",
        "",
        f"Arbitration attempt {attempt}/{max_arbitrations}.",
        "",
        (
            f"The reviewer has requested changes for {rounds} consecutive review "
            "round(s) on this head, so the dispatcher stopped the build/review loop "
            "instead of starting another one."
        ),
        f"@{arbiter}: decide this PR against its task spec and answer with exactly "
        "ONE conversation comment whose FIRST line is one of these (copy it verbatim - "
        "the dispatcher binds the verdict to this request and head):",
        "",
        f"`{verdict_line(arbiter, VERDICT_MERGE, key, head)}`",
        f"`{verdict_line(arbiter, VERDICT_FIX, key, head)}`",
        f"`{verdict_line(arbiter, VERDICT_ESCALATE, key, head)}`",
        "",
        "- MERGE - only minor findings remain. Merge it under your own rules and file "
        "follow-up tasks for what you defer; the task stays parked until the PR is merged.",
        "- FIX - list every blocking defect with a concrete repro in the same comment. "
        "Exactly one fix round scoped to that list will be dispatched; findings you "
        "leave out are deferred.",
        "- ESCALATE - a human decision is required.",
        "",
        f"Task: {task_id} - {title}",
    ]
    return "\n".join(lines).rstrip() + "\n"


def normalize_conversation_comment(item: dict[str, Any]) -> dict[str, Any]:
    """Standardize a raw GitHub issue-comment item (or an already standard one)."""
    user = item.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    raw_id = item.get("comment_id") if "comment_id" in item else item.get("id")
    cid = str(raw_id) if raw_id is not None else ""
    if cid and not cid.startswith("issue_"):
        cid = f"issue_{cid}"
    return {
        "comment_id": cid,
        "type": "pr_comment",
        "author": user.get("login") or item.get("author") or "",
        "body": str(item.get("body") or ""),
        "created_at": item.get("created_at") or item.get("createdAt") or "",
    }


def find_request_comment(
    comments: list[dict[str, Any]] | None,
    *,
    key: str,
    head_sha: str,
    authors: set[str] | None = None,
) -> dict[str, Any] | None:
    """The request comment the dispatcher already posted for this request.

    A comment qualifies when it is authored by one of ``authors``, its first
    line is exactly ``REQUEST_PREFIX`` and it carries this request's key and
    head as whole ``REQUEST:`` / ``HEAD:`` lines.
    """
    allowed = {_normalize_login(a) for a in (authors or set())}
    wanted = {f"REQUEST: {key}", f"HEAD: {str(head_sha or '').lower()}"}
    for item in comments or []:
        if not isinstance(item, dict):
            continue
        comment = normalize_conversation_comment(item)
        if allowed and _normalize_login(comment["author"]) not in allowed:
            continue
        lines = [line.strip() for line in comment["body"].splitlines()]
        if not lines or lines[0] != REQUEST_PREFIX:
            continue
        if not wanted.issubset(set(lines)):
            continue
        return comment
    return None


def find_arbitration_verdict(
    comments: list[dict[str, Any]] | None,
    *,
    arbiter: str,
    key: str,
    request_head: str,
    requested_at: Any,
    live_head: str,
    allowed_logins: set[str] | None,
    now: Any,
    timeout_hours: int,
) -> dict[str, Any]:
    """The latest conversation comment that is a valid verdict for THIS request.

    Returns ``{"decision": <dict or None>, "rejected": [<dict>]}``. Each
    rejected entry names the comment, its author and the reason it was not
    applied, so callers can surface it once instead of re-logging every cycle.
    """
    decision: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = []
    started = _coerce_int(requested_at)
    if started <= 0:
        return {"decision": None, "rejected": rejected}
    deadline = started + int(timeout_hours) * 3600
    allowed = {_normalize_login(a) for a in (allowed_logins or set())}
    wanted_role = (arbiter or "").strip().lower()
    wanted_head = str(request_head or "").strip().lower()
    live = str(live_head or "").strip().lower()

    for item in comments or []:
        if not isinstance(item, dict):
            continue
        # Verdicts live only in PR conversation comments: an inline review
        # body or a review summary never counts, even if it carries the line.
        if item.get("type") not in (None, "pr_comment"):
            continue
        comment = normalize_conversation_comment(item)
        body = comment["body"]
        if VERDICT_PREFIX not in body.upper():
            continue
        first_line = body.strip().splitlines()[0].strip() if body.strip() else ""
        entry = {"comment_id": comment["comment_id"], "author": comment["author"]}
        match = _VERDICT_LINE_RE.match(first_line)
        if not match:
            rejected.append({**entry, "reason": "not-a-first-line-verdict"})
            continue
        if match.group("role").lower() != wanted_role:
            rejected.append({**entry, "reason": "wrong-role-tag"})
            continue
        if _normalize_login(comment["author"]) not in allowed:
            rejected.append({**entry, "reason": "wrong-author"})
            continue
        if match.group("request") != key:
            rejected.append({**entry, "reason": "request-mismatch"})
            continue
        if match.group("head").lower() != wanted_head:
            rejected.append({**entry, "reason": "head-mismatch"})
            continue
        created = parse_github_timestamp(comment["created_at"])
        if created is None:
            rejected.append({**entry, "reason": "missing-timestamp"})
            continue
        if created < started - _CLOCK_SLACK_SECONDS:
            rejected.append({**entry, "reason": "predates-request"})
            continue
        if created >= deadline:
            rejected.append({**entry, "reason": "posted-after-deadline"})
            continue
        if created > _coerce_int(now) + _CLOCK_SLACK_SECONDS:
            rejected.append({**entry, "reason": "future-dated"})
            continue
        if live != wanted_head:
            rejected.append(
                {
                    **entry,
                    "reason": "live-head-changed" if live else "live-head-unknown",
                }
            )
            continue
        decision = {
            "verdict": match.group("verdict").upper(),
            "comment_id": comment["comment_id"],
            "author": comment["author"],
            "body": body.strip(),
            "first_line": first_line,
            "rationale": "\n".join(body.strip().splitlines()[1:]).strip(),
            "created_at": created,
        }
    return {"decision": decision, "rejected": rejected}


def record_rejections(
    state: dict[str, Any], rejected: list[dict[str, Any]], now: Any
) -> list[dict[str, Any]]:
    """Remember rejected verdict comments on ``state``; return the unseen ones."""
    seen = state.get("ignored_verdicts")
    if not isinstance(seen, dict):
        seen = {}
        state["ignored_verdicts"] = seen
    fresh: list[dict[str, Any]] = []
    for entry in rejected:
        cid = str(entry.get("comment_id") or entry.get("first_line") or "")
        if not cid or cid in seen:
            continue
        seen[cid] = {
            "reason": entry.get("reason"),
            "author": entry.get("author"),
            "at": _coerce_int(now),
        }
        fresh.append(entry)
    while len(seen) > _MAX_TRACKED_REJECTIONS:
        seen.pop(next(iter(seen)))
    return fresh


def close_fix_round(task_meta: dict[str, Any] | None, now: Any) -> bool:
    """End the fix round a FIX verdict opened: a reviewer round was recorded
    on its commit, so the verdict is no longer the builder's open brief.

    Without this the brief could outlive its round - e.g. resurface after an
    operator reset of ``review_round``. Returns True when a round was open.
    """
    meta = task_meta or {}
    state = state_of(meta)
    if state.get("status") != STATUS_FIX_REQUESTED or state.get("fix_round_closed_at"):
        return False
    state["fix_round_closed_at"] = _coerce_int(now)
    meta[STATE_KEY] = state
    return True


def fix_round_brief(task_meta: dict[str, Any] | None) -> str | None:
    """The accepted FIX verdict body while the bounded fix pass it opened is live.

    Live means the request is ``fix-requested``, no reviewer round has been
    recorded since (``review_round`` / ``commit_review_count`` are absent - the
    FIX reset them) and the round was not closed by ``close_fix_round``. Once
    the reviewer answers the fix commit, ordinary review-comment routing owns
    the prompt again.
    """
    meta = task_meta or {}
    state = state_of(meta)
    if state.get("status") != STATUS_FIX_REQUESTED or state.get("fix_round_closed_at"):
        return None
    if "review_round" in meta or "commit_review_count" in meta:
        return None
    brief = str(state.get("decision_body") or "").strip()
    return brief or None


def post_arbitration_request(
    *,
    pr_url: str | None,
    repo_path: Any,
    body: str,
    key: str,
    head_sha: str,
) -> dict[str, Any] | None:
    """Post the request comment once and return its verified identity.

    Returns ``{"comment_id", "author", "created_at", "adopted"}``, or None when
    no live request exists: skip mode, no PR URL, no authenticated GitHub
    account to author the comment, or a POST that failed and could not be
    reconciled. A comment this account already posted for this request key
    (any page) is adopted instead of posting, so a crash between a successful
    POST and the local task update never produces a duplicate.
    """
    if os.environ.get("ZEROFACTORY_SKIP_GIT") or not pr_url:
        return None
    # The request is posted as - and therefore owned by - the account ``gh``
    # is authenticated as here, regardless of who may answer it. Looking for an
    # earlier copy by that author is what makes a retry after a lost local
    # update a no-op even when the arbiter allowlist is configured to others.
    owner = gh_authenticated_login(repo_path)
    if not owner:
        _log.warning(
            "Arbitration request %s: the dispatcher's GitHub account is unknown; not posting",
            key,
        )
        return None
    authors = {_normalize_login(owner)}

    def _adopt(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "comment_id": existing["comment_id"],
            "author": existing["author"],
            "created_at": parse_github_timestamp(existing.get("created_at")),
            "adopted": True,
        }

    def _existing() -> dict[str, Any] | None:
        comments = (
            fetch_pr_conversation_comments(repo_path=repo_path, pr_url=pr_url) or []
        )
        return find_request_comment(
            comments, key=key, head_sha=head_sha, authors=authors
        )

    found = _existing()
    if found is not None:
        return _adopt(found)

    author = owner
    fd, path = tempfile.mkstemp(prefix="zf-arbitration-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body.rstrip() + "\n")
        res = subprocess.run(
            ["gh", "pr", "comment", pr_url, "--body-file", path],
            cwd=str(repo_path) if repo_path else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        _log.warning(
            "Arbitration request %s: `gh pr comment` raised %s: %s",
            key,
            type(exc).__name__,
            exc,
        )
        res = None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    # The POST may have landed even when gh reports failure or prints no URL
    # (timeout after GitHub accepted it): reconcile before deciding.
    found = _existing()
    if found is not None:
        return _adopt(found)
    url_match = _COMMENT_URL_RE.search((res.stdout if res else "") or "")
    if res is None or res.returncode != 0 or not url_match:
        _log.warning(
            "Arbitration request %s: `gh pr comment` did not produce a comment (rc=%s): %s",
            key,
            res.returncode if res else "raised",
            (res.stderr if res else "").strip()[:300],
        )
        return None
    return {
        "comment_id": f"issue_{url_match.group(1)}",
        "author": author,
        "created_at": None,
        "adopted": False,
    }
