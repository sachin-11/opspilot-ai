from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from opspilot_api.incidents.models import Severity
from opspilot_api.incidents.schemas import EventIngestRequest
from opspilot_api.incidents.service import event_fingerprint, normalize_slug


def test_slug_normalization_is_deterministic() -> None:
    assert normalize_slug("  Checkout API / Production ") == "checkout-api-production"


def test_event_fingerprint_normalizes_case_and_whitespace() -> None:
    workspace_id = uuid4()
    service_id = uuid4()
    base = {
        "service_id": service_id,
        "source": "monitor",
        "severity": Severity.SEV2,
        "summary": "variable message 123",
        "occurred_at": datetime.now(UTC),
    }
    first = EventIngestRequest(event_type="HTTP Error", error_signature=" CHECKOUT.5XX ", **base)
    second = EventIngestRequest(event_type=" http   error ", error_signature="checkout.5xx", **base)
    assert event_fingerprint(workspace_id, first) == event_fingerprint(workspace_id, second)


@pytest.mark.parametrize("key", ["authorization", "cookie", "access_token", "api-key", "password"])
def test_event_attributes_reject_sensitive_keys(key: str) -> None:
    with pytest.raises(PydanticValidationError):
        EventIngestRequest(
            service_id=uuid4(),
            source="monitor",
            event_type="error",
            severity=Severity.SEV3,
            summary="test",
            occurred_at=datetime.now(UTC),
            attributes={key: "must-not-store"},
        )


def test_event_attributes_reject_payload_over_64_kib() -> None:
    with pytest.raises(PydanticValidationError):
        EventIngestRequest(
            service_id=uuid4(),
            source="monitor",
            event_type="error",
            severity=Severity.SEV3,
            summary="test",
            occurred_at=datetime.now(UTC),
            attributes={"blob": "x" * (65 * 1024)},
        )


def test_event_request_over_128_kib_is_rejected_before_database(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/workspaces/{uuid4()}/events",
        headers={"Idempotency-Key": "oversized"},
        json={"blob": "x" * (129 * 1024)},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "content_too_large"
