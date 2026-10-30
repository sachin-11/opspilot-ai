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
from opspilot_api.investigations.workflow import WorkflowLimitReached, run_workflow
from opspilot_api.main import create_app
from opspilot_api.worker.jobs import resume_investigation

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


def test_investigation_checkpoint_review_and_tenant_boundary(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "opspilot_api.investigations.router.enqueue_investigation", lambda *args: None
    )
    monkeypatch.setattr(
        "opspilot_api.investigations.router.enqueue_review_resume", lambda *args: None
    )
    workspace_id = str(create_workspace(live_client, "Investigation Workspace")["id"])
    other_workspace_id = str(create_workspace(live_client, "Other Workspace")["id"])
    incident = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        json={
            "title": "Repeated checkout failures",
            "description": "HTTP 500 errors increased after a deploy",
            "severity": "sev2",
        },
    ).json()
    collection_path = f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/investigations"
    created = live_client.post(collection_path, json={"trigger": "manual"})
    assert created.status_code == 202, created.text
    investigation_id = created.json()["investigation_id"]
    assert live_client.post(collection_path, json={"trigger": "manual"}).status_code == 409
    assert (
        live_client.get(
            f"/api/v1/workspaces/{other_workspace_id}/investigations/{investigation_id}"
        ).status_code
        == 404
    )

    settings = get_settings()
    run_workflow(settings, UUID(workspace_id), UUID(investigation_id))
    detail_path = f"/api/v1/workspaces/{workspace_id}/investigations/{investigation_id}"
    awaiting = live_client.get(detail_path)
    assert awaiting.status_code == 200
    body = awaiting.json()
    assert body["status"] == "awaiting_review"
    assert body["report"]["evidence_citations"]
    assert body["evidence"]
    assert any(step["node_name"] == "produce_report" for step in body["steps"])
    step_ids = {step["id"] for step in body["steps"]}
    evidence_ids = {item["id"] for item in body["evidence"]}

    # Re-delivery uses the stable checkpoint thread and does not duplicate durable records.
    run_workflow(settings, UUID(workspace_id), UUID(investigation_id))
    repeated = live_client.get(detail_path).json()
    assert {step["id"] for step in repeated["steps"]} == step_ids
    assert {item["id"] for item in repeated["evidence"]} == evidence_ids

    approved = live_client.post(
        f"{detail_path}/review",
        json={"action": "approved", "version": repeated["version"]},
    )
    assert approved.status_code == 202, approved.text
    review_id = live_client.get(detail_path).json()["reviews"][-1]["id"]
    resume_investigation(workspace_id, investigation_id, review_id)
    completed = live_client.get(detail_path).json()
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None
    timeline = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/timeline"
    ).json()
    assert timeline[-1]["event_type"] == "investigation_completed"


def test_investigation_cancel_is_idempotent(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "opspilot_api.investigations.router.enqueue_investigation", lambda *args: None
    )
    workspace_id = str(create_workspace(live_client, "Cancellation Workspace")["id"])
    incident = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        json={"title": "Cancel investigation", "severity": "sev3"},
    ).json()
    created = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/investigations",
        json={"trigger": "manual"},
    ).json()
    path = f"/api/v1/workspaces/{workspace_id}/investigations/{created['investigation_id']}/cancel"
    first = live_client.post(path)
    second = live_client.post(path)
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "cancelled"


def test_investigation_revision_then_rejection(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "opspilot_api.investigations.router.enqueue_investigation", lambda *args: None
    )
    monkeypatch.setattr(
        "opspilot_api.investigations.router.enqueue_review_resume", lambda *args: None
    )
    workspace_id = str(create_workspace(live_client, "Revision Workspace")["id"])
    incident = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        json={"title": "Revision investigation", "severity": "sev2"},
    ).json()
    created = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/investigations",
        json={"trigger": "manual"},
    ).json()
    investigation_id = created["investigation_id"]
    settings = get_settings()
    run_workflow(settings, UUID(workspace_id), UUID(investigation_id))
    detail_path = f"/api/v1/workspaces/{workspace_id}/investigations/{investigation_id}"
    first = live_client.get(detail_path).json()

    revision = live_client.post(
        f"{detail_path}/review",
        json={
            "action": "revision_requested",
            "comment": "Clarify the uncertainty and verification steps.",
            "version": first["version"],
        },
    )
    assert revision.status_code == 202, revision.text
    revision_detail = live_client.get(detail_path).json()
    resume_investigation(workspace_id, investigation_id, revision_detail["reviews"][-1]["id"])
    revised = live_client.get(detail_path).json()
    assert revised["status"] == "awaiting_review"
    assert revised["revision_count"] == 1
    assert any(step["node_name"] == "revision" for step in revised["steps"])
    stale = live_client.post(
        f"{detail_path}/review",
        json={
            "action": "rejected",
            "comment": "Reject stale review.",
            "version": first["version"],
        },
    )
    assert stale.status_code == 409

    rejected = live_client.post(
        f"{detail_path}/review",
        json={
            "action": "rejected",
            "comment": "Evidence is not sufficient for operational use.",
            "version": revised["version"],
        },
    )
    assert rejected.status_code == 202, rejected.text
    rejected_detail = live_client.get(detail_path).json()
    resume_investigation(workspace_id, investigation_id, rejected_detail["reviews"][-1]["id"])
    terminal = live_client.get(detail_path).json()
    assert terminal["status"] == "rejected"
    assert terminal["report"] is not None
    assert [review["action"] for review in terminal["reviews"]] == [
        "revision_requested",
        "rejected",
    ]


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"AI_MAX_TOOL_CALLS": 1}, "maximum tool call limit"),
        (
            {
                "AI_INPUT_PRICE_PER_MILLION": 1000.0,
                "AI_OUTPUT_PRICE_PER_MILLION": 1000.0,
                "AI_MAX_COST_USD": 0.001,
            },
            "maximum investigation cost",
        ),
    ],
)
def test_investigation_circuit_breakers_fail_safely(
    live_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    expected_message: str,
) -> None:
    monkeypatch.setattr(
        "opspilot_api.investigations.router.enqueue_investigation", lambda *args: None
    )
    workspace_id = str(create_workspace(live_client, "Limit Workspace")["id"])
    incident = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        json={"title": "Bounded investigation", "severity": "sev3"},
    ).json()
    investigation_id = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident['id']}/investigations",
        json={"trigger": "manual"},
    ).json()["investigation_id"]
    settings = get_settings().model_copy(update=overrides)
    with pytest.raises(WorkflowLimitReached, match=expected_message):
        run_workflow(settings, UUID(workspace_id), UUID(investigation_id))
    detail = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/investigations/{investigation_id}"
    ).json()
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "limit_reached"
    assert expected_message in detail["failure_message"]


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
