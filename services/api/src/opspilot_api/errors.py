"""Consistent JSON error responses: ``{"error": {"code", "message", "correlation_id"}}``."""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from opspilot_api.incidents.service import DomainError
from opspilot_api.logging_config import correlation_id_var

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str | None


class ErrorResponse(BaseModel):
    error: ErrorBody


def _response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    # Unhandled errors are rendered outside the correlation middleware, after its
    # context var is reset, so the ID is also kept on the ASGI scope state.
    correlation_id = correlation_id_var.get() or getattr(request.state, "correlation_id", None)
    body = ErrorResponse(error=ErrorBody(code=code, message=message, correlation_id=correlation_id))
    response = JSONResponse(status_code=status_code, content=body.model_dump())
    if correlation_id:
        response.headers["X-Request-ID"] = correlation_id
    return response


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return _response(request, exc.status_code, exc.code, str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
        return _response(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(".".join(str(part) for part in err["loc"]) for err in exc.errors())
        return _response(request, 422, "validation_error", f"Invalid request: {fields}")

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled exception", exc_info=exc)
        return _response(request, 500, "internal_error", "Internal server error")
