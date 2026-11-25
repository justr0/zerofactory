"""Shared fixtures and configuration for Zero Factory tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Ensure repository root is on sys.path
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# Pin the repository's own `tests` package in sys.modules before anything else
# can shadow the top-level name: the dispatcher import chain inserts foreign
# roots (e.g. ~/.hermes/hermes-agent) into sys.path, and that repo's own `tests/`
# package would otherwise win `import tests` and break `from tests.unit...`
# imports inside test modules (order-dependent ModuleNotFoundError).
import tests  # noqa: E402

# Ensure hermetic defaults before any tests run
os.environ.setdefault("ZEROFACTORY_SKIP_GIT", "1")
os.environ.setdefault("ZEROFACTORY_SKIP_CRON_SYNC", "1")
os.environ.setdefault("ZEROFACTORY_DISABLE_DISPATCHER", "1")
os.environ.setdefault("ZEROFACTORY_SKIP_WORKER_SPAWN", "1")
os.environ.setdefault("ZEROFACTORY_SKIP_UPDATE_CHECK", "1")


@pytest.fixture
def isolated_env() -> Iterator[dict]:
    """Preserve and restore os.environ around a test."""
    orig = dict(os.environ)
    try:
        yield orig
    finally:
        os.environ.clear()
        os.environ.update(orig)


@pytest.fixture(autouse=True)
def _hermetic_dispatcher_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep dispatcher lock state out of the real ``~/.hermes``.

    The dispatcher lock file lives at ``ZEROFACTORY_LOCK_PATH`` (default: the
    Hermes home) and stop markers are written beside it.  Tests that reach
    those code paths must not touch - or leave files in - the operator's real
    home.  Tests about the lock itself override this with their own path.
    """
    monkeypatch.setenv(
        "ZEROFACTORY_LOCK_PATH", str(tmp_path / "zerofactory_dispatcher.lock")
    )


@pytest.fixture(autouse=True)
def _hermetic_default_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests that request no DB fixture off the operator's board database.

    Worker precommit runs inherit ``ZEROFACTORY_DB`` (the live board DB). A
    test that reaches ``init_db()`` without the ``initialized_db`` fixture would
    otherwise migrate and seed the real database and fail intermittently with
    ``sqlite3.OperationalError: database is locked`` whenever the running
    dispatcher holds a write transaction. Tests about DB path resolution
    override this with ``monkeypatch.delenv``/``setenv`` of their own.
    """
    monkeypatch.setenv("ZEROFACTORY_DB", str(tmp_path / "zerofactory_default.db"))


@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    """Provide a path to a fresh SQLite database for tests."""
    return tmp_path / "zerofactory_test.db"


@pytest.fixture
def initialized_db(test_db_path: Path, isolated_env: dict) -> Path:
    """Initialize a fresh Zero Factory database schema and set environment variable."""
    from dashboard.plugin_api import init_db

    os.environ["ZEROFACTORY_DB"] = str(test_db_path)
    init_db(force=True)
    return test_db_path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create and initialize a clean local git repository."""
    repo = tmp_path / "test_repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: initial commit"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def api_client(initialized_db: Path):
    """Provide a FastAPI TestClient wired to the Zero Factory plugin router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/zerofactory")
    return TestClient(app)


@pytest.fixture
def default_board(api_client) -> str:
    """Ensure a default board exists for tasks that require a valid board foreign key."""
    res = api_client.post(
        "/api/plugins/zerofactory/boards",
        json={
            "git_url": "https://github.com/hotcode-dev/zerofactory.git",
            "description": "Default Test Board",
        },
    )
    return res.json()["slug"]
