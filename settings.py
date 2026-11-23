"""Zero Factory — shared global-settings definitions and parsing.

Single source of truth for the global settings keys persisted in the
``settings`` table:

* their default values,
* the seed rows written on first ``init_db()``, and
* the parse / clamp / fallback logic shared by the dashboard API
  (``dashboard/plugin_api.py``) and the dispatcher (``dispatcher.py``).

Both consumers call :func:`load_settings` instead of re-implementing the
key-lookup + clamp + fallback pattern, so the defaults and the clamping
semantics can no longer drift between the two modules.

Idle-scan settings
------------------
Scanner configuration is read separately by the dispatcher via
``get_scanner_cron_config()`` and is **not** returned by
:func:`load_settings`. Cooldown and backlog defaults are defined below.

Unit boundary
-------------
``idle_scan_cooldown_minutes`` is stored in **minutes** (the DB/API unit).
The dispatcher is the only consumer that needs seconds and converts at a
single, explicit site (``cooldown_seconds = cooldown_minutes * 60`` in
``dispatcher/scheduler.py``). :const:`DEFAULT_IDLE_SCAN_COOLDOWN_SECONDS`
exposes the seconds view of the default so that fallback path reads
naturally.
"""

from __future__ import annotations

from typing import Any

# --- Default values (source of truth) ---------------------------------------

# Kanban WIP limit: max total active tasks across all boards in 'running'.
# Controls direct todo->running dispatch and worktree pre-provisioning to avoid queue flooding.
DEFAULT_MAX_ACTIVE_TASKS = 10

# Global cap on concurrent LLM workers across boards (tasks and scans).
# Keep the task WIP limit independent from the process capacity limit.
DEFAULT_MAX_CONCURRENT_LLM_WORKERS = 10

# Fallback cap for concurrent active agent worker subprocesses ('running').
# Defaults to 1; customized per board via boards.max_concurrent_running.
DEFAULT_MAX_CONCURRENT_WORKERS = 1


# Minimum cooldown between idle improvement scans for the same board, in MINUTES
# (the DB storage unit). See DEFAULT_IDLE_SCAN_COOLDOWN_SECONDS for the seconds view.
DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES = 15

# Max 'todo' backlog tasks allowed on a board before idle scans are suppressed.
DEFAULT_IDLE_SCAN_MAX_TODO = 2

# Retention window (days) for the append-only ``task_activity`` log. Rows older
# than this are pruned by ``dashboard.plugin_api.prune_old_activity``. A value of
# ``0`` disables pruning (the table then grows without bound). Kept >= 1 by the
# parser when set via the API so a stray "0" can't accidentally delete everything.
DEFAULT_ACTIVITY_RETENTION_DAYS = 7

# Non-destructive archive window (days) for completed ('done') tasks in the Kanban
# Done column. Tasks older than this are collapsed into an expandable archive section
# while keeping all task rows, comments, PR links, and history intact. 0 disables.
DEFAULT_DONE_ARCHIVE_DAYS = 7

# Enable periodic background cron scheduler execution (dispatcher ticker & scheduled runs).
DEFAULT_ENABLE_CRON_SCHEDULER = True

# Automatically create a public GitHub issue on hotcode-dev/zerofactory when internal defects are reported.
DEFAULT_AUTO_CREATE_GITHUB_ISSUES = False

# Allow workers/scanners to pass --yolo and --accept-hooks unattended (default: False).
DEFAULT_ALLOW_UNATTENDED_APPROVALS = False

# Automatically start the background dispatcher during plugin register() (default: False).
DEFAULT_AUTO_START_DISPATCHER = False

# Langfuse observability settings
DEFAULT_LANGFUSE_ENABLED = False
DEFAULT_LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
DEFAULT_LANGFUSE_PUBLIC_KEY = ""
DEFAULT_LANGFUSE_SECRET_KEY = ""
DEFAULT_LANGFUSE_CAPTURE_MODE = "sanitized"
DEFAULT_LANGFUSE_ENV = "zerofactory"

# Continuous Learning & Memory settings
# When enabled, reviewer feedback and rejections with gotchas/conventions are automatically
# persisted to board_memories for that repository.
DEFAULT_AUTO_RECORD_MEMORY = True
DEFAULT_MEM0_ENABLED = False
DEFAULT_MEM0_BASE_URL = ""
DEFAULT_MEM0_API_KEY = ""
DEFAULT_MEM0_MODEL = ""
DEFAULT_MEM0_EMBEDDER_PROVIDER = "fastembed"
DEFAULT_MEM0_EMBEDDER_MODEL = "BAAI/bge-small-en-v1.5"

