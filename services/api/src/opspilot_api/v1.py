"""Version 1 of the public API (mounted at ``/api/v1``)."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from opspilot_api.config import APP_NAME, APP_VERSION, SERVICE_NAME, Settings

router = APIRouter(prefix="/api/v1", tags=["meta"])


class MetaResponse(BaseModel):
    name: str
    service: str
    version: str
    environment: str


def build_meta(settings: Settings) -> MetaResponse:
    return MetaResponse(
        name=APP_NAME,
        service=SERVICE_NAME,
        version=APP_VERSION,
        environment=settings.APP_ENV.value,
    )


@router.get("/meta")
async def meta(request: Request) -> MetaResponse:
    return build_meta(request.app.state.settings)
