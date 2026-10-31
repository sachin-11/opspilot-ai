from typing import Any

import pytest
from sqlalchemy.pool import QueuePool

from opspilot_api import infrastructure

from .conftest import make_settings


def test_engine_uses_the_configured_pool_limits() -> None:
    settings = make_settings(
        DB_POOL_SIZE=7, DB_MAX_OVERFLOW=3, DB_POOL_TIMEOUT_SECONDS=4, DB_POOL_RECYCLE_SECONDS=600
    )
    engine = infrastructure.create_db_engine(settings)
    pool = engine.sync_engine.pool
    assert isinstance(pool, QueuePool)
    assert pool.size() == 7
    assert pool._max_overflow == 3
    assert pool.timeout() == 4
    assert pool._recycle == 600


def test_statement_timeout_is_sent_to_the_server_in_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_create(*args: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(infrastructure, "create_async_engine", fake_create)
    infrastructure.create_db_engine(make_settings(DB_STATEMENT_TIMEOUT_SECONDS=45))
    assert captured["connect_args"]["options"] == "-c statement_timeout=45000"
    assert captured["pool_pre_ping"] is True


def test_defaults_are_bounded_and_invalid_values_fail_at_startup() -> None:
    settings = make_settings()
    assert (settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW) == (10, 10)
    assert settings.DB_POOL_TIMEOUT_SECONDS == 10
    for bad in (
        {"DB_POOL_SIZE": 0},
        {"DB_MAX_OVERFLOW": -1},
        {"DB_POOL_TIMEOUT_SECONDS": 0},
        {"DB_POOL_RECYCLE_SECONDS": 5},
        {"DB_STATEMENT_TIMEOUT_SECONDS": 0},
    ):
        with pytest.raises(ValueError):
            make_settings(**bad)