# Maximum wall-clock execution duration (in seconds) before an active task worker is terminated as stuck.
DEFAULT_TASK_TIMEOUT_SECONDS = 3600  # 1 hour max running time

# Maximum idle duration (in seconds) with no log output or session updates before an agent worker is reaped.
DEFAULT_INACTIVITY_TIMEOUT_SECONDS = 900  # 15 mins with no log/session update

# Seconds view of the cooldown default — the single unit-conversion point for the
# default path. The DB stores minutes; the dispatcher converts to seconds where needed.
DEFAULT_IDLE_SCAN_COOLDOWN_SECONDS = DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES * 60

# Canonical list of the global settings keys (order is only for readability/seed order).
SETTING_KEYS = (
    "max_active_tasks",
    "max_concurrent_llm_workers",
    "activity_retention_days",
    "done_archive_days",
    "task_timeout_seconds",
    "inactivity_timeout_seconds",
    "enable_cron_scheduler",
    "langfuse_enabled",
    "langfuse_base_url",
    "langfuse_public_key",
    "langfuse_secret_key",
    "langfuse_capture_mode",
    "langfuse_env",
    "auto_record_memory",
    "mem0_enabled",
    "mem0_base_url",
    "mem0_api_key",
    "mem0_model",
    "mem0_embedder_provider",
    "mem0_embedder_model",
    "auto_create_github_issues",
    "allow_unattended_approvals",
    "auto_start_dispatcher",
)

# Seed values for the settings table, derived from the constants above (NOT
# string literals) so the seed rows and the in-code defaults can never drift.
DEFAULT_SETTING_VALUES: dict[str, str] = {
    "max_active_tasks": str(DEFAULT_MAX_ACTIVE_TASKS),
    "max_concurrent_llm_workers": str(DEFAULT_MAX_CONCURRENT_LLM_WORKERS),
    "activity_retention_days": str(DEFAULT_ACTIVITY_RETENTION_DAYS),
    "done_archive_days": str(DEFAULT_DONE_ARCHIVE_DAYS),
    "task_timeout_seconds": str(DEFAULT_TASK_TIMEOUT_SECONDS),
    "inactivity_timeout_seconds": str(DEFAULT_INACTIVITY_TIMEOUT_SECONDS),
    "enable_cron_scheduler": "true" if DEFAULT_ENABLE_CRON_SCHEDULER else "false",
    "auto_create_github_issues": "true"
    if DEFAULT_AUTO_CREATE_GITHUB_ISSUES
    else "false",
    "allow_unattended_approvals": "true"
    if DEFAULT_ALLOW_UNATTENDED_APPROVALS
    else "false",
    "auto_start_dispatcher": "true" if DEFAULT_AUTO_START_DISPATCHER else "false",
    "langfuse_enabled": "true" if DEFAULT_LANGFUSE_ENABLED else "false",
    "langfuse_base_url": DEFAULT_LANGFUSE_BASE_URL,
    "langfuse_public_key": DEFAULT_LANGFUSE_PUBLIC_KEY,
    "langfuse_secret_key": DEFAULT_LANGFUSE_SECRET_KEY,
    "langfuse_capture_mode": DEFAULT_LANGFUSE_CAPTURE_MODE,
    "langfuse_env": DEFAULT_LANGFUSE_ENV,
    "auto_record_memory": "true" if DEFAULT_AUTO_RECORD_MEMORY else "false",
    "mem0_enabled": "true" if DEFAULT_MEM0_ENABLED else "false",
    "mem0_base_url": DEFAULT_MEM0_BASE_URL,
    "mem0_api_key": DEFAULT_MEM0_API_KEY,
    "mem0_model": DEFAULT_MEM0_MODEL,
    "mem0_embedder_provider": DEFAULT_MEM0_EMBEDDER_PROVIDER,
    "mem0_embedder_model": DEFAULT_MEM0_EMBEDDER_MODEL,
}


def _clamp_int(value: Any, lower: int) -> Any:
    """Coerce ``value`` to ``int`` clamped to ``lower``; return ``None`` on failure."""
    try:
        return max(lower, int(value))
    except (TypeError, ValueError):
        return None


def _parse_bool(value: Any) -> bool:
    """Parse a stored boolean ('true'/'1'/'yes' are truthy). Never raises."""
    return str(value).strip().lower() in ("true", "1", "yes")


