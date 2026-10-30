"""Readiness probing for required infrastructure dependencies."""

import asyncio
import logging
from collections.abc import Coroutine
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


class DependencyState(StrEnum):
    UP = "up"
    DOWN = "down"


class DependencyStates(BaseModel):
    postgres: DependencyState
    redis: DependencyState


class ReadinessReport(BaseModel):
    status: str
    dependencies: DependencyStates

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


class ReadinessChecker(Protocol):
    async def check(self) -> ReadinessReport: ...


class ReadinessService:
    def __init__(self, engine: AsyncEngine, redis: Redis, timeout_seconds: float) -> None:
        self._engine = engine
        self._redis = redis
        self._timeout = timeout_seconds

    async def check(self) -> ReadinessReport:
        postgres, redis = await asyncio.gather(self._check_postgres(), self._check_redis())
        ready = postgres is DependencyState.UP and redis is DependencyState.UP
        return ReadinessReport(
            status="ready" if ready else "degraded",
            dependencies=DependencyStates(postgres=postgres, redis=redis),
        )

    async def _check_postgres(self) -> DependencyState:
        async def probe() -> None:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        return await self._run("postgres", probe())

    async def _check_redis(self) -> DependencyState:
        async def probe() -> None:
            await self._redis.ping()

        return await self._run("redis", probe())

    async def _run(self, name: str, probe: Coroutine[Any, Any, None]) -> DependencyState:
        try:
            await asyncio.wait_for(probe, timeout=self._timeout)
        except Exception as exc:
            # Only the exception class is logged: messages can embed connection details.
            logger.warning(
                "dependency unavailable",
                extra={"dependency": name, "error_type": type(exc).__name__},
            )
            return DependencyState.DOWN
        return DependencyState.UP
