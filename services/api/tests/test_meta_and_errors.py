import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opspilot_api.health.router import get_readiness_checker


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


def test_unclassified_routes_are_denied(app: FastAPI, client: TestClient) -> None:
    @app.get("/not-classified")
    async def not_classified() -> dict[str, str]:
        return {"ok": "should never be reachable"}

    response = client.get("/not-classified")
    assert response.status_code == 403
    assert "should never" not in response.text


def test_unhandled_exception_is_masked(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opspilot_api.auth.route_policy import RoutePolicy, classify

    real = classify
    monkeypatch.setattr(
        "opspilot_api.auth.dependencies.classify",
        lambda method, template: (
            RoutePolicy(public=True) if template == "/boom" else real(method, template)
        ),
    )

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


def test_a_database_outage_is_a_retryable_503_not_a_500(app: FastAPI) -> None:
    from sqlalchemy.exc import InterfaceError, OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    class Failing:
        def __init__(self, error: Exception) -> None:
            self.error = error

        async def check(self) -> object:
            raise self.error

    errors: list[Exception] = [
        OperationalError("SELECT 1", {}, Exception("connection refused host=db user=app")),
        InterfaceError("SELECT 1", {}, Exception("connection is closed")),
        PoolTimeoutError("QueuePool limit of size 10 overflow 10 reached"),
    ]
    for error in errors:
        app.dependency_overrides[get_readiness_checker] = lambda e=error: Failing(e)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        body = response.json()["error"]
        assert body["code"] == "dependency_unavailable"
        assert "host=db" not in response.text and "user=app" not in response.text
