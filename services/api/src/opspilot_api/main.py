"""FastAPI application factory and lifecycle management."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from opspilot_api import v1
from opspilot_api.config import APP_NAME, APP_VERSION, Settings, get_settings
from opspilot_api.errors import register_exception_handlers
from opspilot_api.health.router import router as health_router
from opspilot_api.incidents.router import router as incidents_router
from opspilot_api.infrastructure import (
    create_db_engine,
    create_redis_client,
    create_session_factory,
)
from opspilot_api.investigations.router import router as investigations_router
from opspilot_api.knowledge.router import router as knowledge_router
from opspilot_api.logging_config import configure_logging
from opspilot_api.middleware import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    EventPayloadLimitMiddleware,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.db_engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(app.state.db_engine)
    app.state.redis = create_redis_client(settings)
    logger.info(
        "api starting",
        extra={"environment": settings.APP_ENV.value, "version": APP_VERSION},
    )
    try:
        yield
    finally:
        logger.info("api shutting down")
        await app.state.redis.aclose()
        await app.state.db_engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL.value)

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=[CORRELATION_HEADER, "Content-Type", "Idempotency-Key"],
        expose_headers=[CORRELATION_HEADER],
    )
    app.add_middleware(EventPayloadLimitMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(v1.router)
    app.include_router(incidents_router)
    app.include_router(investigations_router)
    app.include_router(knowledge_router)
    return app
