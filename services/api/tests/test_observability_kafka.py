"""Real Kafka-compatible broker tests (Redpanda locally and in CI).

Needs the compose ``broker`` (or any broker at ``KAFKA_BOOTSTRAP_SERVERS``). Set
``REQUIRE_KAFKA=1`` (CI does) so a missing broker fails instead of skipping.
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Incident
from opspilot_api.main import create_app
from opspilot_api.observability.backpressure import NORMALIZED_GROUP, RAW_GROUP
from opspilot_api.observability.bus import KafkaEventBus, Topics
from opspilot_api.observability.consumer import drain
from opspilot_api.observability.models import DeadLetterSignal, SignalReceipt

from .obs_helpers import (
    alert_payload,
    body_of,
    make_source,
    make_workspace,
    obs_settings,
    run_async,
    signed_headers,
)

pytestmark = pytest.mark.integration


def kafka_settings() -> Settings:
    return obs_settings(EVENT_BUS_BACKEND="kafka")


@pytest.fixture
def settings() -> Settings:
    return kafka_settings()


@pytest.fixture
def bus(settings: Settings) -> Iterator[KafkaEventBus]:
    candidate = KafkaEventBus(settings)
    if not candidate.health():
        candidate.close()
        if os.environ.get("REQUIRE_KAFKA"):
            pytest.fail("a Kafka-compatible broker is required but unreachable")
        pytest.skip("no Kafka-compatible broker reachable (start `docker compose up -d broker`)")
    candidate.ensure_topics(Topics.from_settings(settings).all(), settings.KAFKA_PARTITIONS)
    yield candidate
    candidate.close()


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(obs_settings().database_url)
    with Session(engine) as db:
        yield db
    engine.dispose()


def eventually(check: Callable[[], Any], timeout: float = 60.0, step: float = 0.5) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met in time")


def consume_one(bus: KafkaEventBus, topic: str, group: str) -> Any:
    return eventually(lambda: bus.consume([topic], group, 1, 1.0) or None)[0]


def test_publish_consume_commit_headers_and_lag_on_a_real_broker(
    bus: KafkaEventBus, settings: Settings
) -> None:
    topic = Topics.from_settings(settings).raw
    group = f"kafka-it-{uuid.uuid4().hex[:8]}"
    key = f"{uuid.uuid4()}:{uuid.uuid4()}"
    first = bus.publish(topic, key, b'{"n":1}', {"signal_id": "s1", "authorization": "Bearer nope"})
    second = bus.publish(topic, key, b'{"n":2}')
    assert first.offset is not None and second.offset is not None
    assert first.partition == second.partition and second.offset == first.offset + 1
    assert first.headers == {"signal_id": "s1"}
    got = consume_one(bus, topic, group)
    assert got.headers == {"signal_id": "s1"} and "authorization" not in got.headers
    assert got.key == key and got.value == b'{"n":1}'
    time.sleep(2.2)  # lag is cached for two seconds
    assert (bus.lag(group, [topic]) or 0) >= 1
    bus.commit(group, got)
    assert bus.health()


def test_uncommitted_messages_are_redelivered_after_a_consumer_restart(
    bus: KafkaEventBus, settings: Settings
) -> None:
    topic = Topics.from_settings(settings).raw
    group = f"kafka-restart-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex.encode()
    bus.publish(topic, "restart-key", marker)
    crashing = KafkaEventBus(settings)
    seen = eventually(
        lambda: [m for m in crashing.consume([topic], group, 50, 1.0) if m.value == marker] or None
    )
    assert seen  # delivered, but the consumer "crashes" before committing
    crashing.close()
    survivor = KafkaEventBus(settings)
    try:
        again = eventually(
            lambda: (
                [m for m in survivor.consume([topic], group, 50, 1.0) if m.value == marker] or None
            )
        )
        assert again[0].offset == seen[0].offset and again[0].partition == seen[0].partition
        survivor.commit(group, again[0])
    finally:
        survivor.close()
    third = KafkaEventBus(settings)
    try:
        assert not [m for m in third.consume([topic], group, 50, 3.0) if m.value == marker]
    finally:
        third.close()


def test_webhook_to_incident_through_kafka_survives_a_consumer_crash_and_dead_letters_poison(
    bus: KafkaEventBus, settings: Settings, session: Session
) -> None:
    topics = Topics.from_settings(settings)
    workspace, service = make_workspace(session)
    source, secret = make_source(session, settings, workspace, service)
    assert secret
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.event_bus = bus
        for index in range(3):
            payload = alert_payload(f"kafka-{index}")
            body = body_of(payload)
            response = client.post(
                f"/api/v1/ingest/webhooks/{source.public_id}", content=body,
                headers=signed_headers(secret, body),
            )  # fmt: skip
            assert response.status_code == 202, response.text

    # The raw consumer receives the first message but crashes before committing anything.
    doomed = KafkaEventBus(settings)
    eventually(lambda: doomed.consume([topics.raw], RAW_GROUP, 1, 1.0) or None)
    doomed.close()

    bus.publish(topics.raw, "poison", b"\xff not an envelope")

    def processed() -> int:
        async def go(ctx: Any) -> None:
            await drain(ctx, "raw", 1.0)
            await drain(ctx, "normalized", 1.0)

        run_async(go, settings, bus)
        session.expire_all()
        return (
            session.scalar(
                select(func.count(SignalReceipt.id)).where(
                    SignalReceipt.workspace_id == workspace.id, SignalReceipt.status == "processed"
                )
            )
            or 0
        )

    eventually(lambda: processed() == 3, timeout=90)
    session.expire_all()
    assert (
        session.scalar(select(func.count(Incident.id)).where(Incident.workspace_id == workspace.id))
        == 1
    )
    eventually(lambda: bus.lag(RAW_GROUP, [topics.raw]) == 0 or None, timeout=30)
    # Redelivery after the crash did not duplicate incidents or receipts.
    assert (
        session.scalar(
            select(func.count(SignalReceipt.id)).where(SignalReceipt.workspace_id == workspace.id)
        )
        == 3
    )
    letter = eventually(
        lambda: session.scalar(
            select(DeadLetterSignal).where(
                DeadLetterSignal.original_topic == topics.raw,
                DeadLetterSignal.failure_code == "invalid_signal",
            )
        )
    )
    assert letter.workspace_id is None
    dead = bus.consume([topics.dead_letter], f"dlq-reader-{uuid.uuid4().hex[:6]}", 10, 3.0)
    assert dead or eventually(
        lambda: (
            bus.consume([topics.dead_letter], f"dlq-reader2-{uuid.uuid4().hex[:6]}", 10, 3.0)
            or None
        )
    )
    assert NORMALIZED_GROUP != RAW_GROUP
