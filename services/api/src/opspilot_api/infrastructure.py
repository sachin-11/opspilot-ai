"""Factories for the long-lived PostgreSQL and Redis clients owned by the app lifespan."""

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from opspilot_api.config import Settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    # Engine creation is lazy: no connection is opened until first use, so the API
    # can start while PostgreSQL is down and report that through /health/ready.
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": max(1, int(settings.DEPENDENCY_TIMEOUT_SECONDS))},
    )


def create_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        socket_connect_timeout=settings.DEPENDENCY_TIMEOUT_SECONDS,
        socket_timeout=settings.DEPENDENCY_TIMEOUT_SECONDS,
    )
