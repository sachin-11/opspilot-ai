"""Factories for the long-lived PostgreSQL and Redis clients owned by the app lifespan."""

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opspilot_api.config import Settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    # Engine creation is lazy: no connection is opened until first use, so the API
    # can start while PostgreSQL is down and report that through /health/ready.
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        connect_args={
            "connect_timeout": max(1, int(settings.DEPENDENCY_TIMEOUT_SECONDS)),
            "options": f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_SECONDS * 1000}",
        },
    )


def create_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.REDIS_URL.get_secret_value(),
        socket_connect_timeout=settings.DEPENDENCY_TIMEOUT_SECONDS,
        socket_timeout=settings.DEPENDENCY_TIMEOUT_SECONDS,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
