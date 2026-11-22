---
type: conventions
title: Engineering conventions
description: Summarizes shared configuration and path ownership, hermetic pytest patterns, and cross-platform implementation constraints.
tags: [conventions, testing, configuration, portability]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-22T19:30:17.111Z
sources:
  - id: openwiki-source-0d153fd68e7210eb5d3be49d
    resource: repo://paths.py
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
  - id: openwiki-source-97796927cb8e70471c35e328
    resource: repo://zf_wincompat.py
generated: { by: "hermes", at: "2026-11-22T19:30:17.111Z" }
---

# Engineering conventions

## Shared sources of truth

Use `paths.py` for canonical profile identity, assignee normalization, and profile/storage path resolution rather than duplicating mappings in CLI, dashboard, or dispatcher code. Runtime settings are centralized in `settings.py`. Keep task lifecycle rules in the dispatcher state machine and use the dashboard database/service layer for durable records.

## Tests

The supported test runner is pytest. `tests/conftest.py` establishes hermetic defaults and provides fixtures for isolated environments, temporary database paths, initialized schemas, and temporary Git repositories. Prefer focused unit tests for local behavior, integration tests for REST/API interactions, and end-to-end tests for workflow contracts. Tests should avoid real worker launches and external GitHub/network effects unless explicitly testing those boundaries.

## Portability

Platform-specific process and file-lock behavior belongs in the compatibility layer (`zf_wincompat.py`) and should be gated by the operating system. Preserve equivalent semantics across POSIX and Windows; notably Windows process liveness must use a non-signaling kernel probe, not `os.kill(pid, 0)`. Avoid introducing platform-specific assumptions into shared orchestration paths.

## Configuration and generated assets

Resolve runtime locations through shared path helpers and use documented environment overrides for test isolation. The dashboard stylesheet is generated from its source/build script; update sources and rebuild rather than editing generated output directly. Keep documentation lifecycle artifacts under OpenWiki management.
