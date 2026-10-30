"""Deterministic end-to-end pipeline smoke test: ``python -m opspilot_api.observability.smoke``.

Uses the in-memory bus (no broker) with real PostgreSQL and Redis: signed webhook -> raw consumer
-> normalized consumer -> incident, plus duplicate suppression, poison dead-lettering, and replay.
Exits non-zero on the first violated expectation. Development/CI only.
"""

import asyncio
import json
import secrets
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings, get_settings
from opspilot_api.incidents.models import Incident, MonitoredService, Workspace
from opspilot_api.infrastructure import create_db_engine, create_session_factory
from opspilot_api.main import create_app
from opspilot_api.observability.bus import InMemoryEventBus, Topics
from opspilot_api.observability.consumer import drain
from opspilot_api.observability.models import DeadLetterSignal, SignalReceipt
from opspilot_api.observability.pipeline import PipelineContext
from opspilot_api.observability.secrets_util import compute_signature

if sys.platform == "win32":  # psycopg's async driver cannot use the default Proactor loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def check(label: str, ok: bool) -> None:
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        sys.exit(1)


def run_consumers(settings: Settings, bus: InMemoryEventBus) -> None:
    async def go() -> None:
        engine = create_db_engine(settings)
        try:
            ctx = PipelineContext(settings, bus, create_session_factory(engine))
            for _ in range(3):
                await drain(ctx, "raw", 0.0)
                await drain(ctx, "normalized", 0.0)
        finally:
            await engine.dispose()

    asyncio.run(go())


def main() -> None:
    settings = get_settings().model_copy(
        update={
            "EVENT_BUS_BACKEND": "memory",
            "KAFKA_TOPIC_PREFIX": f"smoke-{uuid.uuid4().hex[:8]}",
            "OBS_RETRY_BASE_DELAY_SECONDS": 0.0,
            "OBS_WORKSPACE_RATE_PER_SECOND": 10_000,
            "OBS_SOURCE_RATE_PER_SECOND": 10_000,
        }
    )
    if settings.OBS_SECRET_ENCRYPTION_KEY is None:
        raise SystemExit("OBS_SECRET_ENCRYPTION_KEY must be set to run the smoke test")
    bus = InMemoryEventBus(settings.KAFKA_PARTITIONS)
    bus.ensure_topics(Topics.from_settings(settings).all(), settings.KAFKA_PARTITIONS)
    engine = create_engine(settings.database_url)
    suffix = secrets.token_hex(4)
    with Session(engine) as session:
        workspace = Workspace(name=f"smoke {suffix}", slug=f"smoke-{suffix}")
        session.add(workspace)
        session.flush()
        service = MonitoredService(
            workspace_id=workspace.id,
            name="Smoke",
            slug=f"smoke-{suffix}",
            environment="production",
        )
        session.add(service)
        session.commit()
        app = create_app(settings)
        with TestClient(app) as client:
            app.state.event_bus = bus
            base = f"/api/v1/workspaces/{workspace.id}/observability"
            created = client.post(
                f"{base}/sources",
                json={"name": "smoke", "provider": "generic_webhook",
                      "service_id": str(service.id), "environment": "production"},
            ).json()  # fmt: skip
            secret, public_id = created["webhook_secret"], created["public_id"]

            def send(
                payload: dict[str, Any], timestamp: int | None = None, sign: str | None = None
            ) -> Any:
                body = json.dumps(payload).encode()
                stamp = str(timestamp or int(datetime.now(UTC).timestamp()))
                signature = compute_signature(sign or secret, stamp, body)
                return client.post(
                    f"/api/v1/ingest/webhooks/{public_id}",
                    content=body,
                    headers={"X-OpsPilot-Timestamp": stamp, "X-OpsPilot-Signature": signature},
                )

            alert: dict[str, Any] = {
                "type": "alert",
                "event_id": "smoke-1",
                "title": "Smoke alert",
                "severity": "high",
                "occurred_at": datetime.now(UTC).isoformat(),
                "error_signature": "smoke",
            }
            check("signed webhook accepted (202)", send(alert).status_code == 202)
            check("duplicate delivery is idempotent (200)", send(alert).status_code == 200)
            check(
                "bad signature rejected (401)", send(alert, sign="whsec_wrong").status_code == 401
            )
            check("expired timestamp rejected (401)", send(alert, timestamp=1).status_code == 401)
            check(
                "poison payload accepted for async DLQ",
                send({"type": "mystery", "event_id": "p"}).status_code == 202,
            )
            run_consumers(settings, bus)
            session.expire_all()
            incidents = session.scalar(
                select(func.count(Incident.id)).where(Incident.workspace_id == workspace.id)
            )
            check("exactly one incident created", incidents == 1)
            statuses = sorted(
                session.scalars(
                    select(SignalReceipt.status).where(SignalReceipt.workspace_id == workspace.id)
                )
            )
            check(
                "receipts: one processed, one dead-lettered",
                statuses == ["dead_lettered", "processed"],
            )
            letter = session.scalar(
                select(DeadLetterSignal).where(DeadLetterSignal.workspace_id == workspace.id)
            )
            check(
                "poison message dead-lettered",
                letter is not None and letter.failure_code == "unsupported_signal_type",
            )
            if letter is None:
                raise SystemExit(1)
            replay = client.post(f"{base}/dead-letters/{letter.id}/replay").json()
            again = client.post(f"{base}/dead-letters/{letter.id}/replay").json()
            check(
                "replay is idempotent",
                (replay["status"], again["status"]) == ("replayed", "already_replayed"),
            )
            run_consumers(settings, bus)
            session.expire_all()
            check(
                "replay did not duplicate incidents",
                session.scalar(
                    select(func.count(Incident.id)).where(Incident.workspace_id == workspace.id)
                )
                == 1,
            )
    engine.dispose()
    print("pipeline smoke test passed")


if __name__ == "__main__":
    main()
