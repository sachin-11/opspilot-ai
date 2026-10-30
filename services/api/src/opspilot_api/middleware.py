"""ASGI middleware: correlation IDs and access logging."""

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opspilot_api.logging_config import correlation_id_var

CORRELATION_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

logger = logging.getLogger("opspilot_api.access")
MAX_EVENT_REQUEST_BYTES = 128 * 1024


class EventPayloadLimitMiddleware:
    """Reject oversized ingestion requests before buffering/parsing their body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].startswith("/api/v1/workspaces/")
            and scope["path"].endswith("/events")
        ):
            raw_length = dict(scope["headers"]).get(b"content-length")
            if raw_length:
                try:
                    too_large = int(raw_length) > MAX_EVENT_REQUEST_BYTES
                except ValueError:
                    too_large = True
                if too_large:
                    correlation_id = scope.get("state", {}).get("correlation_id")
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "content_too_large",
                                "message": "Event request exceeds 128 KiB",
                                "correlation_id": correlation_id,
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(CORRELATION_HEADER.lower().encode(), b"").decode()
        correlation_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        token = correlation_id_var.set(correlation_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            correlation_id_var.reset(token)
