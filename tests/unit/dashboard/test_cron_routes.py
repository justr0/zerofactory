"""Unit tests for dashboard/routes/cron.py endpoints."""

from fastapi.testclient import TestClient

from dashboard.routes import cron as cron_routes


def test_get_cron_jobs(api_client: TestClient, default_board: str):
    """Verify listing cron jobs returns core watchdog and board scanners."""
    res = api_client.get("/api/plugins/zerofactory/cron")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["scheduler_enabled"] is True
    jobs = data["jobs"]
    assert len(jobs) >= 2

    # Check for core watchdog
    watchdog = next(
        (j for j in jobs if j["id"] == "zero-factory-task-queue-check"), None
    )
    assert watchdog is not None
    assert watchdog["category"] == "core"

    # Check for board scanner
    scanner = next(
        (
            j
            for j in jobs
            if j["id"] == f"zero-factory-improvement-scanner-{default_board}"
        ),
        None,
    )
    assert scanner is not None
    assert scanner["category"] == "scanner"
    assert scanner["scan_on_idle"] is False
    assert scanner["enabled"] is False


def test_get_cron_jobs_scanner_config_error_falls_back(
    api_client: TestClient, default_board: str, monkeypatch
):
    """Use disabled defaults when the scanner config provider raises."""

    def raise_config_error(*args, **kwargs):
        raise RuntimeError("scanner config unavailable")

    monkeypatch.setattr(cron_routes, "get_scanner_cron_config", raise_config_error)

    res = api_client.get("/api/plugins/zerofactory/cron")
    assert res.status_code == 200
    scanner = next(
        job
        for job in res.json()["jobs"]
        if job["id"] == f"zero-factory-improvement-scanner-{default_board}"
    )
    assert scanner["enabled"] is False
    assert scanner["scan_on_idle"] is False
    assert (
        scanner["idle_scan_cooldown_minutes"]
        == cron_routes.DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES
    )
    assert scanner["idle_scan_max_todo"] == cron_routes.DEFAULT_IDLE_SCAN_MAX_TODO


def test_toggle_scheduler(api_client: TestClient):
    """Verify toggling scheduler."""
    res = api_client.post(
        "/api/plugins/zerofactory/cron/scheduler/toggle",
        json={"enabled": False},
    )
    assert res.status_code == 200
    assert res.json()["scheduler_enabled"] is False

    res2 = api_client.post(
        "/api/plugins/zerofactory/cron/scheduler/toggle",
        json={"enabled": True},
    )
    assert res2.status_code == 200
    assert res2.json()["scheduler_enabled"] is True


def test_cron_sync(api_client: TestClient):
    """Verify syncing cron jobs."""
    res = api_client.post("/api/plugins/zerofactory/cron/sync")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["synced"] >= 1


def test_cron_job_update_and_reset(api_client: TestClient, default_board: str):
    """Scanner settings are isolated per board and reset to board defaults."""
    job_id = f"zero-factory-improvement-scanner-{default_board}"
    second_board_res = api_client.post(
        "/api/plugins/zerofactory/boards",
        json={"git_url": "https://github.com/example/second-board.git"},
    )
    assert second_board_res.status_code == 200
    second_board = second_board_res.json()["slug"]
    second_job_id = f"zero-factory-improvement-scanner-{second_board}"

    for target_job_id, cooldown, max_todo in (
        (job_id, 30, 5),
        (second_job_id, 45, 7),
    ):
        res = api_client.put(
            f"/api/plugins/zerofactory/cron/{target_job_id}",
            json={
                "idle_scan_cooldown_minutes": cooldown,
                "idle_scan_max_todo": max_todo,
            },
        )
        assert res.status_code == 200

    jobs = api_client.get("/api/plugins/zerofactory/cron").json()["jobs"]
    scanners = {job["id"]: job for job in jobs}
    assert scanners[job_id]["idle_scan_cooldown_minutes"] == 30
    assert scanners[job_id]["idle_scan_max_todo"] == 5
    assert scanners[second_job_id]["idle_scan_cooldown_minutes"] == 45
    assert scanners[second_job_id]["idle_scan_max_todo"] == 7

    res_reset = api_client.post(f"/api/plugins/zerofactory/cron/{job_id}/reset")
    assert res_reset.status_code == 200
    assert res_reset.json()["ok"] is True

    jobs = api_client.get("/api/plugins/zerofactory/cron").json()["jobs"]
    scanners = {job["id"]: job for job in jobs}
    assert (
        scanners[job_id]["idle_scan_cooldown_minutes"]
        == cron_routes.DEFAULT_IDLE_SCAN_COOLDOWN_MINUTES
    )
    assert (
        scanners[job_id]["idle_scan_max_todo"] == cron_routes.DEFAULT_IDLE_SCAN_MAX_TODO
    )
    assert scanners[second_job_id]["idle_scan_cooldown_minutes"] == 45
    assert scanners[second_job_id]["idle_scan_max_todo"] == 7


def test_cron_run_job(api_client: TestClient, default_board: str):
    """Verify triggering an immediate run of a cron job."""
    res = api_client.post(
        "/api/plugins/zerofactory/cron/zero-factory-task-queue-check/run"
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # Unknown job
    res_unknown = api_client.post("/api/plugins/zerofactory/cron/unknown-job-id/run")
    assert res_unknown.status_code == 400


def test_cron_job_pause_persists_status(api_client: TestClient, default_board: str):
    """Verify pausing a job updates its enabled flag and paused state in GET /cron."""
    job_id = f"zero-factory-improvement-scanner-{default_board}"

    # Pause job
    res_pause = api_client.post(
        f"/api/plugins/zerofactory/cron/{job_id}/toggle",
        json={"enabled": False},
    )
    assert res_pause.status_code == 200
    assert res_pause.json()["enabled"] is False

    # Check GET /cron reflects paused state
    res = api_client.get("/api/plugins/zerofactory/cron")
    assert res.status_code == 200
    jobs = res.json()["jobs"]
    scanner = next((j for j in jobs if j["id"] == job_id), None)
    assert scanner is not None
    assert scanner["enabled"] is False
    assert scanner["state"] == "paused"

    # Resume job
    res_resume = api_client.post(
        f"/api/plugins/zerofactory/cron/{job_id}/toggle",
        json={"enabled": True},
    )
    assert res_resume.status_code == 200
    assert res_resume.json()["enabled"] is True

    # Check GET /cron reflects active state
    res2 = api_client.get("/api/plugins/zerofactory/cron")
    scanner2 = next((j for j in res2.json()["jobs"] if j["id"] == job_id), None)
    assert scanner2 is not None
    assert scanner2["enabled"] is True
    assert scanner2["state"] == "active"
