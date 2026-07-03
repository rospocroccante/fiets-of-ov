"""Phase 0 skeleton: the service must expose a liveness endpoint.

This is the one trivial test the roadmap calls for — it also proves the FastAPI
app boots and routes correctly, which is what CI verifies on the first push.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_lifespan_starts_and_shuts_down_cleanly():
    # Entering/exiting the context runs the lifespan: shutdown closes the shared HTTP
    # client singletons, which must be a no-op even when no request ever used them.
    with TestClient(app):
        pass
