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
