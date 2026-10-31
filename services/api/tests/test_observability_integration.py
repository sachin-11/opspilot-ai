"""Needs live PostgreSQL + Redis with migrations applied (``pytest -m integration``).

Pipeline behavior is exercised with the deterministic in-memory bus; the real Kafka-compatible
broker is covered by ``test_observability_kafka.py``. No external network or credentials.
"""

import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from opspilot_api.auth.providers import LocalProvider
from opspilot_api.config import Environment, Settings
from opspilot_api.incidents.models import Incident, IncidentEvent, IngestedEvent
from opspilot_api.incidents.service import ingest_event
from opspilot_api.investigations.tools import TOOL_REGISTRY, ToolContext, execute_tool
from opspilot_api.investigations.workflow import run_workflow
from opspilot_api.main import create_app
from opspilot_api.observability import pipeline as pipeline_module
from opspilot_api.observability.bus import BusMessage, InMemoryEventBus, Topics
from opspilot_api.observability.models import (
    CloudWatchQueryAudit,
    DeadLetterReplay,
    DeadLetterSignal,
    ObservabilitySource,
    SignalReceipt,
    SourceProvider,
)
from opspilot_api.observability.pipeline import (
    PipelineContext,
    TerminalProcessingError,
    dead_letter,
    process_message,
)
from opspilot_api.observability.signal_schema import (
    CURRENT_SCHEMA_VERSION,
    RawSignalEnvelope,
    SignalProvider,
    partition_key,
)

from .obs_helpers import (
    alert_payload,
    body_of,
    make_source,
    make_workspace,
    memory_bus,
    obs_settings,
    run_async,
    run_pipeline,
    signed_headers,
)
from .repo_helpers import new_incident

pytestmark = pytest.mark.integration


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(obs_settings().database_url)
    with Session(engine) as db:
        yield db
    engine.dispose()


