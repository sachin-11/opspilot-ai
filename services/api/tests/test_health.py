from fastapi import FastAPI
from fastapi.testclient import TestClient

from opspilot_api.health.service import DependencyState

from .conftest import TEST_PASSWORD, override_readiness

UP = DependencyState.UP
DOWN = DependencyState.DOWN


def test_live_ok_without_dependencies(client: TestClient) -> None:
    # Dependencies point at a closed port; liveness must not care.
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "opspilot-api"}


def test_ready_ok_when_all_dependencies_up(app: FastAPI, client: TestClient) -> None:
    override_readiness(app, UP, UP)
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"postgres": "up", "redis": "up"},
    }


def test_ready_503_when_redis_down(app: FastAPI, client: TestClient) -> None:
    override_readiness(app, UP, DOWN)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "dependencies": {"postgres": "up", "redis": "down"},
    }


def test_ready_503_when_postgres_down(app: FastAPI, client: TestClient) -> None:
    override_readiness(app, DOWN, UP)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["dependencies"] == {"postgres": "down", "redis": "up"}


def test_ready_with_real_probes_reports_down_and_leaks_nothing(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "dependencies": {"postgres": "down", "redis": "down"},
    }
    for secret in (TEST_PASSWORD, "redis-test-secret", "test_user", "127.0.0.1"):
        assert secret not in response.text
