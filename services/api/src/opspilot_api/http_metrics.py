"""Prometheus metrics for every HTTP request served by the API.

Labels are bounded on purpose: the *route template* (``/api/v1/workspaces/{workspace_id}``), never
the raw path, so identifiers cannot create unbounded series; unmatched paths (404s, scanners) share
one ``unmatched`` label. No request data, headers, or identities are recorded.
"""

import time

from prometheus_client import Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUESTS = Counter(
    "opspilot_http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
LATENCY = Histogram(
    "opspilot_http_request_duration_seconds",
    "HTTP request latency until the response has been sent",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
IN_PROGRESS = Gauge("opspilot_http_requests_in_progress", "HTTP requests currently being handled")

UNMATCHED = "unmatched"


def route_label(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED


class HttpMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status_code = 500  # an exception that escapes the app is reported as a server error
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        IN_PROGRESS.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            IN_PROGRESS.dec()
            # The router stores the matched route on the shared scope, so it is readable here.
            route = route_label(scope)
            method = scope["method"]
            REQUESTS.labels(method, route, str(status_code)).inc()
            LATENCY.labels(method, route).observe(time.perf_counter() - started)
