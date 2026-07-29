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


async def test_reorder_backends(client):
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

    r = await client.put("/admin/models/test-reorder/backends/reorder", json={
        "order": [1, 0],
    })
    assert r.status_code == 200, f"Reorder failed: {r.text}"

    r2 = await client.get("/admin/models/test-reorder")
    backends = r2.json()["backends"]
    assert backends[0]["model"] == "model2"
    assert backends[0]["priority"] == 1
    assert backends[1]["model"] == "model1"
    assert backends[1]["priority"] == 2

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


async def test_catalog_search(client):
    await client.post("/admin/models", json={
        "id": "catalog1",
        "description": "Vision model",
        "capabilities": {"vision": True},
        "modality": "text+vision",
        "context_length": 8192,
        "tags": ["fast"],
    })
    await client.post("/admin/models", json={
        "id": "catalog2",
        "description": "Text only",
        "capabilities": {"text": True},
        "modality": "text",
        "context_length": 16384,
    })

    r = await client.get("/admin/catalog?q=vision")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    assert any(m["id"] == "catalog1" for m in data["models"])

    r2 = await client.get("/admin/catalog?capability=vision")
    data2 = r2.json()
    assert any(m["id"] == "catalog1" for m in data2["models"])
    assert not any(m["id"] == "catalog2" for m in data2["models"])

    r3 = await client.get("/admin/catalog?modality=text")
    data3 = r3.json()
    assert any(m["id"] == "catalog2" for m in data3["models"])

    r4 = await client.get("/admin/catalog?min_ctx=10000")
    data4 = r4.json()
    assert any(m["id"] == "catalog2" for m in data4["models"])
    assert not any(m["id"] == "catalog1" for m in data4["models"])

    await client.delete("/admin/models/catalog1")
    await client.delete("/admin/models/catalog2")
