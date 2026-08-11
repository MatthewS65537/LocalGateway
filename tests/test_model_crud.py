import pytest


async def test_create_model(client):
    r = await client.post("/admin/models", json={
        "id": "test-model",
        "description": "Test model",
        "context_length": 8192,
        "capabilities": {"text": True, "vision": False},
        "modality": "text",
        "tags": ["fast", "cheap"],
        "aliases": ["test-alias"],
        "max_output_tokens": 2048,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["model"]["id"] == "test-model"
    assert data["model"]["description"] == "Test model"
    assert data["model"]["context_length"] == 8192
    assert data["model"]["capabilities"] == {"text": True, "vision": False}
    assert data["model"]["tags"] == ["fast", "cheap"]
    assert data["model"]["aliases"] == ["test-alias"]

    # Cleanup
    await client.delete("/admin/models/test-model")


async def test_create_model_duplicate(client):
    r = await client.post("/admin/models", json={"id": "test-dup"})
    assert r.status_code == 200

    r2 = await client.post("/admin/models", json={"id": "test-dup"})
    assert r2.status_code == 409
    assert "already exists" in r2.json()["error"]

    await client.delete("/admin/models/test-dup")


async def test_get_model(client):
    await client.post("/admin/models", json={
        "id": "test-get",
        "description": "Get test",
        "context_length": 4096,
        "capabilities": {"tools": True},
    })

    r = await client.get("/admin/models/test-get")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "test-get"
    assert data["description"] == "Get test"
    assert data["context_length"] == 4096
    assert data["capabilities"] == {"tools": True}
    assert "backends" in data

    await client.delete("/admin/models/test-get")


async def test_get_model_not_found(client):
    r = await client.get("/admin/models/nonexistent")
    assert r.status_code == 404


async def test_update_model(client):
    await client.post("/admin/models", json={"id": "test-update"})

    r = await client.put("/admin/models/test-update", json={
        "description": "Updated",
        "context_length": 16384,
        "tags": ["smart"],
        "capabilities": {"vision": True, "audio": True},
    })
    assert r.status_code == 200

    r2 = await client.get("/admin/models/test-update")
    data = r2.json()
    assert data["description"] == "Updated"
    assert data["context_length"] == 16384
    assert data["tags"] == ["smart"]
    assert data["capabilities"] == {"vision": True, "audio": True}

    await client.delete("/admin/models/test-update")


async def test_delete_model(client):
    await client.post("/admin/models", json={"id": "test-delete"})
    r = await client.delete("/admin/models/test-delete")
    assert r.status_code == 200

    r2 = await client.get("/admin/models/test-delete")
    assert r2.status_code == 404


async def test_add_backend(client):
    await client.post("/admin/models", json={"id": "test-backend"})

    r = await client.post("/admin/models/test-backend/backends", json={
        "provider": "mock1",
        "model": "test-backend-model",
        "context_length": 8192,
    })
    assert r.status_code == 200

    r2 = await client.get("/admin/models/test-backend")
    data = r2.json()
    assert len(data["backends"]) == 1
    assert data["backends"][0]["provider"] == "mock1"
    assert data["backends"][0]["model"] == "test-backend-model"
    assert data["backends"][0]["priority"] == 1

    await client.delete("/admin/models/test-backend")


async def test_update_backend(client):
    await client.post("/admin/models", json={"id": "test-backend-update"})
    await client.post("/admin/models/test-backend-update/backends", json={
        "provider": "mock1",
        "model": "original",
    })

    r = await client.put("/admin/models/test-backend-update/backends/0", json={
        "model": "updated",
        "context_length": 16384,
    })
    assert r.status_code == 200

    r2 = await client.get("/admin/models/test-backend-update")
    backend = r2.json()["backends"][0]
    assert backend["model"] == "updated"
    assert backend["context_length"] == 16384

    await client.delete("/admin/models/test-backend-update")


async def test_remove_backend(client):
    await client.post("/admin/models", json={"id": "test-backend-remove"})
    await client.post("/admin/models/test-backend-remove/backends", json={
        "provider": "mock1",
        "model": "model1",
    })
    await client.post("/admin/models/test-backend-remove/backends", json={
        "provider": "mock2",
        "model": "model2",
    })

    r = await client.delete("/admin/models/test-backend-remove/backends/0")
    assert r.status_code == 200

    r2 = await client.get("/admin/models/test-backend-remove")
    backends = r2.json()["backends"]
    assert len(backends) == 1
    assert backends[0]["model"] == "model2"
    assert backends[0]["priority"] == 1

    await client.delete("/admin/models/test-backend-remove")


async def test_reorder_backends_via_tiers(client):
    # Reordering now uses /backends/tiers (the /backends/reorder endpoint was
    # deleted as dead code — drag-reorder persists via tiers since refinement-v2).
    await client.post("/admin/models", json={"id": "test-reorder"})
    r1 = await client.post("/admin/models/test-reorder/backends", json={
        "provider": "mock1",
        "model": "model1",
    })
    assert r1.status_code == 200, f"First backend add failed: {r1.text}"
    r2 = await client.post("/admin/models/test-reorder/backends", json={
        "provider": "mock2",
        "model": "model2",
    })
    assert r2.status_code == 200, f"Second backend add failed: {r2.text}"

    # Reverse the order via tiers: [[1], [0]] → model2 in tier 1, model1 in tier 2.
    # tiers sets priorities but does NOT reorder the list (unlike the deleted
    # reorder endpoint), so we assert on priority, not list position.
    r = await client.put("/admin/models/test-reorder/backends/tiers", json={
        "tiers": [[1], [0]],
    })
    assert r.status_code == 200, f"Tiers reorder failed: {r.text}"

    r2 = await client.get("/admin/models/test-reorder")
    backends = r2.json()["backends"]
    by_model = {b["model"]: b["priority"] for b in backends}
    assert by_model["model2"] == 1, f"model2 should be tier 1, got {by_model['model2']}"
    assert by_model["model1"] == 2, f"model1 should be tier 2, got {by_model['model1']}"

    # The old reorder endpoint must be gone. The path now falls through to
    # /backends/{index} (int param) which 422s on "reorder" — either way it's
    # no longer a functioning reorder endpoint, so it must not return 200.
    gone = await client.put("/admin/models/test-reorder/backends/reorder", json={"order": [1, 0]})
    assert gone.status_code != 200, f"reorder endpoint should be deleted, got {gone.status_code}"

    await client.delete("/admin/models/test-reorder")


async def test_compare_models(client):
    await client.post("/admin/models", json={
        "id": "compare1",
        "description": "First",
        "context_length": 8192,
    })
    await client.post("/admin/models", json={
        "id": "compare2",
        "description": "Second",
        "context_length": 16384,
    })

    r = await client.get("/admin/models/compare?ids=compare1,compare2")
    assert r.status_code == 200
    data = r.json()
    assert len(data["models"]) == 2
    assert data["models"][0]["id"] == "compare1"
    assert data["models"][1]["id"] == "compare2"

    await client.delete("/admin/models/compare1")
    await client.delete("/admin/models/compare2")


async def test_compare_too_many(client):
    await client.post("/admin/models", json={"id": "c1"})
    await client.post("/admin/models", json={"id": "c2"})
    await client.post("/admin/models", json={"id": "c3"})
    await client.post("/admin/models", json={"id": "c4"})

    r = await client.get("/admin/models/compare?ids=c1,c2,c3,c4")
    assert r.status_code == 422
    assert "Max 3" in r.json()["error"]

    await client.delete("/admin/models/c1")
    await client.delete("/admin/models/c2")
    await client.delete("/admin/models/c3")
    await client.delete("/admin/models/c4")


    await client.delete("/admin/models/catalog1")
    await client.delete("/admin/models/catalog2")
