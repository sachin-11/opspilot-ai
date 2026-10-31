import asyncio
import os
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opspilot_api.auth.providers import issue_local_token
from opspilot_api.config import Environment, Settings
from opspilot_api.health.router import get_readiness_checker
from opspilot_api.health.service import DependencyState, DependencyStates, ReadinessReport
from opspilot_api.main import create_app

# The whole suite shares a few deterministic identities; relax (not disable) rate limits for it.
# Rate-limit tests build their own settings with a multiplier of 1.
os.environ.setdefault("AUTH_RATE_LIMIT_MULTIPLIER", "1000")

if sys.platform == "win32":
    # psycopg's async driver cannot run on the default Proactor loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

TEST_PASSWORD = "test-only-password"  # not a real credential


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


TEST_SUBJECT = "test-owner"
TEST_EMAIL = "test-owner@test.local"


def auth_headers(subject: str = TEST_SUBJECT, email: str = TEST_EMAIL) -> dict[str, str]:
    """Deterministic signed local-provider token (never a real IdP; CI uses this too)."""
    token = issue_local_token(make_settings(), subject, email, subject)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def authenticated_test_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every TestClient acts as the deterministic test user unless a test sets its own headers.

    Tests that exercise anonymous or other-user behavior pass ``headers={...}`` explicitly (or
    ``authenticate=False``) when constructing the client.
    """
    original = TestClient.__init__

    def init(self: TestClient, *args: Any, authenticate: bool = True, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        if authenticate and "authorization" not in {k.lower() for k in self.headers}:
            self.headers.update(auth_headers())

    monkeypatch.setattr(TestClient, "__init__", init)


def anonymous_client(app: FastAPI, **kwargs: Any) -> TestClient:
    """A client that sends no credentials (the default test identity is not attached)."""
    return TestClient(app, authenticate=False, **kwargs)  # type: ignore[call-arg]


@pytest.fixture
def app() -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
