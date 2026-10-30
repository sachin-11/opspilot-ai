from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from opspilot_api.config import SERVICE_NAME
from opspilot_api.health.service import ReadinessChecker, ReadinessReport, ReadinessService

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: str
    service: str


def get_readiness_checker(request: Request) -> ReadinessChecker:
    return ReadinessService(
        engine=request.app.state.db_engine,
        redis=request.app.state.redis,
        timeout_seconds=request.app.state.settings.DEPENDENCY_TIMEOUT_SECONDS,
    )


@router.get("/live")
async def live() -> LiveResponse:
    """Process liveness only. Deliberately independent of PostgreSQL and Redis."""
    return LiveResponse(status="ok", service=SERVICE_NAME)


@router.get(
    "/ready",
    response_model=ReadinessReport,
    responses={503: {"model": ReadinessReport, "description": "A dependency is unavailable"}},
)
async def ready(
    checker: Annotated[ReadinessChecker, Depends(get_readiness_checker)],
) -> JSONResponse:
    report = await checker.check()
    return JSONResponse(
        status_code=200 if report.is_ready else 503,
        content=report.model_dump(mode="json"),
    )
