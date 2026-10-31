from typing import Any

import pytest
import sentry_sdk

from opspilot_api.error_tracking import init_error_tracking, scrub_event

from .conftest import make_settings

DSN = "https://publickey@o0.ingest.example.invalid/42"  # not a real project


@pytest.fixture(autouse=True)
def _reset_sentry() -> object:
    yield None
    sentry_sdk.get_client().close()


def test_disabled_without_a_dsn_and_blank_values_count_as_unset() -> None:
    assert init_error_tracking(make_settings()) is False
    assert init_error_tracking(make_settings(SENTRY_DSN="", SENTRY_ENVIRONMENT="")) is False
    assert not sentry_sdk.get_client().is_active()


def test_invalid_dsn_fails_at_startup_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="SENTRY_DSN must look like") as error:
        make_settings(SENTRY_DSN="not-a-dsn-with-secret-token")
    assert "secret-token" not in str(error.value)
    with pytest.raises(ValueError):
        make_settings(SENTRY_TRACES_SAMPLE_RATE=2)


def test_enabled_with_privacy_preserving_options() -> None:
    settings = make_settings(SENTRY_DSN=DSN, SENTRY_TRACES_SAMPLE_RATE=0.25)
    assert init_error_tracking(settings) is True
    options = sentry_sdk.get_client().options
    collection = options["data_collection"]
    assert collection["user_info"] is False
    assert collection["http_bodies"] == []
    assert collection["cookies"]["mode"] == "off"
    assert collection["url_query_params"]["mode"] == "off"
    assert collection["stack_frame_variables"] is False
    assert "authorization" not in collection["http_headers"]["request"]["terms"]
    assert options["traces_sample_rate"] == 0.25
    assert options["environment"] == "test"
    assert options["before_send"] is scrub_event


def test_events_are_scrubbed_of_bodies_cookies_tickets_and_credentials() -> None:
    event = {
        "request": {
            "url": "https://api.example.test/api/v1/ws/workspaces/w/incidents?ticket=SECRET-TICKET",
            "query_string": "ticket=SECRET-TICKET",
            "data": {"password": "hunter2"},
            "cookies": {"opspilot_session": "SECRET-SESSION"},
            "env": {"REMOTE_ADDR": "203.0.113.9"},
            "headers": {
                "Authorization": "Bearer SECRET-TOKEN",
                "X-API-Key": "opk_SECRET",
                "Cookie": "opspilot_session=SECRET-SESSION",
                "User-Agent": "pytest",
                "Content-Type": "application/json",
            },
        },
        "user": {"ip_address": "203.0.113.9", "email": "a@example.test"},
        "message": "boom",
    }
    cleaned = scrub_event(event)
    assert "SECRET" not in repr(cleaned)
    assert "203.0.113.9" not in repr(cleaned)
    assert cleaned["request"]["headers"] == {
        "User-Agent": "pytest",
        "Content-Type": "application/json",
    }
    assert cleaned["request"]["url"] == "https://api.example.test/api/v1/ws/workspaces/w/incidents"
    assert cleaned["message"] == "boom"
    assert scrub_event({"message": "no request"}) == {"message": "no request"}


def test_a_real_request_error_reaches_the_transport_without_sensitive_data() -> None:
    import json

    from fastapi.testclient import TestClient
    from sentry_sdk.transport import Transport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    sent: list[str] = []

    class Capture(Transport):
        def capture_envelope(self, envelope: Any) -> None:
            for item in envelope.items:
                if item.headers.get("type") == "event":
                    sent.append(json.dumps(item.payload.json))

    async def boom(request: Request) -> Response:
        password = "-".join(["LOCAL", "VARIABLE", "SECRET"])  # noqa: F841 - built at runtime
        raise RuntimeError("expected failure")

    settings = make_settings(SENTRY_DSN=DSN)
    init_error_tracking(settings)
    sentry_sdk.get_client().transport = Capture(sentry_sdk.get_client().options)
    app = Starlette(routes=[Route("/boom", boom, methods=["POST"])])
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/boom?ticket=SECRET-TICKET",
            content=b'{"password": "BODY-SECRET"}',
            headers={"Authorization": "Bearer HEADER-SECRET", "Cookie": "s=COOKIE-SECRET"},
        )
    sentry_sdk.flush()
    assert sent, "the error event should have reached the transport"
    payload = "".join(sent)
    assert "expected failure" in payload
    for secret in (
        "SECRET-TICKET",
        "BODY-SECRET",
        "HEADER-SECRET",
        "COOKIE-SECRET",
        "LOCAL-VARIABLE-SECRET",
    ):
        assert secret not in payload, secret
