import json

from kinglet import TestClient

from cartray.worker import app


def test_health_endpoint_declares_test_only_mode():
    status, _headers, body = TestClient(app).request("GET", "/health")

    assert status == 200
    assert json.loads(body) == {"service": "cartray", "mode": "test-only", "status": "ok"}