_INT_SETTING_MINIMUMS = {
    "max_active_tasks": 1,
    "max_concurrent_llm_workers": 1,
    "activity_retention_days": 1,
    "done_archive_days": 0,
    "task_timeout_seconds": 1,
    "inactivity_timeout_seconds": 1,
}
_BOOL_SETTING_KEYS = {
    "enable_cron_scheduler",
    "langfuse_enabled",
    "auto_record_memory",
    "mem0_enabled",
    "auto_create_github_issues",
    "allow_unattended_approvals",
    "auto_start_dispatcher",
}
_STRIPPED_SETTING_KEYS = {
    "langfuse_base_url",
    "langfuse_public_key",
    "langfuse_secret_key",
    "mem0_base_url",
    "mem0_api_key",
}
_NONEMPTY_SETTING_KEYS = {"langfuse_env", "mem0_model", "mem0_embedder_model"}
_ENUM_SETTING_VALUES = {
    "langfuse_capture_mode": {"sanitized", "metadata", "full"},
    "mem0_embedder_provider": {"fastembed", "openai"},
}


def _setting_row_values(row: Any) -> tuple[Any, Any] | None:
    try:
        return row["key"], row["value"]
    except Exception:
        try:
            return row[0], row[1]
        except Exception:
            return None


def _apply_setting(settings: dict[str, Any], key: Any, value: Any) -> None:
    if key in _INT_SETTING_MINIMUMS:
        parsed = _clamp_int(value, _INT_SETTING_MINIMUMS[key])
        if parsed is not None:
            settings[key] = parsed
    elif key in _BOOL_SETTING_KEYS:
        settings[key] = _parse_bool(value)
    elif key in _STRIPPED_SETTING_KEYS and value is not None:
        settings[key] = str(value).strip()
    elif key in _NONEMPTY_SETTING_KEYS and value is not None and str(value).strip():
        settings[key] = str(value).strip()
    elif key in _ENUM_SETTING_VALUES:
        parsed = str(value).strip().lower() if value is not None else ""
        if parsed in _ENUM_SETTING_VALUES[key]:
            settings[key] = parsed


def load_settings(conn_or_cursor: Any) -> dict[str, Any]:
    """Load all global settings from the ``settings`` table.

    Accepts a ``sqlite3`` connection or cursor. Every key falls back to its
    module default when the row is missing or its value cannot be parsed,
    preserving the clamping semantics (``max(1, ...)`` /
    ``max(0, ...)``). Unknown keys in the table are ignored.

    Returns a dict with exactly the :data:`SETTING_KEYS` in their native
    types. Scanner settings are **not** included — they are read separately
    from scanner configuration by the dispatcher.
    """
    settings: dict[str, Any] = {
        "max_active_tasks": DEFAULT_MAX_ACTIVE_TASKS,
        "max_concurrent_llm_workers": DEFAULT_MAX_CONCURRENT_LLM_WORKERS,
        "activity_retention_days": DEFAULT_ACTIVITY_RETENTION_DAYS,
        "done_archive_days": DEFAULT_DONE_ARCHIVE_DAYS,
        "task_timeout_seconds": DEFAULT_TASK_TIMEOUT_SECONDS,
        "inactivity_timeout_seconds": DEFAULT_INACTIVITY_TIMEOUT_SECONDS,
        "enable_cron_scheduler": DEFAULT_ENABLE_CRON_SCHEDULER,
        "langfuse_enabled": DEFAULT_LANGFUSE_ENABLED,
        "langfuse_base_url": DEFAULT_LANGFUSE_BASE_URL,
        "langfuse_public_key": DEFAULT_LANGFUSE_PUBLIC_KEY,
        "langfuse_secret_key": DEFAULT_LANGFUSE_SECRET_KEY,
        "langfuse_capture_mode": DEFAULT_LANGFUSE_CAPTURE_MODE,
        "langfuse_env": DEFAULT_LANGFUSE_ENV,
        "auto_record_memory": DEFAULT_AUTO_RECORD_MEMORY,
        "mem0_enabled": DEFAULT_MEM0_ENABLED,
        "mem0_base_url": DEFAULT_MEM0_BASE_URL,
        "mem0_api_key": DEFAULT_MEM0_API_KEY,
        "mem0_model": DEFAULT_MEM0_MODEL,
        "mem0_embedder_provider": DEFAULT_MEM0_EMBEDDER_PROVIDER,
        "mem0_embedder_model": DEFAULT_MEM0_EMBEDDER_MODEL,
        "auto_create_github_issues": DEFAULT_AUTO_CREATE_GITHUB_ISSUES,
        "allow_unattended_approvals": DEFAULT_ALLOW_UNATTENDED_APPROVALS,
        "auto_start_dispatcher": DEFAULT_AUTO_START_DISPATCHER,
    }
    try:
        rows = conn_or_cursor.execute("SELECT key, value FROM settings").fetchall()
    except Exception:
        # No settings table (minimal DB) — return defaults unchanged.
        return settings

    for row in rows:
        values = _setting_row_values(row)
        if values is None:
            continue
        k, v = values
        _apply_setting(settings, k, v)

    return settings
