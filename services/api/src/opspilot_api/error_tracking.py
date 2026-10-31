"""Optional Sentry error tracking with aggressive scrubbing.

Disabled unless ``SENTRY_DSN`` is set. Events never carry request bodies, cookies, query strings
(WebSocket tickets travel there), authorization or API-key headers, client IPs, or local variables,
so a crash report cannot leak a credential or customer data.
"""

import logging
from typing import Any

import sentry_sdk

from opspilot_api.config import SERVICE_NAME, Settings

logger = logging.getLogger(__name__)

# Headers worth keeping for debugging; everything else (authorization, cookie, x-api-key, ...) goes.
SAFE_HEADERS = frozenset({"user-agent", "content-type", "content-length", "x-request-id", "accept"})


def scrub_event(event: Any, hint: Any = None) -> Any:
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        request.pop("query_string", None)
        request.pop("env", None)  # includes REMOTE_ADDR
        url = request.get("url")
        if isinstance(url, str):
            request["url"] = url.split("?", 1)[0]
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                name: value for name, value in headers.items() if name.lower() in SAFE_HEADERS
            }
    event.pop("user", None)
    return event


def init_error_tracking(settings: Settings) -> bool:
    """Initialise Sentry if configured. Returns whether error tracking is active."""
    if settings.SENTRY_DSN is None:
        return False
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN.get_secret_value(),
        environment=settings.SENTRY_ENVIRONMENT or settings.APP_ENV.value,
        server_name=SERVICE_NAME,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        # Collect nothing sensitive by default; scrub_event below is a second, independent layer.
        data_collection={
            "user_info": False,
            "cookies": {"mode": "off"},
            "http_headers": {"request": {"mode": "allowlist", "terms": sorted(SAFE_HEADERS)}},
            "http_bodies": [],
            "url_query_params": {"mode": "off"},
            "stack_frame_variables": False,
            "database_query_data": False,
            "queues": False,
            "gen_ai": {"inputs": False, "outputs": False},
            "graphql": {"document": False, "variables": False},
        },
        before_send=scrub_event,
    )
    logger.info("error tracking enabled", extra={"environment": settings.APP_ENV.value})
    return True
