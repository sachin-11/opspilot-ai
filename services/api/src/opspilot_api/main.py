"""FastAPI application factory and lifecycle management."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from opspilot_api import v1
from opspilot_api.auth.dependencies import authorize_request
from opspilot_api.auth.providers import create_identity_provider
from opspilot_api.auth.ratelimit import RateLimiter
from opspilot_api.auth.router import router as auth_router
from opspilot_api.config import APP_NAME, APP_VERSION, Settings, get_settings
from opspilot_api.errors import register_exception_handlers
from opspilot_api.health.router import router as health_router
from opspilot_api.http_metrics import HttpMetricsMiddleware
from opspilot_api.incidents.router import router as incidents_router
from opspilot_api.infrastructure import (
    create_db_engine,
    create_redis_client,
    create_session_factory,
)
from opspilot_api.investigations.router import router as investigations_router
from opspilot_api.knowledge.router import router as knowledge_router
from opspilot_api.llmops.router import router as llmops_router
from opspilot_api.logging_config import configure_logging
from opspilot_api.middleware import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    EventPayloadLimitMiddleware,
)
from opspilot_api.observability.router import router as observability_router
from opspilot_api.remediation.router import router as remediation_router
from opspilot_api.repositories.router import router as repositories_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.db_engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(app.state.db_engine)
    app.state.redis = create_redis_client(settings)
    app.state.rate_limiter = RateLimiter(app.state.redis, settings)
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

    # Fails startup on misconfiguration (e.g. local auth in production).
    identity_provider = create_identity_provider(settings)
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(authorize_request)],
    )
    app.state.settings = settings
    app.state.identity_provider = identity_provider

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            CORRELATION_HEADER,
            "Content-Type",
            "Idempotency-Key",
            "Authorization",
            "X-API-Key",
        ],
        expose_headers=[CORRELATION_HEADER],
    )
    app.add_middleware(EventPayloadLimitMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(HttpMetricsMiddleware)  # outermost: measures everything above

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(v1.router)
    app.include_router(auth_router)
    app.include_router(incidents_router)
    app.include_router(investigations_router)
    app.include_router(knowledge_router)
    app.include_router(repositories_router)
    app.include_router(observability_router)
    app.include_router(remediation_router)
    app.include_router(llmops_router)
    return app