class Api:
    def __init__(self, client: TestClient, bus: InMemoryEventBus, settings: Settings) -> None:
        self.client, self.bus, self.settings = client, bus, settings

    def post(
        self, source: ObservabilitySource, secret: str, payload: dict[str, Any], **kw: Any
    ) -> Any:
        body = body_of(payload)
        extra = kw.pop("headers", {})
        return self.client.post(
            f"/api/v1/ingest/webhooks/{source.public_id}",
            content=body,
            headers=signed_headers(secret, body, **kw) | extra,
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Api]:
    settings = obs_settings(OBS_WEBHOOK_MAX_BYTES=4096)
    bus = memory_bus(settings)
    monkeypatch.setattr(
        "opspilot_api.observability.pipeline.publish_observability_event", lambda *a, **k: None
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.event_bus = bus
        yield Api(client, bus, settings)


def receipts(session: Session, workspace_id: UUID) -> list[SignalReceipt]:
    session.expire_all()
    return list(
        session.scalars(
            select(SignalReceipt)
            .where(SignalReceipt.workspace_id == workspace_id)
            .order_by(SignalReceipt.received_at)
        )
    )


def incident_events(session: Session, workspace_id: UUID) -> tuple[int, int]:
    session.expire_all()
    incidents = session.scalar(
        select(func.count(Incident.id)).where(Incident.workspace_id == workspace_id)
    )
    events = session.scalar(
        select(func.count(IngestedEvent.id)).where(IngestedEvent.workspace_id == workspace_id)
    )
    return incidents or 0, events or 0


# -- webhook ingress ----------------------------------------------------------------------------
def test_signed_webhook_is_durably_accepted_and_published_without_credentials(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    response = api.post(source, secret, alert_payload("evt-1"))
    assert response.status_code == 202 and response.json()["status"] == "accepted"
    topics = Topics.from_settings(api.settings)
    [message] = api.bus.messages(topics.raw)
    assert message.key == partition_key(workspace.id, service.id)
    assert set(message.headers) <= {
        "schema_version",
        "signal_id",
        "workspace_id",
        "source_id",
        "content_type",
    }
    envelope = RawSignalEnvelope.model_validate_json(message.value)
    assert str(envelope.signal_id) == response.json()["signal_id"]
    assert secret not in message.value.decode()
    [receipt] = receipts(session, workspace.id)
    assert receipt.status == "published" and receipt.provider_event_id == "evt-1"
    # Incident creation is asynchronous: nothing was created in the request.
    assert incident_events(session, workspace.id) == (0, 0)


def test_invalid_expired_missing_oversized_and_malformed_requests_are_rejected(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    body = body_of(alert_payload("evt-x"))
    url = f"/api/v1/ingest/webhooks/{source.public_id}"
    wrong = api.client.post(url, content=body, headers=signed_headers("whsec_wrong", body))
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "signature_invalid"
    expired = api.post(source, secret, alert_payload("evt-e"), timestamp=int(time.time()) - 3600)
    assert expired.status_code == 401 and expired.json()["error"]["code"] == "signature_expired"
    missing = api.client.post(url, content=body, headers={"content-type": "application/json"})
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "signature_missing"
    tampered = api.client.post(url, content=body + b" ", headers=signed_headers(secret, body))
    assert tampered.status_code == 401
    assert api.post(source, secret, {"blob": "x" * 5000}).status_code == 413
    junk = b"not json"
    assert (
        api.client.post(url, content=junk, headers=signed_headers(secret, junk)).status_code == 400
    )
    arr = b"[1]"
    assert api.client.post(url, content=arr, headers=signed_headers(secret, arr)).status_code == 400
    assert (
        api.client.post("/api/v1/ingest/webhooks/does-not-exist", content=body).status_code == 404
    )
    assert api.bus.messages(Topics.from_settings(api.settings).raw) == []
    assert receipts(session, workspace.id) == []  # rejected requests leave no state behind
    assert "signature_failures" in generate_latest().decode().replace(
        "webhook_signature", "signature"
    )
    body_text = generate_latest().decode()
    assert secret not in body_text and "whsec_wrong" not in body_text


def test_duplicate_deliveries_are_idempotent_by_event_id_and_idempotency_key(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    first = api.post(source, secret, alert_payload("evt-dup"))
    second = api.post(source, secret, alert_payload("evt-dup", title="Changed body"))
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["status"] == "duplicate"
    assert second.json()["signal_id"] == first.json()["signal_id"]
    keyed_one = api.post(source, secret, alert_payload(), headers={"Idempotency-Key": "abc"})
    keyed_two = api.post(
        source, secret, alert_payload(title="x"), headers={"Idempotency-Key": "abc"}
    )
    assert keyed_one.status_code == 202 and keyed_two.status_code == 200
    unkeyed = alert_payload()
    one, two = api.post(source, secret, unkeyed), api.post(source, secret, unkeyed)
    assert (one.status_code, two.status_code) == (202, 200)  # identical bytes inside one window
    assert len(api.bus.messages(Topics.from_settings(api.settings).raw)) == 3
    assert len(receipts(session, workspace.id)) == 3


def test_disabled_sources_and_cross_workspace_access_are_blocked(
    api: Api, session: Session
) -> None:
    first_ws, first_service = make_workspace(session)
    second_ws, second_service = make_workspace(session)
    source, secret = make_source(session, api.settings, first_ws, first_service)
    other, _ = make_source(session, api.settings, second_ws, second_service)
    assert secret
    base = f"/api/v1/workspaces/{first_ws.id}/observability"
    assert api.client.get(f"{base}/sources/{other.id}").status_code == 404
    assert (
        api.client.get(
            f"/api/v1/workspaces/{second_ws.id}/observability/sources/{source.id}"
        ).status_code
        == 404
    )
    assert (
        api.client.patch(f"{base}/sources/{other.id}", json={"status": "disabled"}).status_code
        == 404
    )
    assert api.client.post(f"{base}/sources/{other.id}/rotate-secret").status_code == 404
    assert api.client.post(f"{base}/sources/{other.id}/test").status_code == 404
    listed = api.client.get(f"{base}/sources").json()
    assert [item["id"] for item in listed] == [str(source.id)]
    assert (
        api.client.patch(f"{base}/sources/{source.id}", json={"status": "disabled"}).json()[
            "status"
        ]
        == "disabled"
    )
    assert api.post(source, secret, alert_payload("evt-d")).status_code == 403
    api.client.patch(f"{base}/sources/{source.id}", json={"status": "active"})
    assert api.post(source, secret, alert_payload("evt-d")).status_code == 202


def test_source_lifecycle_secret_is_shown_once_and_rotation_invalidates_the_old_one(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    base = f"/api/v1/workspaces/{workspace.id}/observability"
    created = api.client.post(
        f"{base}/sources",
        json={"name": "pagerduty", "provider": "generic_webhook", "service_id": str(service.id),
              "environment": "production"},
    )  # fmt: skip
    assert created.status_code == 201
    body = created.json()
    secret = body["webhook_secret"]
    assert (
        secret.startswith("whsec_")
        and body["has_secret"]
        and body["webhook_path"].endswith(body["public_id"])
    )
    for view in (api.client.get(f"{base}/sources/{body['id']}"), api.client.get(f"{base}/sources")):
        assert secret not in view.text
    stored = session.scalar(
        select(ObservabilitySource.secret_reference).where(
            ObservabilitySource.id == UUID(body["id"])
        )
    )
    assert stored and secret not in stored and stored.startswith("enc:v1:")
    source = session.get(ObservabilitySource, UUID(body["id"]))
    assert source is not None
    assert api.post(source, secret, alert_payload("evt-a")).status_code == 202
    rotated = api.client.post(f"{base}/sources/{body['id']}/rotate-secret").json()
    assert rotated["webhook_secret"] != secret and secret not in json.dumps(rotated)
    assert api.post(source, secret, alert_payload("evt-b")).status_code == 401
    assert api.post(source, rotated["webhook_secret"], alert_payload("evt-b")).status_code == 202
    assert api.client.post(
        f"{base}/sources",
        json={"name": "pagerduty", "provider": "generic_webhook", "service_id": str(service.id),
              "environment": "production"},
    ).status_code == 409  # fmt: skip
    other_ws, _ = make_workspace(session)
    foreign_service = api.client.post(
        f"/api/v1/workspaces/{other_ws.id}/observability/sources",
        json={"name": "x", "provider": "generic_webhook", "service_id": str(service.id),
              "environment": "production"},
    )  # fmt: skip
    assert foreign_service.status_code == 404
    bad = api.client.post(
        f"{base}/sources",
        json={"name": "cw", "provider": "aws_cloudwatch", "service_id": str(service.id),
              "environment": "production", "configuration": {"aws_secret_access_key": "x",
                                                              "allowed_log_groups": ["/x"]}},
    )  # fmt: skip
    assert bad.status_code == 422 and "credential" in bad.json()["error"]["message"]
    fixture = api.client.post(
        f"{base}/sources",
        json={"name": "demo", "provider": "demo_fixture", "service_id": str(service.id),
              "environment": "production", "configuration": {"fixture_mode": "throttle"}},
    ).json()  # fmt: skip
    assert fixture["webhook_secret"] is None and fixture["webhook_path"] is None
    tested = api.client.post(f"{base}/sources/{fixture['id']}/test").json()
    assert tested["status"] == "throttled" and not tested["ok"]
    api.client.patch(
        f"{base}/sources/{fixture['id']}", json={"configuration": {"fixture_mode": "ok"}}
    )
    assert api.client.post(f"{base}/sources/{fixture['id']}/test").json()["ok"] is True
    assert api.client.post(f"{base}/sources/{fixture['id']}/rotate-secret").status_code == 422
    missing_key = create_app(obs_settings(OBS_SECRET_ENCRYPTION_KEY=None))
    with TestClient(missing_key) as other:
        result = other.post(
            f"{base}/sources",
            json={"name": "nokey", "provider": "generic_webhook", "service_id": str(service.id),
                  "environment": "production"},
        )  # fmt: skip
        assert (
            result.status_code == 503 and result.json()["error"]["code"] == "secrets_not_configured"
        )


def test_publish_failure_never_returns_202_and_the_retry_completes_the_same_signal(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    api.bus.fail_publishes = 1
    failed = api.post(source, secret, alert_payload("evt-f"))
    assert failed.status_code == 503 and failed.headers["retry-after"]
    assert failed.json()["error"]["code"] == "broker_unavailable"
    [receipt] = receipts(session, workspace.id)
    assert receipt.status == "failed" and receipt.failure_code == "publish_failed"
    assert api.bus.messages(Topics.from_settings(api.settings).raw) == []
    retried = api.post(source, secret, alert_payload("evt-f"))
    assert retried.status_code == 202 and retried.json()["signal_id"] == str(receipt.signal_id)
    assert len(api.bus.messages(Topics.from_settings(api.settings).raw)) == 1
    api.bus.available = False
    assert api.post(source, secret, alert_payload("evt-g")).status_code == 503  # unhealthy broker
    api.bus.available = True


def test_backpressure_returns_honest_status_codes_and_recovers(
    monkeypatch: pytest.MonkeyPatch, session: Session
) -> None:
    settings = obs_settings(
        OBS_SOURCE_RATE_PER_SECOND=2, OBS_LAG_THRESHOLD=3, OBS_DEAD_LETTER_RATE_THRESHOLD=2
    )
    bus = memory_bus(settings)
    events: list[str] = []
    monkeypatch.setattr(
        "opspilot_api.observability.backpressure.publish_observability_event",
        lambda s, w, event, d: events.append(event),
    )
    app = create_app(settings)
    workspace, service = make_workspace(session)
    source, secret = make_source(session, settings, workspace, service)
    assert secret
    with TestClient(app) as client:
        app.state.event_bus = bus
        api_ = Api(client, bus, settings)
        deadline = time.time() + 1.0
        statuses: list[int] = []
        while time.time() < deadline and 429 not in statuses:
            statuses.append(api_.post(source, secret, alert_payload(uuid.uuid4().hex)).status_code)
        assert 429 in statuses
        limited = api_.post(source, secret, alert_payload(uuid.uuid4().hex))
        if limited.status_code == 429:
            assert limited.headers["retry-after"] == "1"
            assert limited.json()["error"]["code"] == "source_rate_limit"
        assert "observability.backpressure_started" in events
        time.sleep(1.1)  # the rate window moves on: pressure clears by itself
        assert api_.post(source, secret, alert_payload(uuid.uuid4().hex)).status_code == 202
        time.sleep(1.1)
        # Consumer lag above the threshold means downstream is unhealthy: 503, not 202.
        topics = Topics.from_settings(settings)
        for _ in range(4):
            bus.publish(topics.raw, "k", b"{}")
        lagging = api_.post(source, secret, alert_payload(uuid.uuid4().hex))
        assert lagging.status_code == 503 and lagging.json()["error"]["code"] == "consumer_lag"
        pressure = client.get(f"/api/v1/workspaces/{workspace.id}/observability/pipeline").json()
        assert (
            pressure["backpressure"]["active"]
            and "consumer_lag" in pressure["backpressure"]["reasons"]
        )
        for message in bus.consume([topics.raw], "opspilot-raw-consumer", 50):
            bus.commit("opspilot-raw-consumer", message)
        time.sleep(1.1)
        assert api_.post(source, secret, alert_payload(uuid.uuid4().hex)).status_code == 202
        assert "observability.backpressure_cleared" in events

        # A burst of dead letters pauses acceptance for the workspace, then the window recovers.
        def add_letter() -> None:
            session.add(DeadLetterSignal(
                workspace_id=workspace.id, original_topic="t", partition=0,
                offset=uuid.uuid4().int % 10**9, payload_hash="a" * 64, failure_code="x",
                failure_message="x", attempt_count=1, first_failed_at=datetime.now(UTC),
                last_failed_at=datetime.now(UTC),
            ))  # fmt: skip

        add_letter()
        add_letter()
        session.commit()
        time.sleep(1.1)
        blocked = api_.post(source, secret, alert_payload(uuid.uuid4().hex))
        assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "dead_letter_rate"
        session.execute(
            update(DeadLetterSignal)
            .where(DeadLetterSignal.workspace_id == workspace.id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=1))
        )
        session.commit()
        time.sleep(1.1)
        assert api_.post(source, secret, alert_payload(uuid.uuid4().hex)).status_code == 202


# -- end-to-end processing, idempotency, restart ------------------------------------------------
def test_signal_flows_to_incident_timeline_and_websocket_without_duplicates(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    for index in range(3):
        assert api.post(source, secret, alert_payload(f"evt-{index}")).status_code == 202
    handled = run_pipeline(api.settings, api.bus)
    assert handled == {"raw": 3, "normalized": 3}
    rows = receipts(session, workspace.id)
    assert {row.status for row in rows} == {"processed"}
    assert all(row.normalized and row.incident_id and row.processed_at for row in rows)
    assert incident_events(session, workspace.id) == (1, 3)  # one incident, three correlated events
    assert sorted(row.ingest_result for row in rows if row.ingest_result) == [
        "correlated",
        "correlated",
        "created",
    ]
    incident_id = rows[0].incident_id
    timeline = session.scalars(
        select(IncidentEvent.event_type).where(IncidentEvent.incident_id == incident_id)
    ).all()
    assert list(timeline).count("alert_received") == 3 and "incident_created" in timeline
    stored = rows[0].normalized or {}
    assert (
        stored["signal_type"] == "alert"
        and stored["severity"] == "sev2"
        and stored["schema_version"] == "1"
    )
    assert api.bus.lag("opspilot-raw-consumer", [Topics.from_settings(api.settings).raw]) == 0
    assert (
        api.bus.lag("opspilot-normalized-consumer", [Topics.from_settings(api.settings).normalized])
        == 0
    )
    assert run_pipeline(api.settings, api.bus) == {"raw": 0, "normalized": 0}
    source_row = session.get(ObservabilitySource, source.id)
    assert source_row is not None and source_row.last_success_at is not None


def test_redelivered_messages_do_not_duplicate_incidents_or_timeline_entries(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    api.post(source, secret, alert_payload("evt-r"))
    run_pipeline(api.settings, api.bus)
    before = incident_events(session, workspace.id)
    timeline_before = session.scalar(
        select(func.count(IncidentEvent.id)).where(IncidentEvent.workspace_id == workspace.id)
    )
    # Consumer crash before commit / restart: both groups redeliver everything from offset 0.
    for group in ("opspilot-raw-consumer", "opspilot-normalized-consumer"):
        api.bus._committed.clear()
        api.bus.restart_consumer(group)
    handled = run_pipeline(api.settings, api.bus)
    assert handled["raw"] >= 1 and handled["normalized"] >= 1
    session.expire_all()
    assert incident_events(session, workspace.id) == before
    assert (
        session.scalar(
            select(func.count(IncidentEvent.id)).where(IncidentEvent.workspace_id == workspace.id)
        )
        == timeline_before
    )
    assert {r.status for r in receipts(session, workspace.id)} == {"processed"}
    # The same normalized record published twice is absorbed by receipt state + Module 1 idempotency.
    topics = Topics.from_settings(api.settings)
    [normalized] = api.bus.messages(topics.normalized)[:1]
    api.bus.publish(topics.normalized, normalized.key, normalized.value, normalized.headers)
    run_pipeline(api.settings, api.bus)
    assert incident_events(session, workspace.id) == before


def test_fingerprint_window_suppresses_repeats_without_event_ids(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    api.post(source, secret, alert_payload())  # same title/signature, different timestamps
    time.sleep(0.01)
    api.post(source, secret, alert_payload())
    run_pipeline(api.settings, api.bus)
    statuses = sorted(r.status for r in receipts(session, workspace.id))
    assert statuses == ["duplicate", "processed"]
    duplicate = next(r for r in receipts(session, workspace.id) if r.status == "duplicate")
    original = next(r for r in receipts(session, workspace.id) if r.status == "processed")
    assert (
        duplicate.duplicate_of == original.signal_id
        and duplicate.incident_id == original.incident_id
    )
    assert incident_events(session, workspace.id) == (1, 1)


def test_provider_event_ids_dedupe_across_different_sources_payload_shapes(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    # Distinct ingress keys but the same provider event id inside the body: caught at ingress.
    assert api.post(source, secret, alert_payload("same-id")).status_code == 202
    assert api.post(source, secret, alert_payload("same-id", title="Other")).status_code == 200
    run_pipeline(api.settings, api.bus)
    assert incident_events(session, workspace.id) == (1, 1)


# -- retry, commit semantics, dead letters, replay -----------------------------------------------
def make_context(settings: Settings, bus: InMemoryEventBus, factory: Any) -> PipelineContext:
    return PipelineContext(settings, bus, factory)


def test_commit_happens_only_after_durable_processing_and_retries_are_bounded(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    api.post(source, secret, alert_payload("evt-c"))
    topics = Topics.from_settings(api.settings)
    calls: list[int] = []
    committed_at_failure: list[int | None] = []

    async def flaky(ctx: PipelineContext, message: BusMessage) -> None:
        calls.append(1)
        committed_at_failure.append(ctx.bus.lag("g", [topics.raw]))
        if len(calls) < 3:
            raise OperationalError("select", {}, Exception("database restarting"))

    async def go(ctx: PipelineContext) -> str:
        [message] = ctx.bus.consume([topics.raw], "g", 1)
        return await process_message(ctx, "g", message, flaky, "raw")

    assert run_async(go, api.settings, api.bus) == "ok"
    assert len(calls) == 3 and committed_at_failure == [
        1,
        1,
        1,
    ]  # offset not committed until success
    assert api.bus.lag("g", [topics.raw]) == 0

    async def always_fails(ctx: PipelineContext, message: BusMessage) -> None:
        raise OperationalError("select", {}, Exception("down"))

    api.post(source, secret, alert_payload("evt-c2"))

    async def exhaust(ctx: PipelineContext) -> str:
        [message] = ctx.bus.consume([topics.raw], "g", 5)
        return await process_message(ctx, "g", message, always_fails, "raw")

    assert run_async(exhaust, api.settings, api.bus) == "retries_exhausted"
    letters = session.scalars(
        select(DeadLetterSignal).where(DeadLetterSignal.workspace_id == workspace.id)
    ).all()
    assert len(letters) == 1 and letters[0].attempt_count == api.settings.OBS_RETRY_MAX_ATTEMPTS
    assert api.bus.messages(topics.dead_letter)  # published to the dead-letter topic
    assert receipts(session, workspace.id)[-1].status == "dead_lettered"


def test_failed_dead_lettering_leaves_the_message_uncommitted(
    api: Api, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    api.post(source, secret, alert_payload("evt-u"))
    topics = Topics.from_settings(api.settings)

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise OperationalError("insert", {}, Exception("db down while dead-lettering"))

    monkeypatch.setattr(pipeline_module, "dead_letter", boom)

    async def terminal(ctx: PipelineContext, message: BusMessage) -> None:
        raise TerminalProcessingError("poison", "invalid_payload")

    async def go(ctx: PipelineContext) -> None:
        [message] = ctx.bus.consume([topics.raw], "g", 1)
        with pytest.raises(OperationalError):
            await process_message(ctx, "g", message, terminal, "raw")

    run_async(go, api.settings, api.bus)
    assert api.bus.lag("g", [topics.raw]) == 1  # not committed: it will be redelivered, not lost


def publish_raw(bus: InMemoryEventBus, settings: Settings, value: bytes, key: str = "k") -> None:
    bus.publish(Topics.from_settings(settings).raw, key, value)


def test_poison_messages_are_dead_lettered_with_sanitized_bounded_payloads(
    api: Api, session: Session
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    other_ws, other_service = make_workspace(session)
    foreign, _ = make_source(session, api.settings, other_ws, other_service)
    valid = RawSignalEnvelope(
        schema_version=CURRENT_SCHEMA_VERSION, signal_id=uuid.uuid4(), workspace_id=workspace.id,
        service_id=service.id, source_id=source.id, provider=SignalProvider.GENERIC_WEBHOOK,
        ingress_key="hash:" + "c" * 64 + ":1", payload={"type": "mystery", "password": "hunter2"},
        payload_hash="c" * 64, environment="production", received_at=datetime.now(UTC),
    )  # fmt: skip
    unsupported = json.loads(valid.model_dump_json()) | {
        "schema_version": "42",
        "payload": {"token": "tok-secret"},
    }
    cross_tenant = json.loads(valid.model_dump_json()) | {
        "signal_id": str(uuid.uuid4()),
        "source_id": str(foreign.id),
        "payload": {"type": "alert"},
    }
    for value in (
        b"\xff\xfe not json at all",
        json.dumps(unsupported).encode(),
        valid.model_dump_json().encode(),
        json.dumps(cross_tenant).encode(),
        b'{"schema_version": "1"}',
    ):
        publish_raw(api.bus, api.settings, value)
    run_pipeline(api.settings, api.bus)
    session.expire_all()
    letters = list(session.scalars(select(DeadLetterSignal).order_by(DeadLetterSignal.offset)))
    mine = [
        row
        for row in letters
        if row.workspace_id in (workspace.id, None)
        and row.original_topic.endswith("raw.v1")
        and row.created_at > datetime.now(UTC) - timedelta(minutes=2)
    ]
    codes = sorted(row.failure_code for row in mine if row.payload_hash)
    for expected in (
        "unsupported_schema_version",
        "unsupported_signal_type",
        "invalid_signal",
        "invalid_tenant_relationship",
    ):
        assert expected in codes, (expected, codes)
    blob = json.dumps(
        [row.sanitized_payload for row in letters if row.sanitized_payload], default=str
    )
    assert "hunter2" not in blob and "tok-secret" not in blob
    dlq = api.bus.messages(Topics.from_settings(api.settings).dead_letter)
    assert len(dlq) >= 5 and all(
        b"hunter2" not in m.value and b"tok-secret" not in m.value for m in dlq
    )
    poisoned = next(row for row in mine if row.failure_code == "unsupported_signal_type")
    assert poisoned.workspace_id == workspace.id and poisoned.source_id == source.id
    unparseable = next(
        row
        for row in letters
        if row.failure_code == "invalid_signal" and row.sanitized_payload is None
    )
    assert unparseable.workspace_id is None  # untrusted tenant claims are never believed
    receipt = next(r for r in receipts(session, workspace.id) if r.signal_id == poisoned.signal_id)
    assert receipt.status == "dead_lettered"
    refreshed = session.get(ObservabilitySource, source.id)
    assert refreshed is not None and refreshed.status == "failing" and refreshed.last_failure_at
    assert incident_events(session, workspace.id) == (0, 0)  # nothing silently correlated
    many = {"schema_version": "9"} | {f"k{i}": "x" * 1900 for i in range(60)}
    big = json.dumps(many).encode()
    publish_raw(api.bus, api.settings, big)
    run_pipeline(api.settings, api.bus)
    oversized = session.scalar(
        select(DeadLetterSignal)
        .where(DeadLetterSignal.failure_code == "unsupported_schema_version")
        .order_by(DeadLetterSignal.created_at.desc())
    )
    assert oversized is not None and oversized.sanitized_payload is None  # too large to retain


def test_dead_letter_replay_is_audited_idempotent_and_workspace_scoped(
    api: Api, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    api.post(source, secret, alert_payload("evt-replay"))
    # Simulate a transient downstream failure that exhausts the retries (database "down").
    real = ingest_event

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("insert", {}, Exception("database down"))

    monkeypatch.setattr(pipeline_module, "ingest_event", down)
    run_pipeline(api.settings, api.bus)
    [letter] = list(
        session.scalars(
            select(DeadLetterSignal).where(DeadLetterSignal.workspace_id == workspace.id)
        )
    )
    assert letter.failure_code == "retries_exhausted" and letter.resolved_at is None
    assert incident_events(session, workspace.id) == (0, 0)
    base = f"/api/v1/workspaces/{workspace.id}/observability"
    listed = api.client.get(f"{base}/dead-letters").json()["items"]
    assert listed[0]["failure_class"] == "retryable_exhausted" and listed[0]["replayable"]
    assert "hunter2" not in json.dumps(listed)
    other_ws, _ = make_workspace(session)
    assert (
        api.client.post(
            f"/api/v1/workspaces/{other_ws.id}/observability/dead-letters/{letter.id}/replay"
        ).status_code
        == 404
    )

    monkeypatch.setattr(pipeline_module, "ingest_event", real)  # the dependency recovered
    before = len(api.bus.messages(letter.original_topic))
    replay = api.client.post(f"{base}/dead-letters/{letter.id}/replay")
    assert replay.status_code == 200 and replay.json()["status"] == "replayed"
    assert len(api.bus.messages(letter.original_topic)) == before + 1
    assert api.bus.messages(letter.original_topic)[-1].headers["replay_of"] == str(letter.id)
    again = api.client.post(f"{base}/dead-letters/{letter.id}/replay").json()
    assert again["status"] == "already_replayed" and again["replay_count"] == 1
    assert len(api.bus.messages(letter.original_topic)) == before + 1  # no second publication
    run_pipeline(api.settings, api.bus)
    session.expire_all()
    assert incident_events(session, workspace.id) == (1, 1)
    refreshed = session.get(DeadLetterSignal, letter.id)
    assert (
        refreshed is not None
        and refreshed.resolved_at is not None
        and refreshed.replayed_at is not None
    )
    assert {r.status for r in receipts(session, workspace.id)} == {"processed"}
    audit = session.scalars(
        select(DeadLetterReplay.outcome)
        .where(DeadLetterReplay.dead_letter_id == letter.id)
        .order_by(DeadLetterReplay.created_at)
    ).all()
    assert list(audit) == ["replayed", "already_replayed"]
    run_pipeline(api.settings, api.bus)
    assert incident_events(session, workspace.id) == (1, 1)  # replay is idempotent end to end


def test_unreplayable_dead_letters_are_refused(api: Api, session: Session) -> None:
    make_workspace(session)
    many = {f"k{i}": "x" * 1900 for i in range(60)} | {"schema_version": "9"}
    big = json.dumps(many).encode()
    message = BusMessage(
        topic=Topics.from_settings(api.settings).raw, value=big, key="k", partition=0, offset=7
    )

    async def go(ctx: PipelineContext) -> None:
        await dead_letter(ctx, message, "unsupported_schema_version", "too big", 1)

    run_async(go, api.settings, api.bus)
    letter = session.scalar(
        select(DeadLetterSignal).where(
            DeadLetterSignal.original_topic == message.topic,
            DeadLetterSignal.offset == 7,
            DeadLetterSignal.workspace_id.is_(None),
        )
    )
    assert letter is not None and letter.sanitized_payload is None
    run_async(go, api.settings, api.bus)  # the same origin is upserted, not duplicated
    assert (
        session.scalar(
            select(func.count(DeadLetterSignal.id)).where(
                DeadLetterSignal.offset == 7, DeadLetterSignal.original_topic == message.topic
            )
        )
        == 1
    )


# -- API surface --------------------------------------------------------------------------------
def test_signal_explorer_pipeline_status_and_demo_generation(api: Api, session: Session) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    demo = api.client.post(
        f"/api/v1/workspaces/{workspace.id}/observability/sources",
        json={
            "name": "demo",
            "provider": "demo_fixture",
            "service_id": str(service.id),
            "environment": "production",
        },
    ).json()
    assert secret
    base = f"/api/v1/workspaces/{workspace.id}/observability"
    for index in range(5):
        api.post(source, secret, alert_payload(f"evt-p{index}", title=f"Problem {index}"))
    generated = api.client.post(
        f"{base}/demo/generate",
        json={"source_id": demo["id"], "count": 20, "duplicate_percent": 20, "invalid_percent": 10},
    )
    assert generated.status_code == 200
    totals = generated.json()
    assert (
        totals["accepted"] + totals["duplicates"] == 20
        and totals["duplicates"] > 0
        and totals["rejected"] == 0
    )
    run_pipeline(api.settings, api.bus, rounds=4)
    page = api.client.get(f"{base}/signals?limit=10").json()
    assert len(page["items"]) == 10 and page["next_cursor"]
    seen = {item["signal_id"] for item in page["items"]}
    second = api.client.get(f"{base}/signals?limit=10&cursor={page['next_cursor']}").json()
    assert second["items"] and not seen & {item["signal_id"] for item in second["items"]}
    everything: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        chunk = api.client.get(
            f"{base}/signals?limit=7" + (f"&cursor={cursor}" if cursor else "")
        ).json()
        everything += chunk["items"]
        cursor = chunk["next_cursor"]
        if not cursor:
            break
    assert len(everything) == len({i["signal_id"] for i in everything}) == 5 + totals["accepted"]
    stamps = [i["received_at"] for i in everything]
    assert stamps == sorted(stamps, reverse=True)
    dead = api.client.get(f"{base}/signals?status=dead_lettered").json()["items"]
    assert dead and all(item["status"] == "dead_lettered" for item in dead)
    by_source = api.client.get(f"{base}/signals?source_id={source.id}").json()["items"]
    assert len(by_source) == 5 and all(item["source_id"] == str(source.id) for item in by_source)
    assert api.client.get(f"{base}/signals?severity=sev2").json()["items"]
    detail = api.client.get(f"{base}/signals/{by_source[0]['signal_id']}").json()
    assert detail["normalized"]["schema_version"] == "1" and detail["payload_hash"]
    assert api.client.get(f"{base}/signals/{uuid.uuid4()}").status_code == 404
    assert (
        api.client.get(
            f"/api/v1/workspaces/{uuid.uuid4()}/observability/signals/{by_source[0]['signal_id']}"
        ).status_code
        == 404
    )
    assert api.client.get(f"{base}/signals?cursor=garbage").status_code == 422
    pipeline = api.client.get(f"{base}/pipeline").json()
    assert pipeline["accepted"] >= 5 and pipeline["processed"] >= 5 and pipeline["broker_healthy"]
    assert (
        pipeline["broker_backend"] == "memory"
        and pipeline["lag_available"]
        and pipeline["consumer_lag"] == 0
    )
    assert pipeline["latency_p50_ms"] is not None and pipeline["incidents_created"] >= 1
    assert pipeline["dead_letters_open"] >= 1 and {s["name"] for s in pipeline["sources"]} >= {
        "demo"
    }
    production = create_app(
        obs_settings(
            APP_ENV=Environment.PRODUCTION,
            AUTH_PROVIDER="oidc",
            AUTH_OIDC_ISSUER="https://idp.test.example",
            AUTH_OIDC_AUDIENCE="client",
        )
    )
    # Authenticate with the deterministic test identity while the app runs in production mode.
    production.state.identity_provider = LocalProvider(obs_settings())
    with TestClient(production) as prod:
        denied = prod.post(f"{base}/demo/generate", json={"source_id": demo["id"], "count": 1})
        assert denied.status_code == 404 and denied.json()["error"]["code"] == "demo_disabled"


def test_metrics_endpoint_exposes_counters_without_payloads(api: Api, session: Session) -> None:
    workspace, service = make_workspace(session)
    source, secret = make_source(session, api.settings, workspace, service)
    assert secret
    api.post(source, secret, alert_payload("evt-m", title="needle-title-should-not-appear"))
    run_pipeline(api.settings, api.bus)
    text = api.client.get("/api/v1/metrics").text
    for name in ("opspilot_signals_accepted_total", "opspilot_signals_processed_total",
                 "opspilot_signal_end_to_end_seconds", "opspilot_incidents_created_total",
                 "opspilot_consumer_lag_messages"):  # fmt: skip
        assert name in text
    assert "needle-title-should-not-appear" not in text and secret not in text


# -- LangGraph tools ----------------------------------------------------------------------------
def tool_context(
    settings: Settings, workspace: Any, incident: Incident, investigation_id: UUID | None
) -> ToolContext:
    return ToolContext(workspace_id=workspace.id, incident_id=incident.id,
                       investigation_id=investigation_id or uuid.uuid4(), settings=settings)  # fmt: skip


def make_investigation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, workspace_id: UUID, incident_id: UUID
) -> UUID:
    monkeypatch.setattr("opspilot_api.investigations.router.enqueue_investigation", lambda *a: None)
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/incidents/{incident_id}/investigations",
        json={"trigger": "manual"},
    )
    assert created.status_code == 202, created.text
    return UUID(created.json()["investigation_id"])


OBSERVED = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def test_observability_tools_are_bounded_redacted_cited_and_audited(
    api: Api, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, service = make_workspace(session)
    make_source(session, api.settings, workspace, service, SourceProvider.DEMO_FIXTURE,
                {"max_window_minutes": 30, "max_results": 5, "spike_after": (OBSERVED - timedelta(minutes=8)).isoformat()},
                name="fixture-logs")  # fmt: skip
    incident = new_incident(session, workspace, service, "Checkout errors", OBSERVED)
    investigation = make_investigation(api.client, monkeypatch, workspace.id, incident.id)
    context = tool_context(api.settings, workspace, incident, investigation)
    assert {
        "query_service_logs",
        "get_service_metrics",
        "compare_metric_windows",
        "get_alarm_context",
    } <= set(TOOL_REGISTRY)

    logs = execute_tool(
        "query_service_logs", session, context, {"search_terms": ["payment"], "limit": 50}, 65536
    )
    assert logs.evidence and logs.evidence[0].source_type == "observability_log"
    assert "sk-fixture" not in logs.evidence[0].excerpt and "[REDACTED]" in logs.evidence[0].excerpt
    assert logs.evidence[0].metadata["untrusted"] and logs.evidence[0].title.startswith(
        "[observability: fixture-logs logs"
    )
    assert logs.data["count"] <= 5  # source max_results bounds the model-supplied limit of 50
    wide = execute_tool("query_service_logs", session, context,
                        {"start": (OBSERVED - timedelta(days=30)).isoformat(), "end": OBSERVED.isoformat()}, 65536)  # fmt: skip
    assert wide.data["window_clamped"] is True
    # Models cannot pick infrastructure: extra arguments are ignored, never forwarded.
    ignored = execute_tool("query_service_logs", session, context,
                           {"log_group": "/other/account/group", "role_arn": "arn:aws:iam::1:role/x", "region": "eu-west-1"}, 65536)  # fmt: skip
    assert ignored.data["evidence_count"] >= 0 and not ignored.data.get("unavailable")
    unsafe = execute_tool(
        "query_service_logs", session, context, {"search_terms": ["x'; DROP TABLE"]}, 65536
    )
    assert unsafe.data["unavailable"] and unsafe.evidence == []
    metrics = execute_tool("get_service_metrics", session, context, {"period": 300}, 65536)
    assert (
        metrics.evidence[0].source_type == "observability_metric"
        and metrics.data["average"] is not None
    )
    compare = execute_tool("compare_metric_windows", session, context, {}, 65536)
    assert compare.evidence[0].source_type == "observability_metric"
    assert compare.data["change_percent"] and compare.data["change_percent"] > 50
    alarm = execute_tool("get_alarm_context", session, context, {}, 65536)
    assert alarm.evidence[0].source_type == "observability_alarm"
    audits = session.scalars(
        select(CloudWatchQueryAudit).where(CloudWatchQueryAudit.workspace_id == workspace.id)
    ).all()
    assert {a.query_type for a in audits} >= {"logs", "metrics", "metric_compare", "alarm"}
    assert all(a.investigation_id == investigation and a.status in {"ok", "failed"} for a in audits)
    assert "sk-fixture" not in json.dumps([a.__dict__.get("failure_code") for a in audits])


def test_provider_failures_become_unavailable_observations_and_scope_is_workspace_only(
    api: Api, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, service = make_workspace(session)
    other_ws, other_service = make_workspace(session)
    source, _ = make_source(
        session,
        api.settings,
        workspace,
        service,
        SourceProvider.DEMO_FIXTURE,
        {"fixture_mode": "throttle"},
        name="flaky",
    )
    make_source(
        session,
        api.settings,
        other_ws,
        other_service,
        SourceProvider.DEMO_FIXTURE,
        {},
        name="foreign",
    )
    incident = new_incident(session, workspace, service, "Errors", OBSERVED)
    investigation = make_investigation(api.client, monkeypatch, workspace.id, incident.id)
    context = tool_context(api.settings, workspace, incident, investigation)
    for mode, reason, retryable in (
        ("throttle", "provider_throttled", True),
        ("auth_failure", "provider_authentication_failed", False),
        ("timeout", "provider_unavailable", True),
    ):
        source.configuration = {"fixture_mode": mode}
        session.commit()
        result = execute_tool("query_service_logs", session, context, {}, 65536)
        assert (
            result.evidence == [] and result.data["unavailable"] and result.data["reason"] == reason
        )
        assert result.data["retryable"] is retryable and "continues" in result.summary
    failed = session.scalars(
        select(CloudWatchQueryAudit.failure_code).where(
            CloudWatchQueryAudit.workspace_id == workspace.id
        )
    ).all()
    assert set(failed) == {
        "provider_throttled",
        "provider_authentication_failed",
        "provider_unavailable",
    }
    # A service without any source (and sources that belong to other workspaces) yields no evidence.
    lonely = new_incident(session, other_ws, None, "No service", OBSERVED)
    result = execute_tool(
        "get_alarm_context", session, tool_context(api.settings, other_ws, lonely, None), {}, 65536
    )
    assert result.evidence == [] and result.data["has_observability_source"] is False
    foreign_incident = new_incident(session, workspace, service, "Cross", OBSERVED)
    spoofed = ToolContext(
        workspace_id=other_ws.id,
        incident_id=foreign_incident.id,
        investigation_id=uuid.uuid4(),
        settings=api.settings,
    )
    refused = execute_tool("query_service_logs", session, spoofed, {}, 65536)
    assert refused.evidence == [] and refused.data["unavailable"]


def test_investigation_collects_cited_observability_evidence_and_works_without_a_source(
    api: Api, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, service = make_workspace(session)
    make_source(
        session, api.settings, workspace, service, SourceProvider.DEMO_FIXTURE, {}, name="fixture"
    )
    incident = new_incident(session, workspace, service, "Database connection exhaustion", OBSERVED,
                            events=[("db_error", "connection pool exhausted")])  # fmt: skip
    investigation_id = make_investigation(api.client, monkeypatch, workspace.id, incident.id)
    run_workflow(api.settings, workspace.id, investigation_id)
    detail = api.client.get(
        f"/api/v1/workspaces/{workspace.id}/investigations/{investigation_id}"
    ).json()
    assert detail["status"] == "awaiting_review"
    kinds = {item["source_type"] for item in detail["evidence"]}
    assert {"observability_log", "observability_metric", "observability_alarm"} <= kinds
    observed = {i["id"] for i in detail["evidence"] if i["source_type"].startswith("observability")}
    assert set(detail["report"]["evidence_citations"]) & observed
    assert all(
        i["metadata"].get("citation_label")
        for i in detail["evidence"]
        if i["source_type"].startswith("observability")
    )
    assert all("sk-fixture" not in i["excerpt"] for i in detail["evidence"])
    snapshot = api.client.get(
        f"/api/v1/workspaces/{workspace.id}/incidents/{incident.id}/observability"
    ).json()
    assert snapshot["has_source"] and snapshot["logs"] and snapshot["metric"] and snapshot["alarm"]
    assert "correlation" in snapshot["notice"] and snapshot["unavailable"] is None
    assert all("sk-fixture" not in line["message"] for line in snapshot["logs"])

    bare_ws, bare_service = make_workspace(session)
    bare = new_incident(session, bare_ws, bare_service, "No source", OBSERVED, events=[("x", "y")])
    bare_id = make_investigation(api.client, monkeypatch, bare_ws.id, bare.id)
    run_workflow(api.settings, bare_ws.id, bare_id)
    plain = api.client.get(f"/api/v1/workspaces/{bare_ws.id}/investigations/{bare_id}").json()
    assert plain["status"] == "awaiting_review"
    assert not [i for i in plain["evidence"] if i["source_type"].startswith("observability")]
    empty = api.client.get(
        f"/api/v1/workspaces/{bare_ws.id}/incidents/{bare.id}/observability"
    ).json()
    assert empty["has_source"] is False and empty["logs"] == []
    assert (
        api.client.get(
            f"/api/v1/workspaces/{workspace.id}/incidents/{bare.id}/observability"
        ).status_code
        == 404
    )
    fail_ws, fail_service = make_workspace(session)
    make_source(
        session,
        api.settings,
        fail_ws,
        fail_service,
        SourceProvider.DEMO_FIXTURE,
        {"fixture_mode": "auth_failure"},
        name="broken",
    )
    failing = new_incident(session, fail_ws, fail_service, "Broken", OBSERVED)
    view = api.client.get(
        f"/api/v1/workspaces/{fail_ws.id}/incidents/{failing.id}/observability"
    ).json()
    assert (
        view["has_source"]
        and view["unavailable"] == "provider_authentication_failed"
        and view["logs"] == []
    )


def test_chatty_incidents_do_not_starve_observability_evidence_or_fail_the_investigation(
    api: Api, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline-driven incidents correlate hundreds of events; the evidence budget must hold."""
    workspace, service = make_workspace(session)
    make_source(
        session, api.settings, workspace, service, SourceProvider.DEMO_FIXTURE, {}, name="fx"
    )
    incident = new_incident(
        session, workspace, service, "Checkout 5xx storm", OBSERVED,
        events=[(f"alert_{i % 7}", f"sig-{i}") for i in range(90)],
    )  # fmt: skip
    investigation_id = make_investigation(api.client, monkeypatch, workspace.id, incident.id)
    run_workflow(api.settings, workspace.id, investigation_id)
    detail = api.client.get(
        f"/api/v1/workspaces/{workspace.id}/investigations/{investigation_id}"
    ).json()
    assert detail["status"] == "awaiting_review", detail.get("failure_code")
    assert len(detail["evidence"]) <= api.settings.AI_MAX_EVIDENCE_ITEMS
    kinds = {item["source_type"] for item in detail["evidence"]}
    assert {"observability_log", "observability_metric", "observability_alarm"} <= kinds
