"""Exactly one dispatcher owns dispatching, however often the plugin is loaded.

The plugin is imported under several module names inside one Hermes process and
is loaded by several processes (gateway, dashboard, CLI). Every copy used to
start its own dispatch loop; the loops shared no worker registry, dispatched
duplicate workers for the same task and tore each other's workers down. These
tests pin the ownership guarantee that closes that hole.
"""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest
from dispatcher import fcntl


def _hold_lock(path) -> int:
    """Take the lock the way another dispatcher loop (another process) would."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _enable_dispatcher_autostart(db_path) -> None:
    """Create the minimal settings table read by plugin registration."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('auto_start_dispatcher', 'true')"
        )


def _configure_autostart_environment(
    tmp_path, monkeypatch, profile: str | None = None, *, profile_home: bool = False
):
    db_path = tmp_path / "zerofactory.db"
    _enable_dispatcher_autostart(db_path)
    hermes_home = tmp_path / "hermes"
    if profile_home:
        assert profile is not None
        hermes_home = hermes_home / "profiles" / profile
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
    elif profile is not None:
        monkeypatch.setenv("HERMES_PROFILE", profile)
    else:
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ZEROFACTORY_DB", str(db_path))
    for name in (
        "ZEROFACTORY_SKIP_DISPATCHER",
        "ZEROFACTORY_DISABLE_DISPATCHER",
        "ZEROFACTORY_IDLE_SCAN",
        "HERMES_KANBAN_TASK",
        "_HERMES_CRON_EXTERNAL_WORKER",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("profile", ["zf-gatekeeper", "zf-auditor"])
@pytest.mark.parametrize("profile_home", [False, True])
def test_auxiliary_zf_profiles_are_workers_and_do_not_auto_start(
    tmp_path, monkeypatch, profile, profile_home
):
    import __init__ as plugin_init
    from dispatcher.config import is_worker_or_child_process

    _configure_autostart_environment(
        tmp_path, monkeypatch, profile, profile_home=profile_home
    )

    assert is_worker_or_child_process() is True
    assert plugin_init._should_auto_start_dispatcher() is False


def test_default_profile_auto_start_behavior_is_unchanged(tmp_path, monkeypatch):
    import __init__ as plugin_init
    from dispatcher.config import is_worker_or_child_process

    _configure_autostart_environment(tmp_path, monkeypatch)

    assert is_worker_or_child_process() is False
    assert plugin_init._should_auto_start_dispatcher() is True


@pytest.mark.parametrize("profile_home", [False, True])
def test_register_under_gatekeeper_does_not_start_or_tick_dispatcher(
    tmp_path, monkeypatch, profile_home
):
    from unittest.mock import MagicMock

    import __init__ as plugin_init

    _configure_autostart_environment(
        tmp_path, monkeypatch, "zf-gatekeeper", profile_home=profile_home
    )

    start_mock = MagicMock()
    init_mock = MagicMock()
    cron_mock = MagicMock()
    dispatch_mock = MagicMock()
    hooks = {}
    monkeypatch.setattr(plugin_init, "start_background_dispatcher", start_mock)
    monkeypatch.setattr(plugin_init, "init_db", init_mock)
    monkeypatch.setattr(plugin_init, "ensure_builtin_cron_jobs", cron_mock)
    monkeypatch.setattr(plugin_init, "run_dispatch_cycle", dispatch_mock)

    ctx = MagicMock()
    ctx.register_hook.side_effect = lambda name, fn: hooks.__setitem__(name, fn)
    plugin_init.register(ctx)

    start_mock.assert_not_called()
    init_mock.assert_not_called()
    cron_mock.assert_not_called()
    assert hooks["on_kanban_dispatch_tick"]() == {
        "ok": True,
        "skipped": True,
        "reason": "dispatcher_disabled",
    }
    dispatch_mock.assert_not_called()


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    """Scheduler with an isolated lock directory and a no-op dispatch cycle."""
    from dispatcher import scheduler as sched

    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatch.lock"))
    monkeypatch.setattr(sched, "is_worker_or_child_process", lambda: False)
    cycles: list[int] = []
    monkeypatch.setattr(sched, "run_dispatch_cycle", lambda *a, **kw: cycles.append(1))
    sched._dispatcher_stop_event.clear()
    try:
        yield sched, cycles
    finally:
        sched.stop_background_dispatcher(timeout=5.0)
        sched._dispatcher_stop_event.clear()


def test_start_is_idempotent_and_holds_the_owner_lock(scheduler):
    sched, _ = scheduler
    owner_path = sched.get_dispatcher_owner_lock_path()
    assert owner_path.name == "dispatch.lock.owner"

    assert sched.start_background_dispatcher() is True
    assert sched.start_background_dispatcher() is False

    threads = sched.live_dispatcher_threads()
    assert [t.name for t in threads] == [sched.DISPATCHER_THREAD_NAME]
    # The loop's own lock is a different open file description, so even this
    # process sees the loop as the owner.
    assert sched.dispatcher_owned_elsewhere() is True
    with pytest.raises(BlockingIOError):
        fd = _hold_lock(owner_path)
        os.close(fd)


def test_stop_releases_ownership(scheduler):
    sched, _ = scheduler
    assert sched.start_background_dispatcher() is True

    assert sched.stop_background_dispatcher() is True

    assert sched.live_dispatcher_threads() == []
    assert sched.dispatcher_owned_elsewhere() is False
    fd = _hold_lock(sched.get_dispatcher_owner_lock_path())
    os.close(fd)


def test_start_declines_while_another_process_owns_the_loop(scheduler):
    sched, cycles = scheduler
    fd = _hold_lock(sched.get_dispatcher_owner_lock_path())
    try:
        assert sched.dispatcher_owned_elsewhere() is True
        assert sched.start_background_dispatcher() is False
        assert sched.live_dispatcher_threads() == []
        assert cycles == []
    finally:
        os.close(fd)
    # Ownership is advisory and self-healing: once the other loop is gone the
    # next start takes over.
    assert sched.start_background_dispatcher() is True


def test_start_adopts_a_loop_started_by_another_module_copy(scheduler):
    sched, _ = scheduler
    release = threading.Event()

    def _foreign_loop():
        release.wait(10)

    foreign = threading.Thread(
        target=_foreign_loop, name=sched.DISPATCHER_THREAD_NAME, daemon=True
    )
    foreign._zf_stop_event = release  # type: ignore[attr-defined]
    foreign.start()
    try:
        assert sched.start_background_dispatcher() is False
        assert sched.live_dispatcher_threads() == [foreign]

        # Stop reaches that loop too, instead of reporting "nothing is running".
        assert sched.stop_background_dispatcher() is True
        assert not foreign.is_alive()
        assert sched.live_dispatcher_threads() == []
    finally:
        release.set()
        foreign.join(5)


def test_stop_signals_a_loop_that_has_no_own_stop_event(scheduler):
    sched, _ = scheduler
    # A loop started before threads carried their own stop event listens to the
    # module-level event; stop must fall back to it.
    legacy = threading.Thread(
        target=sched._dispatcher_stop_event.wait,
        name=sched.DISPATCHER_THREAD_NAME,
        daemon=True,
    )
    legacy.start()
    try:
        assert sched.stop_background_dispatcher() is True
        assert not legacy.is_alive()
    finally:
        sched._dispatcher_stop_event.set()
        legacy.join(5)


def test_loop_releases_ownership_when_it_exits(scheduler):
    sched, _ = scheduler
    assert sched.start_background_dispatcher() is True
    thread = sched.live_dispatcher_threads()[0]

    thread._zf_stop_event.set()
    thread.join(10)

    assert not thread.is_alive()
    assert sched.dispatcher_owned_elsewhere() is False


def test_tick_hook_defers_to_the_loop_owner(tmp_path, monkeypatch):
    """A non-owning process (e.g. the dashboard) must not run its own cycles."""
    import __init__ as plugin_init

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ZEROFACTORY_DB", str(tmp_path / "zf.db"))
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatch.lock"))
    monkeypatch.setattr(plugin_init, "_should_auto_start_dispatcher", lambda: True)
    monkeypatch.setattr(plugin_init, "init_db", lambda: None)
    monkeypatch.setattr(plugin_init, "ensure_builtin_cron_jobs", lambda: None)
    ticks: list[int] = []
    monkeypatch.setattr(plugin_init, "run_dispatch_cycle", lambda: ticks.append(1))

    hooks = {}

    class _Ctx:
        def register_cli_command(self, *args, **kwargs):
            pass

        def register_dashboard(self, *args, **kwargs):
            pass

        def register_hook(self, name, fn):
            hooks[name] = fn

    fd = _hold_lock(tmp_path / "dispatch.lock.owner")
    try:
        # Loading the plugin in a process that does not own the loop starts nothing.
        plugin_init.register(_Ctx())
        assert "on_kanban_dispatch_tick" in hooks
        from dispatcher import scheduler as sched

        assert sched.live_dispatcher_threads() == []

        tick = hooks["on_kanban_dispatch_tick"]
        assert tick() == {
            "ok": True,
            "skipped": True,
            "reason": "dispatcher_owned_elsewhere",
        }
        assert ticks == []
    finally:
        os.close(fd)

    assert tick() is None
    assert ticks == [1]


def test_dispatch_routes_report_a_loop_owned_by_another_process(tmp_path, monkeypatch):
    import profile_manager
    from dashboard.routes import cron as cron_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ZEROFACTORY_DB", str(tmp_path / "zf.db"))
    monkeypatch.setenv("ZEROFACTORY_LOCK_PATH", str(tmp_path / "dispatch.lock"))
    monkeypatch.setattr(
        profile_manager, "check_gh_auth_status", lambda use_cache=True: True
    )
    monkeypatch.setattr(cron_routes, "ensure_builtin_cron_jobs", lambda: None)
    profile_manager.ensure_zf_profiles()

    from dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/zerofactory")
    client = TestClient(app)

    fd = _hold_lock(tmp_path / "dispatch.lock.owner")
    try:
        status = client.get("/api/plugins/zerofactory/setup/status").json()
        assert status["dispatcher_running"] is True
        assert status["dispatcher_in_process"] is False

        stopped = client.post("/api/plugins/zerofactory/dispatch/stop").json()
        assert stopped["stopped"] is False
        assert stopped["dispatcher_running"] is True
        assert stopped["dispatcher_in_process"] is False
        assert "another process" in stopped["message"]

        started = client.post("/api/plugins/zerofactory/dispatch/start").json()
        assert started["started"] is False
        assert started["dispatcher_running"] is True
        assert started["dispatcher_in_process"] is False
        assert "another process" in started["message"]

        from dispatcher import scheduler as sched

        assert sched.live_dispatcher_threads() == []
    finally:
        os.close(fd)
