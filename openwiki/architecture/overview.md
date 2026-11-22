---
type: architecture
title: System architecture
description: Explains Zero Factory's plugin entrypoint, dashboard API and SQLite persistence, and dispatcher responsibilities and data flow.
tags: [architecture, plugin, persistence, dispatcher]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-22T19:30:17.111Z
sources:
  - id: openwiki-source-594f18a4ed0f4f061e35fdc9
    resource: repo://dashboard/db.py
  - id: openwiki-source-36474181a50218a7c6a546cf
    resource: repo://dispatcher/__init__.py
generated: { by: "hermes", at: "2026-11-22T19:30:17.111Z" }
---

# System architecture

Zero Factory is a Hermes plugin whose Python package entrypoint (`__init__.py`) exposes CLI operations and plugin registration. The dashboard backend in `dashboard/plugin_api.py` provides the REST-facing service layer; it delegates persistence to `dashboard/db.py` and coordinates task operations with the dispatcher. The React dashboard is served from the embedded dashboard assets.

## Durable task state

The dashboard database layer owns SQLite connection setup, schema initialization/migrations, and durable task, board, activity, and settings data. `get_db_conn()` enables foreign-key checks and a busy timeout. Database paths are resolved through the shared paths module, allowing tests and runtime configuration to select isolated storage. JSON is used for structured fields while relational tables provide the durable substrate.

## Dispatch and worker execution

The dispatcher is the orchestration engine. Its scheduler selects eligible Todo tasks subject to board and global capacity limits, and resolves the task stage/assignee contract before execution. Work is isolated in per-task Git worktrees. Worker processes run specialized Hermes profiles; dispatcher components track lifecycle, collect results, run deterministic packaging/precommit, and route pull requests through reviewer feedback. Recovery and reaping paths return interrupted work to a safe lifecycle state rather than treating worker processes as durable state.

## Boundaries

- CLI and dashboard are entry surfaces; task state is persisted in SQLite.
- Dispatcher owns execution, worktree and worker lifecycle, and GitHub handoffs; it is not the source of durable task state.
- Settings and path helpers centralize runtime configuration and profile/storage locations.
- Tests are organized by unit, integration, and end-to-end behavior; database-backed tests use isolated test configuration.

## Source evidence

See `__init__.py` for the plugin/CLI entrypoint, `dashboard/db.py` for persistence setup, `dashboard/plugin_api.py` for API service composition, and `dispatcher/__init__.py` plus its submodules for orchestration.
