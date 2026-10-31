"""Idempotent, append-only ingestion of usage events into PostgreSQL."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.llmops.models import UsageEventRow
from opspilot_api.llmops.usage import UsageEvent


def _uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


async def record_usage(session: AsyncSession, event: UsageEvent) -> bool:
    """Insert once per ``idempotency_key``. Returns False for a replay (the row is untouched)."""
    stmt = (
        pg_insert(UsageEventRow)
        .values(
            idempotency_key=event.idempotency_key,
            workspace_id=UUID(event.workspace_id),
            user_id=_uuid(event.user_id),
            api_key_id=_uuid(event.api_key_id),
            incident_id=_uuid(event.incident_id),
            investigation_id=_uuid(event.investigation_id),
            remediation_id=_uuid(event.remediation_id),
            workflow=event.workflow,
            node_name=event.node_name,
            provider=event.provider,
            model=event.model,
            pricing_rate_version=event.pricing_rate_version,
            input_tokens=event.input_tokens,
            cached_input_tokens=event.cached_input_tokens,
            output_tokens=event.output_tokens,
            request_count=event.request_count,
            latency_ms=event.latency_ms,
            estimated_cost_usd=event.estimated_cost_usd,
            status=event.status,
            routing_decision_id=_uuid(event.routing_decision_id),
            trace_id=event.trace_id,
            occurred_at=event.occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(UsageEventRow.id)
    )
    inserted = (await session.execute(stmt)).first() is not None
    await session.commit()
    return inserted
