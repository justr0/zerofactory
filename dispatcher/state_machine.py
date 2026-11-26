"""Centralized task state machine governing all lifecycle transitions."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional

from .config import resolve_status_assignee
from .process_manager import cleanup_process_artifacts, terminate_task_worker

try:
    from ..task_lifecycle import clear_task_block, record_task_block
except ImportError:
    from task_lifecycle import clear_task_block, record_task_block

VALID_TRANSITIONS: dict[str, set[str]] = {
    "triage": {"todo", "running", "blocked", "done"},
    "todo": {"running", "blocked", "triage"},
    "running": {"todo", "triage", "running", "blocked", "done"},
    "blocked": {"todo", "triage", "running", "done"},
    "done": {"triage"},
}

KNOWN_STATUSES = {"triage", "todo", "running", "blocked", "done"}


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal status transition is requested."""

    pass


def transition_task(
    conn: sqlite3.Connection,
    task_id: str,
    target_status: str,
    target_assignee: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    metadata_patch: Optional[Dict[str, Any]] = None,
    worker_pid: Optional[int] = None,
    started_at: Optional[int] = None,
    actor: str = "system",
    comment: Optional[str] = None,
    commit: bool = True,
    allow_todo_to_done: bool = False,
) -> dict[str, Any]:
    """Execute a task state transition with process teardown and atomic DB commit.

    Protocol:
    1. Validate transition legality against VALID_TRANSITIONS.
    2. Phase 1: OS resource teardown (outside DB transaction).
       If exiting running or switching workers within running, terminate old worker.
    3. Phase 2: Atomic SQLite update (status, assignee, worker_pid, started_at,
       blocked_reason, metadata, task_activity log, and optional comment).
       metadata_patch keys set to None delete the key from the stored
       metadata (so callers can clear stale state such as
       worker_failure_retries / last_worker_failure / permanently_blocked);
       other values are merged in as usual. Set commit=False when composing
       this transition with other writes in the caller's transaction.
    """
    if target_status not in KNOWN_STATUSES:
        raise InvalidStateTransitionError(f"Unknown status '{target_status}'")

    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, assignee, worker_pid, started_at, blocked_reason, metadata FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"Task '{task_id}' not found")

    if hasattr(row, "keys"):
        current_status = row["status"]
        current_assignee = row["assignee"]
        current_worker_pid = row["worker_pid"]
        current_started_at = row["started_at"]
        current_blocked_reason = row["blocked_reason"]
        raw_meta = row["metadata"]
    else:
        (
            current_status,
            current_assignee,
            current_worker_pid,
            current_started_at,
            current_blocked_reason,
            raw_meta,
        ) = row

    is_human = actor not in ("system", "dispatcher") and not actor.startswith("zf-")

    # Check validity for automated system/agent actors
    if not is_human:
        allowed = VALID_TRANSITIONS.get(current_status, set())
        is_terminal_pr_completion = (
            allow_todo_to_done and current_status == "todo" and target_status == "done"
        )
        if (
            target_status != current_status
            and target_status not in allowed
            and not is_terminal_pr_completion
        ):
            raise InvalidStateTransitionError(
                f"Cannot transition task '{task_id}' from '{current_status}' to '{target_status}'. "
                f"Allowed target states: {sorted(list(allowed))}"
            )

    # Phase 1: OS Resource Teardown
    is_exiting_running = current_status == "running" and target_status != "running"
    is_switching_worker_in_running = (
        current_status == "running"
        and target_status == "running"
        and worker_pid is not None
        and worker_pid != current_worker_pid
    )

    if is_exiting_running or is_switching_worker_in_running:
        if current_worker_pid:
            terminate_task_worker(task_id, current_worker_pid)
        else:
            cleanup_process_artifacts(task_id)
    elif target_status != "running":
        cleanup_process_artifacts(task_id)

    # Phase 2: Metadata & Column Updates
    meta: dict[str, Any] = {}
    try:
        meta = (
            json.loads(raw_meta or "{}")
            if isinstance(raw_meta, str)
            else dict(raw_meta or {})
        )
    except Exception:
        meta = {}
    original_meta = meta.copy()

    if metadata_patch:
        for key, value in metadata_patch.items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value

    if is_human:
        meta.pop("last_worker_failure", None)
        if not (
            target_status == "blocked"
            and metadata_patch
            and metadata_patch.get("permanently_blocked") is True
        ):
            meta.pop("permanently_blocked", None)
        meta.pop("worker_failure_retries", None)
        meta.pop("conflict_retries", None)
        meta.pop("precommit_retries", None)

    if target_status == "todo":
        meta.pop("awaiting_pr", None)
        meta.pop("close_pr", None)
        meta.pop("awaiting_interview", None)
        meta.pop("reply_interview", None)
    if target_status != "running" or worker_pid is None:
        meta.pop("worker_execution_id", None)

    reason_in_patch = (
        metadata_patch.get("blocked_reason")
        if (
            metadata_patch
            and isinstance(metadata_patch.get("blocked_reason"), str)
            and metadata_patch.get("blocked_reason")
            != original_meta.get("blocked_reason")
        )
        else None
    )
    reason_to_record = blocked_reason if blocked_reason is not None else reason_in_patch
    if target_status == "blocked":
        if reason_to_record is not None:
            new_blocked_reason = reason_to_record
            meta["blocked_reason"] = reason_to_record
        elif current_status == "blocked":
            new_blocked_reason = current_blocked_reason
        else:
            # Never carry a reason from an earlier lifecycle into a new block.
            new_blocked_reason = None
            meta.pop("blocked_reason", None)
            meta.pop("blocked_reason_type", None)
    else:
        new_blocked_reason = None
        meta.pop("blocked_reason", None)
        meta.pop("blocked_reason_type", None)

    if current_status == "blocked" and target_status != "blocked":
        meta.pop("worker_failure_retries", None)
        meta.pop("last_worker_failure", None)
        meta.pop("permanently_blocked", None)

    if is_exiting_running:
        new_worker_pid = None
        new_started_at = None
        meta.pop("worker_pid", None)
        meta.pop("session_id", None)
        meta.pop("started_at", None)
    elif target_status == "running":
        new_worker_pid = worker_pid
        new_started_at = started_at
        if new_worker_pid is not None:
            meta["worker_pid"] = new_worker_pid
        else:
            meta.pop("worker_pid", None)
            meta.pop("session_id", None)
        if new_started_at is not None:
            meta["started_at"] = new_started_at
        else:
            meta.pop("started_at", None)
    else:
        new_worker_pid = None
        new_started_at = None
        meta.pop("worker_pid", None)
        meta.pop("session_id", None)
        meta.pop("started_at", None)

    new_assignee = resolve_status_assignee(
        target_status,
        current_assignee=current_assignee,
        target_assignee=target_assignee,
        blocked_reason_type=meta.get("blocked_reason_type"),
        triage_phase=meta.get("triage_phase"),
    )
    if target_status != "todo" or new_assignee != "zf-reviewer":
        meta.pop("reviewer_orphan_recovered", None)
    if target_status == "todo" and new_assignee == "zf-builder":
        meta.pop("awaiting_approval", None)
        if meta.get("triage_phase") in (
            "elephant_drafting",
            "goldfish_auditing",
            "human_approval",
            "recovery",
        ):
            meta.pop("triage_phase", None)
    elif target_status == "blocked" and meta.get("triage_phase") == "recovery":
        meta.pop("triage_phase", None)
        meta["orchestrator_recovery_count"] = max(
            1, int(meta.get("orchestrator_recovery_count", 0))
        )
    now = int(time.time())

    cursor.execute(
        """
        UPDATE tasks
        SET status = ?, assignee = ?, worker_pid = ?, started_at = ?, metadata = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            target_status,
            new_assignee,
            new_worker_pid,
            new_started_at,
            json.dumps(meta),
            now,
            task_id,
        ),
    )

    if target_status == "blocked":
        if current_status != "blocked" or reason_to_record is not None:
            record_task_block(conn, task_id, actor, new_blocked_reason, now)
    else:
        clear_task_block(conn, task_id)

    cursor.execute(
        """
        INSERT INTO task_activity (task_id, actor, action, details, created_at)
        VALUES (?, ?, 'status_change', ?, ?)
        """,
        (
            task_id,
            actor,
            f"{current_status} -> {target_status}",
            now,
        ),
    )

    if comment:
        cursor.execute(
            """
            INSERT INTO task_comments (task_id, author, body, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                task_id,
                actor,
                comment,
                now,
            ),
        )

    if commit:
        conn.commit()

    return {
        "id": task_id,
        "status": target_status,
        "prev_status": current_status,
        "assignee": new_assignee,
        "blocked_reason": new_blocked_reason,
        "worker_pid": new_worker_pid,
        "started_at": new_started_at,
        "metadata": meta,
        "updated_at": now,
    }
