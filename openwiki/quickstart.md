---
type: guide
title: Repository quickstart
description: Entry point for Zero Factory agents, routing to architecture, task workflows, conventions, and verification instructions.
tags: [quickstart, navigation, development]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-22T19:30:17.111Z
sources:
  - id: openwiki-source-4942bcbe129130ccad2b7e2a
    resource: repo://__init__.py
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
generated: { by: "hermes", at: "2026-11-22T19:30:17.111Z" }
---

# Repository quickstart

Zero Factory is a Hermes plugin that coordinates specialized agents to implement, test, and review software changes. Begin with the page matching the task:

- System components and persistence boundaries: [System architecture](architecture/overview.md).
- Task statuses, dispatch, worker lifecycle, and PR feedback: [Task lifecycle and dispatch](architecture/task-lifecycle.md).
- Testing, precommit, and dashboard builds: [Development and verification](operations/development.md).
- Shared code, test, and portability patterns: [Engineering conventions](conventions.md).

## Essential checks

From the repository root, run `./.zerofactory/precommit.sh` to execute formatting/linting, Python compilation, and pytest. For dashboard source or asset changes, also run `npm run build` as appropriate. See `README.md` for user-facing installation and CLI documentation; inspect current source and focused tests when a contract is uncertain.

## Repository map

`__init__.py` is the plugin and CLI entrypoint. `dashboard/` contains the API, persistence layer, and UI. `dispatcher/` owns task scheduling and execution orchestration. `scripts/` contains automation helpers. `tests/` is split into unit, integration, and end-to-end suites.
