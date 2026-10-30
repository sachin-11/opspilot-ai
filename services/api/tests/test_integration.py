"""Needs live PostgreSQL + Redis configured via the environment, with migrations applied.

Run with: ``uv run pytest -m integration``
"""

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine, text

from opspilot_api.config import get_settings
from opspilot_api.incidents.schemas import RealtimeEnvelope
from opspilot_api.incidents.service import utcnow
from opspilot_api.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
def live_client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    with TestClient(create_app(get_settings())) as client:
        yield client


def test_ready_with_real_dependencies(live_client: TestClient) -> None:
    response = live_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["dependencies"] == {"postgres": "up", "redis": "up"}


def test_alembic_enabled_vector_extension() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
    engine.dispose()
    assert found == "vector"


def create_workspace(client: TestClient, name: str = "Test Workspace") -> dict[str, object]:
    suffix = uuid4().hex[:10]
    response = client.post("/api/v1/workspaces", json={"name": name, "slug": f"test-{suffix}"})
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def create_service(
    client: TestClient, workspace_id: str, slug: str | None = None
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/services",
        json={
            "name": "Checkout API",
            "slug": slug or f"checkout-{uuid4().hex[:8]}",
            "description": "integration test",
            "environment": "production",
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


def test_workspace_and_service_tenant_boundaries(live_client: TestClient) -> None:
    first = create_workspace(live_client, "First")
    second = create_workspace(live_client, "Second")
    first_id, second_id = str(first["id"]), str(second["id"])
    duplicate_workspace = live_client.post(
        "/api/v1/workspaces", json={"name": "Duplicate", "slug": first["slug"]}
    )
    assert duplicate_workspace.status_code == 409
    service = create_service(live_client, first_id, "same-slug")
    other = create_service(live_client, second_id, "same-slug")
    assert service["slug"] == other["slug"]
    assert (
        live_client.get(f"/api/v1/workspaces/{second_id}/services/{service['id']}").status_code
        == 404
    )
    duplicate = live_client.post(
        f"/api/v1/workspaces/{first_id}/services",
        json={
            "name": "Duplicate",
            "slug": "same-slug",
            "environment": "staging",
        },
    )
    assert duplicate.status_code == 409


def test_incident_lifecycle_timeline_and_concurrency(live_client: TestClient) -> None:
    workspace = create_workspace(live_client)
    workspace_id = str(workspace["id"])
    service = create_service(live_client, workspace_id)
    created = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        json={
            "title": "Checkout errors",
            "description": "5xx rate high",
            "service_id": service["id"],
            "severity": "sev2",
        },
    )
    assert created.status_code == 201
    incident = created.json()
    incident_id = incident["id"]
    assert incident["version"] == 1
    investigating = live_client.patch(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident_id}/status",
        json={"status": "investigating", "version": 1},
    )
    assert investigating.status_code == 200
    assert investigating.json()["acknowledged_at"] is not None
    stale = live_client.patch(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident_id}/severity",
        json={"severity": "sev1", "version": 1},
    )
    assert stale.status_code == 409
    current = investigating.json()
    severity = live_client.patch(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident_id}/severity",
        json={"severity": "sev1", "version": current["version"]},
    )
    assert severity.status_code == 200
    note = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident_id}/notes",
        json={"summary": "Rollback started", "version": severity.json()["version"]},
    )
    assert note.status_code == 201
    invalid = live_client.patch(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident_id}/status",
        json={"status": "open", "version": severity.json()["version"] + 1},
    )
    assert invalid.status_code == 422
    timeline = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident_id}/timeline"
    ).json()
    assert [item["event_type"] for item in timeline] == [
        "incident_created",
        "status_changed",
        "severity_changed",
        "note_added",
    ]


def test_resolved_incident_can_be_reopened(live_client: TestClient) -> None:
    workspace_id = str(create_workspace(live_client)["id"])
    incident = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        json={
            "title": "Reopen test",
            "severity": "sev3",
        },
    ).json()
    resolved = live_client.patch(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/status",
        json={"status": "resolved", "version": incident["version"]},
    ).json()
    reopened = live_client.patch(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/status",
        json={"status": "investigating", "version": resolved["version"]},
    )
    assert reopened.status_code == 200
    assert reopened.json()["resolved_at"] is None
    timeline = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/timeline"
    ).json()
    assert timeline[-1]["event_type"] == "incident_reopened"


