---
type: workflow
title: Task lifecycle and dispatch
description: Describes legal task status transitions and how dispatch, worker cleanup, and recovery coordinate execution.
tags: [workflow, tasks, dispatch, recovery]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-22T19:30:17.111Z
sources:
  - id: openwiki-source-1cbc0eb99c8b57cd43185064
    resource: repo://dispatcher/state_machine.py
generated: { by: "hermes", at: "2026-11-22T19:30:17.111Z" }
---

# Task lifecycle and dispatch

Task status transitions are centralized in `dispatcher/state_machine.py`. The canonical statuses are triage, todo, running, blocked, and done; system and agent actors are checked against an explicit transition table. A transition away from running tears down the old worker process before atomically updating SQLite task state and activity metadata. Human actors have broader transition authority and clear stale retry/block metadata.

## Dispatch flow

`dispatcher/scheduler.py` runs a dispatch cycle that identifies eligible work, checks board/global capacity, and routes tasks to their stage owner. Worker spawning provisions isolated task worktrees and invokes the appropriate Hermes profile. Worker lifecycle is tracked outside the durable task record through process/session metadata and monitored for completion, timeout, and inactivity.

## Packaging and review

A successful builder phase enters deterministic packaging: precommit and diff checks run before branch/PR handoff. The dispatcher polls PR status and routes review feedback back to the builder, or approved work to the human merge gate. Packaging failures return to implementation with captured diagnostic context; retry exhaustion uses recovery or blocking paths.

## Reaping and recovery

`dispatcher/reaper.py` detects dead or stuck workers and reconciles task/session metadata so orphan processes do not remain authoritative. The reaper and scheduler are coordinated with dispatcher locking and task-state transitions, preserving safe retries instead of leaving abandoned running records.

## Source evidence

- `dispatcher/state_machine.py` defines statuses, allowed transitions, process teardown, and atomic state updates.
- `dispatcher/scheduler.py` owns dispatch cycles and packaging/PR routing.
- `dispatcher/reaper.py` detects and repairs stuck worker state.
- `dispatcher/worker_spawner.py` provisions and starts role-specific workers.
