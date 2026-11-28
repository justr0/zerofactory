"""Shared fixtures and configuration for Zero Factory tests."""

from __future__ import annotations

import gc
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
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
    monkeypatch.setenv("OPENWIKI_CONFIG_DIR", str(tmp_path / "openwiki-config"))


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


# ``Path.home()`` and ``expanduser("~")`` read ``HOME`` on POSIX but ``USERPROFILE``
# (or ``HOMEDRIVE`` + ``HOMEPATH``) on Windows, where ``HOME`` is ignored: a fake
# home that only overrides ``HOME`` resolves to the operator's real profile there.
HOME_ENV_KEYS = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")


def fake_home_env(home: Path) -> dict[str, str]:
    """Env overrides that point ``Path.home()`` at ``home`` on every platform."""
    drive, tail = os.path.splitdrive(str(home))
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    if drive:
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail
    return env


def force_rmtree(path: Path | str) -> None:
    """Remove a tree git and sqlite have written to, even on Windows.

    git stores object files read-only. POSIX lets the owner unlink them
    anyway; Windows refuses, so ``shutil.rmtree`` stops at the first one and,
    with ``ignore_errors=True``, quietly leaves the rest of the tree behind in
    the temp directory: one leaked tree per test run. Windows also refuses to
    delete a database file while a connection holds it open, and a connection
    opened with ``with sqlite3.connect(...)`` - which commits but never
    closes - is only closed once the garbage collector reclaims it.
    """
    if sys.platform != "win32":
        shutil.rmtree(path, ignore_errors=True)
        return
    gc.collect()
    _make_writable(path)
    shutil.rmtree(path, ignore_errors=True)


def _make_writable(path: Path | str) -> None:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            # A junction or symlink: never touch what it points at.
            if st.st_file_attributes & reparse:
                continue
            if entry.is_dir(follow_symlinks=False):
                _make_writable(entry.path)
            elif not st.st_mode & stat.S_IWRITE:
                try:
                    os.chmod(entry.path, stat.S_IWRITE)
                except OSError:
                    pass


@pytest.fixture(autouse=True)
def _hermetic_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``Path.home()``-relative state out of the operator's real home.

    Windows only: the suite's fake homes override ``HOME``, which ``Path.home()``
    ignores there, and an inherited ``HERMES_HOME`` (factory worker sessions)
    points tests at the live home. Tests that set their own fake home with
    ``fake_home_env`` override this for their duration.
    """
    if os.name != "nt":
        return
    monkeypatch.delenv("HERMES_HOME", raising=False)
    for key, value in fake_home_env(tmp_path).items():
        monkeypatch.setenv(key, value)


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


@pytest.fixture
def dir_link() -> Iterator[Callable[[Path, Path], None]]:
    """Link a directory the way an unprivileged user can on this host.

    POSIX: a symbolic link.  Windows: a junction — ``symlink_to`` needs a
    privilege most accounts lack (``WinError 1314``), while a junction is the
    link an unprivileged process can actually create.  Both resolve to their
    target, which is what the path-containment checks must see through.
    """

    def _link(target: Path, link: Path) -> None:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)

    yield _link