def test_event_idempotency_correlation_filters_and_pagination(live_client: TestClient) -> None:
    workspace_id = str(create_workspace(live_client)["id"])
    service = create_service(live_client, workspace_id)
    payload = {
        "service_id": service["id"],
        "external_id": "alert-123",
        "source": "test-monitor",
        "event_type": "http_error_rate",
        "severity": "sev2",
        "summary": "Checkout 5xx high",
        "error_signature": "checkout.http.5xx",
        "attributes": {"rate": 0.2},
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    path = f"/api/v1/workspaces/{workspace_id}/events"
    first = live_client.post(path, headers={"Idempotency-Key": "event-1"}, json=payload)
    duplicate = live_client.post(path, headers={"Idempotency-Key": "event-1"}, json=payload)
    correlated = live_client.post(path, headers={"Idempotency-Key": "event-2"}, json=payload)
    assert first.status_code == duplicate.status_code == correlated.status_code == 201
    assert first.json() == duplicate.json()
    assert correlated.json()["result"] == "correlated"
    assert correlated.json()["incident_id"] == first.json()["incident_id"]
    second_incident = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        json={
            "title": "Second sev2 incident",
            "service_id": service["id"],
            "severity": "sev2",
        },
    )
    assert second_incident.status_code == 201
    listing = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        params={
            "status": "open",
            "severity": "sev2",
            "service_id": str(service["id"]),
            "limit": 1,
        },
    )
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1
    assert listing.json()["next_cursor"] is not None
    next_page = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        params={
            "status": "open",
            "severity": "sev2",
            "service_id": str(service["id"]),
            "limit": 1,
            "cursor": listing.json()["next_cursor"],
        },
    )
    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["id"] != listing.json()["items"][0]["id"]


def test_concurrent_duplicate_ingestion_creates_one_event(live_client: TestClient) -> None:
    workspace_id = str(create_workspace(live_client)["id"])
    service = create_service(live_client, workspace_id)
    payload = {
        "service_id": service["id"],
        "source": "test-monitor",
        "event_type": "queue_depth",
        "severity": "sev3",
        "summary": "Queue depth high",
        "error_signature": "queue.depth",
        "attributes": {},
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    path = f"/api/v1/workspaces/{workspace_id}/events"

    def send() -> tuple[int, dict[str, object]]:
        response = live_client.post(
            path, headers={"Idempotency-Key": "concurrent-key"}, json=payload
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert [item[0] for item in results] == [201, 201]
    assert results[0][1] == results[1][1]


def test_cross_workspace_incident_returns_404(live_client: TestClient) -> None:
    first_id = str(create_workspace(live_client)["id"])
    second_id = str(create_workspace(live_client)["id"])
    incident = live_client.post(
        f"/api/v1/workspaces/{first_id}/incidents",
        json={
            "title": "Private to first",
            "severity": "sev4",
        },
    ).json()
    assert (
        live_client.get(f"/api/v1/workspaces/{second_id}/incidents/{incident['id']}").status_code
        == 404
    )


def test_websocket_workspace_isolation_and_pubsub_cleanup(live_client: TestClient) -> None:
    first_id = str(create_workspace(live_client)["id"])
    second_id = str(create_workspace(live_client)["id"])
    redis = Redis.from_url(get_settings().REDIS_URL.get_secret_value())
    first_channel = f"opspilot:workspace:{first_id}:incidents"
    second_channel = f"opspilot:workspace:{second_id}:incidents"
    try:
        with live_client.websocket_connect(f"/api/v1/ws/workspaces/{first_id}/incidents") as socket:
            connected = socket.receive_json()
            assert connected["type"] == "connection.established"
            counts = dict(redis.pubsub_numsub(first_channel, second_channel))
            assert counts[first_channel.encode()] == 1
            assert counts[second_channel.encode()] == 0
            envelope = RealtimeEnvelope(
                event_id=uuid4(),
                type="incident.status_changed",
                workspace_id=UUID(first_id),
                incident_id=None,
                occurred_at=utcnow(),
                data={},
            )
            redis.publish(first_channel, envelope.model_dump_json())
            received = json.loads(socket.receive_text())
            assert received["workspace_id"] == first_id
        for _ in range(20):
            if dict(redis.pubsub_numsub(first_channel))[first_channel.encode()] == 0:
                break
            time.sleep(0.05)
        assert dict(redis.pubsub_numsub(first_channel))[first_channel.encode()] == 0
    finally:
        redis.close()
