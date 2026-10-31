import asyncio
import sys
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opspilot_api.config import Environment, Settings
from opspilot_api.health.router import get_readiness_checker
from opspilot_api.health.service import DependencyState, DependencyStates, ReadinessReport
from opspilot_api.main import create_app

if sys.platform == "win32":
    # psycopg's async driver cannot run on the default Proactor loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TEST_PASSWORD = "test-only-password"  # not a real credential
TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcdef"  # not a real credential


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "APP_ENV": Environment.TEST,
        "CORS_ORIGINS": "http://localhost:3300",
        "POSTGRES_DB": "test_db",
        "POSTGRES_USER": "test_user",
        "POSTGRES_PASSWORD": TEST_PASSWORD,
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": 1,  # nothing listens here
        "REDIS_URL": "redis://:redis-test-secret@127.0.0.1:1/0",
        "AUTH_JWT_SECRET": TEST_JWT_SECRET,
        "DEPENDENCY_TIMEOUT_SECONDS": 0.5,
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


class FakeChecker:
    def __init__(self, postgres: DependencyState, redis: DependencyState) -> None:
        self._states = DependencyStates(postgres=postgres, redis=redis)

    async def check(self) -> ReadinessReport:
        up = all(s is DependencyState.UP for s in (self._states.postgres, self._states.redis))
        return ReadinessReport(status="ready" if up else "degraded", dependencies=self._states)


def override_readiness(app: FastAPI, postgres: DependencyState, redis: DependencyState) -> None:
    app.dependency_overrides[get_readiness_checker] = lambda: FakeChecker(postgres, redis)


@pytest.fixture
def app() -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
