from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_meta_returns_non_sensitive_metadata(client: TestClient) -> None:
    response = client.get("/api/v1/meta")
    assert response.status_code == 200
    assert response.json() == {
        "name": "OpsPilot AI",
        "service": "opspilot-api",
        "version": "0.1.0",
        "environment": "test",
    }


def test_unknown_route_uses_error_format(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["correlation_id"] == response.headers["X-Request-ID"]


def test_valid_incoming_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "abc-12345678"})
    assert response.headers["X-Request-ID"] == "abc-12345678"


def test_invalid_incoming_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "bad id\t"})
    assert response.headers["X-Request-ID"] != "bad id\t"
    assert len(response.headers["X-Request-ID"]) == 32


def test_unhandled_exception_is_masked(app: FastAPI, client: TestClient) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    response = client.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text
    assert response.json()["error"]["correlation_id"] == response.headers["X-Request-ID"]


def test_cors_allows_configured_origin_only(client: TestClient) -> None:
    allowed = client.get("/health/live", headers={"Origin": "http://localhost:3300"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3300"
    denied = client.get("/health/live", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in denied.headers
