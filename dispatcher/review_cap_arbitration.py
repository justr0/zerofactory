"""Review-cap arbitration: a delegate decides when builder and reviewer disagree.

Without this module a task whose reviewer requests changes in
``ZEROFACTORY_MAX_REVIEW_ROUNDS`` consecutive rounds is parked for a human
(``assignee='human'``, ``status='blocked'``) and nothing moves until a human
answers. Arbitration adds one step before that park becomes final: the
dispatcher requests a decision on the PR from an arbiter profile (default
``zf-gatekeeper``, the profile that already runs the merge sweeps).

Protocol - every message is a PR conversation comment, so the decision trail
stays on the PR:

    dispatcher  [AI:zf-dispatcher] Review-cap arbitration requested (...)
    arbiter     [AI:<arbiter>] ARBITRATION VERDICT: MERGE | FIX | ESCALATE

    MERGE      only minor findings remain. The arbiter merges the PR under its
               own merge rules and files follow-up tasks; the dispatcher only
               records the verdict and completes the task once the PR is merged.
    FIX        the verdict lists the blocking defects with a repro. The
               dispatcher grants ONE bounded fix round on the same task with a
               fresh review budget, instead of re-queuing the builder blind.
    ESCALATE   a genuine human decision is needed; the task stays parked.

Bounds, so this can never turn into a longer loop:
  * at most ``ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS`` handoffs per task
  * an unanswered request older than
    ``ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS`` is left parked
  * tasks tagged ``hitl`` / ``manual-review`` are never arbitrated
  * no arbiter profile on this host -> no request is ever posted

Every knob is an environment variable, exactly like ``ZEROFACTORY_MAX_REVIEW_ROUNDS``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

STATE_KEY = "review_cap_arbitration"
DEFAULT_ARBITER = "zf-gatekeeper"

STATUS_REQUESTED = "requested"
STATUS_FIX_REQUESTED = "fix-requested"
STATUS_MERGE_APPROVED = "merge-approved"
STATUS_ESCALATED = "escalated"
STATUS_EXPIRED = "expired"

VERDICT_MERGE = "MERGE"
VERDICT_FIX = "FIX"
VERDICT_ESCALATE = "ESCALATE"

VERDICT_PREFIX = "ARBITRATION VERDICT:"
REQUEST_PREFIX = "[AI:zf-dispatcher] Review-cap arbitration requested"

# A verdict must open a line and carry the arbiter's own role tag; anything
# else on the PR (review rounds, human chatter, a quoted verdict) is not one.
_VERDICT_RE = re.compile(
    r"^\[AI:(?P<role>[A-Za-z0-9_.-]+)\]\s*ARBITRATION\s+VERDICT:\s*"
    r"(?P<verdict>MERGE|FIX|ESCALATE)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Same opt-out vocabulary as github_pr.is_task_automerge_enabled.
_OPT_OUT_TAGS = {"hitl", "manual-review", "tag:hitl", "tag:manual-review"}


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


def arbitration_settings() -> dict[str, Any]:
    """Env-driven knobs; nothing here needs dashboard plumbing."""
    arbiter = (os.environ.get("ZEROFACTORY_REVIEW_CAP_ARBITER") or "").strip()
    return {
        "enabled": _env_flag("ZEROFACTORY_REVIEW_CAP_ARBITRATION", True),
        "arbiter": arbiter or DEFAULT_ARBITER,
        "timeout_hours": _env_int(
            "ZEROFACTORY_REVIEW_CAP_ARBITRATION_TIMEOUT_HOURS", 6, minimum=1
        ),
        "max_arbitrations": _env_int(
            "ZEROFACTORY_REVIEW_CAP_MAX_ARBITRATIONS", 2, minimum=1
        ),
    }


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


def parse_arbitration_decision(
    comments: list[dict[str, Any]] | None, arbiter: str
) -> dict[str, Any] | None:
    """The latest verdict comment from the configured arbiter wins."""
    decision = None
    for c in comments or []:
        body = str((c or {}).get("body") or "")
        m = _VERDICT_RE.search(body)
        if not m or m.group("role").lower() != (arbiter or "").strip().lower():
            continue
        decision = {
            "verdict": m.group("verdict").upper(),
            "comment_id": (c or {}).get("comment_id"),
            "author": (c or {}).get("author"),
            "body": body,
            "rationale": body[m.end() :].strip().lstrip("-:").strip(),
        }
    return decision


def build_arbitration_request(
    task_meta: dict[str, Any] | None,
    *,
    arbiter: str,
    now: int,
    head_sha: str,
    rounds: int,
    max_arbitrations: int,
) -> dict[str, Any] | None:
    """State for a new handoff, or None once the per-task budget is used up."""
    prev = state_of(task_meta)
    attempts = _coerce_int(prev.get("attempts"))
    if attempts >= max_arbitrations:
        return None
    return {
        "status": STATUS_REQUESTED,
        "arbiter": arbiter,
        "attempts": attempts + 1,
        "max_arbitrations": max_arbitrations,
        "requested_at": int(now),
        "request_head": head_sha,
        "rounds": rounds,
    }


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def arbitration_is_stale(state: dict[str, Any], now: int, timeout_hours: int) -> bool:
    if not state or state.get("status") != STATUS_REQUESTED:
        return False
    requested_at = _coerce_int(state.get("requested_at"))
    return requested_at > 0 and (int(now) - requested_at) >= timeout_hours * 3600


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
    lines = [
        f"{REQUEST_PREFIX} (attempt {attempt}/{max_arbitrations})",
        "",
        (
            f"The reviewer has requested changes for {rounds} consecutive review "
            "round(s), so the dispatcher stopped the build/review loop instead of "
            "starting another one."
        ),
        f"@{arbiter}: decide this PR against its task spec and answer with exactly "
        "one verdict comment on the current head:",
        "",
        f"- `[AI:{arbiter}] ARBITRATION VERDICT: MERGE` - only minor findings remain. "
        "Merge it and file follow-up tasks for what you defer.",
        f"- `[AI:{arbiter}] ARBITRATION VERDICT: FIX` - list every blocking defect with "
        "a concrete repro. Exactly one fix round scoped to that list will be dispatched.",
        f"- `[AI:{arbiter}] ARBITRATION VERDICT: ESCALATE` - a human decision is required.",
        "",
        f"Task: {task_id} - {title}",
        f"Head: {(head_sha or '')[:12] or 'unknown'}",
    ]
    return "\n".join(lines)


def post_arbitration_request(pr_url: str, repo_path: Any, body: str) -> bool:
    """Best-effort PR comment; the board records the request either way."""
    if os.environ.get("ZEROFACTORY_SKIP_GIT") or not pr_url:
        return False
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
        return res.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
