"""Needs live PostgreSQL + Redis configured via the environment, with migrations applied.

Run with: ``uv run pytest -m integration``
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from opspilot_api.config import get_settings
from opspilot_api.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def live_client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    with TestClient(create_app(get_settings())) as client:
        yield client


def test_ready_with_real_dependencies(live_client: TestClient) -> None:
    response = live_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["dependencies"] == {"postgres": "up", "redis": "up"}


def test_alembic_enabled_vector_extension() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
    engine.dispose()
    assert found == "vector"
