---
type: operations
title: Development and verification
description: Gives the supported repository precommit and dashboard build commands, and explains what each verifies.
tags: [operations, development, testing, build]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-22T19:30:17.111Z
sources:
  - id: openwiki-source-9ab161c6e9774cf771b19ced
    resource: repo://.zerofactory/precommit.sh
  - id: openwiki-source-5b54a58d1b51cd490b0e7162
    resource: repo://package.json
generated: { by: "hermes", at: "2026-11-22T19:30:17.111Z" }
---

# Development and verification

## Precommit gate

Run `./.zerofactory/precommit.sh` from the repository root for the deterministic format, build, and test sequence. The format phase requires Ruff and applies its lint/format checks. The build phase compiles Python sources with `compileall`; no additional backend build system is defined. The test phase runs the pytest suite in `tests/` with the pinned runtime test dependencies. The script supports individual `format`, `build`, and `test` phases for focused validation.

## Dashboard assets

The dashboard has a Vite build and a portable Tailwind stylesheet build. `npm run build` rebuilds the dashboard bundle and stylesheet; `npm run build:css` rebuilds only CSS. The Python precommit gate does not invoke these optional Node build steps, so dashboard source changes should verify the appropriate npm build separately.

## Test organization

Use `tests/unit/` for isolated module behavior, `tests/integration/` for API boundaries, and `tests/e2e/` for broader workflow behavior. `tests/conftest.py` provides hermetic defaults and reusable temporary database and Git fixtures.

## Source evidence

The authoritative gate is `.zerofactory/precommit.sh`; dashboard scripts and dependencies are defined in `package.json`; test setup is in `tests/conftest.py`.
