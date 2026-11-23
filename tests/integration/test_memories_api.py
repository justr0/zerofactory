"""Integration tests for Memories API endpoints (/api/plugins/zerofactory/memories)."""

from fastapi.testclient import TestClient


def test_memories_lifecycle(api_client: TestClient, default_board: str):
    """Verify creating, listing, updating, and deleting memories."""
    # 1. Create memory
    create_res = api_client.post(
        f"/api/plugins/zerofactory/boards/{default_board}/memories",
        json={
            "category": "gotcha",
            "content": "Always test SQLite timeout in multithreaded environments.",
            "tags": ["sqlite", "concurrency"],
        },
    )
    assert create_res.status_code == 200
    mem_data = create_res.json()["memory"]
    mem_id = mem_data["id"]
    assert mem_data["category"] == "gotcha"
    assert "Always test SQLite timeout" in mem_data["content"]

    # 2. List memories for board
    list_res = api_client.get(
        f"/api/plugins/zerofactory/boards/{default_board}/memories"
    )
    assert list_res.status_code == 200
    memories = list_res.json()["memories"]
    assert any(m["id"] == mem_id for m in memories)

    # 3. Update memory
    update_res = api_client.put(
        f"/api/plugins/zerofactory/memories/{mem_id}",
        json={
            "content": "Always set timeout >= 15.0 on SQLite connections.",
            "category": "convention",
        },
    )
    assert update_res.status_code == 200
    updated = update_res.json()["memory"]
    assert updated["category"] == "convention"
    assert "Always set timeout >= 15.0" in updated["content"]

    # 4. Search memories via query param q
    search_res = api_client.get("/api/plugins/zerofactory/memories?q=timeout")
    assert search_res.status_code == 200
    assert any(m["id"] == mem_id for m in search_res.json()["memories"])

    # 5. Delete memory
    del_res = api_client.delete(f"/api/plugins/zerofactory/memories/{mem_id}")
    assert del_res.status_code == 200
    assert del_res.json()["ok"] is True

    # Check deleted
    list_after = api_client.get(
        f"/api/plugins/zerofactory/boards/{default_board}/memories"
    ).json()["memories"]
    assert not any(m["id"] == mem_id for m in list_after)


def test_manual_memory_creation_disables_mem0_inference(
    api_client: TestClient, default_board: str, monkeypatch
):
    """Manual memory creation passes infer=False to Mem0."""
    from dashboard import memory_service

    calls = []

    class FakeMem0Client:
        def add(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(memory_service, "get_mem0_client", lambda: FakeMem0Client())

    response = api_client.post(
        f"/api/plugins/zerofactory/boards/{default_board}/memories",
        json={
            "category": "decision",
            "content": "Keep manual memory writes deterministic.",
        },
    )

    assert response.status_code == 200
    assert (
        response.json()["memory"]["content"]
        == "Keep manual memory writes deterministic."
    )
    assert len(calls) == 1
    assert calls[0][1]["infer"] is False
