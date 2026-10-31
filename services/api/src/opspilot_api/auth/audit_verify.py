"""Audit-log integrity verification: ``python -m opspilot_api.auth.audit_verify``.

Recomputes each event's HMAC with ``AUTH_AUDIT_KEY`` and reports any event whose stored
``integrity_hash`` does not match (i.e. content altered outside the application). Exit status is 1
when a mismatch is found. Use ``--workspace <uuid>`` to limit the scan.
"""

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.auth.audit import verify_event
from opspilot_api.auth.models import AuditEvent
from opspilot_api.config import Settings, get_settings
from opspilot_api.infrastructure import create_db_engine, create_session_factory


async def verify_events(
    session: AsyncSession, settings: Settings, workspace_id: UUID | None = None
) -> tuple[int, list[UUID]]:
    """Return ``(events_checked, ids_with_mismatched_hashes)``; streams in bounded batches."""
    query = select(AuditEvent).order_by(AuditEvent.occurred_at, AuditEvent.id)
    if workspace_id is not None:
        query = query.where(AuditEvent.workspace_id == workspace_id)
    checked = 0
    bad: list[UUID] = []
    result = await session.stream_scalars(query.execution_options(yield_per=500))
    async for event in result:
        checked += 1
        if not verify_event(settings, event):
            bad.append(event.id)
    return checked, bad


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=UUID, default=None)
    args = parser.parse_args()
    settings = get_settings()
    engine = create_db_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            checked, bad = await verify_events(session, settings, args.workspace)
    finally:
        await engine.dispose()
    print(f"checked {checked} audit events")
    for event_id in bad:
        print(f"INTEGRITY MISMATCH: {event_id}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
