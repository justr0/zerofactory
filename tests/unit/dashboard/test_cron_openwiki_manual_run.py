"""Regression tests for manually triggering the OpenWiki cron gate."""

from fastapi.testclient import TestClient

from dashboard.routes import cron as cron_routes


def test_openwiki_job_runs_gate_and_preserves_embedded_prefix(
    api_client: TestClient, monkeypatch, tmp_path
):
    slug = "demo-zero-factory-openwiki-update-lab"
    created = api_client.post(
        "/api/plugins/zerofactory/boards",
        json={"slug": slug, "git_url": "https://github.com/example/openwiki-lab.git"},
    )
    assert created.status_code == 200
    job_id = f"zero-factory-openwiki-update-{slug}"
    resolved_slugs = []
    gate_calls = []

    monkeypatch.setattr(
        cron_routes,
        "resolve_task_repo_path",
        lambda cursor, board_slug, repo: resolved_slugs.append(board_slug) or tmp_path,
    )
    from scripts import zf_openwiki_gate

    monkeypatch.setattr(
        zf_openwiki_gate,
        "check_openwiki_gate",
        lambda repo_path, board_slug: (
            gate_calls.append((repo_path, board_slug))
            or (False, "No OpenWiki update needed.")
        ),
    )

    triggered = api_client.post(f"/api/plugins/zerofactory/cron/{job_id}/run")

    assert triggered.status_code == 200
    assert triggered.json() == {
        "ok": True,
        "message": "No OpenWiki update needed.",
        "wakeAgent": False,
    }
    assert resolved_slugs == [slug]
    assert gate_calls == [(tmp_path, slug)]
