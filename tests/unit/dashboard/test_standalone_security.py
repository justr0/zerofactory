"""Security boundaries for the standalone dashboard."""

from __future__ import annotations

import pytest


def test_standalone_rejects_non_loopback_host(monkeypatch):
    from dashboard import standalone

    monkeypatch.setattr("sys.argv", ["zerofactory-standalone", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit) as exc_info:
        standalone.main()
    assert exc_info.value.code == 2


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_standalone_accepts_loopback_ip(host):
    from dashboard.standalone import _loopback_host

    assert _loopback_host(host) == host


def test_settings_response_omits_secret_values(api_client):
    from dashboard.db import get_db_conn

    with get_db_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, 1)",
            [
                ("langfuse_secret_key", "secret-langfuse-value"),
                ("mem0_api_key", "secret-mem0-value"),
            ],
        )
        conn.commit()

    response = api_client.get("/api/plugins/zerofactory/settings")

    assert response.status_code == 200
    settings = response.json()["settings"]
    assert "langfuse_secret_key" not in settings
    assert "mem0_api_key" not in settings
    assert "secret-langfuse-value" not in response.text
    assert "secret-mem0-value" not in response.text


def test_settings_save_preserves_blank_secrets(api_client):
    from dashboard.db import get_db_conn

    with get_db_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, 1)",
            [
                ("langfuse_secret_key", "saved-langfuse-secret"),
                ("mem0_api_key", "saved-mem0-secret"),
            ],
        )
        conn.commit()

    response = api_client.patch(
        "/api/plugins/zerofactory/settings",
        json={
            "max_active_tasks": 6,
            "langfuse_secret_key": "",
            "mem0_api_key": "",
        },
    )

    assert response.status_code == 200
    assert "saved-langfuse-secret" not in response.text
    assert "saved-mem0-secret" not in response.text
    with get_db_conn() as conn:
        stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    assert stored["langfuse_secret_key"] == "saved-langfuse-secret"
    assert stored["mem0_api_key"] == "saved-mem0-secret"


def test_new_langfuse_secret_is_synced_but_not_returned(api_client, monkeypatch):
    from dashboard.routes import settings as settings_route

    synced = {}
    monkeypatch.setattr(
        settings_route,
        "sync_langfuse_profiles",
        lambda settings: synced.update(settings),
    )

    response = api_client.patch(
        "/api/plugins/zerofactory/settings",
        json={"langfuse_secret_key": "new-langfuse-secret"},
    )

    assert response.status_code == 200
    assert synced["langfuse_secret_key"] == "new-langfuse-secret"
    assert "langfuse_secret_key" not in response.json()["settings"]
    assert "new-langfuse-secret" not in response.text
