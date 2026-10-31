"""Data retention for the observability pipeline: ``python -m opspilot_api.maintenance.retention``.

Conservative by design:

* Disabled unless a retention period is configured (``RETENTION_*_DAYS``; 0 means keep forever).
* A dry run is the default; nothing is deleted without ``--apply``.
* Only data that is finished and reproducible is purged: signal receipts that were processed or
  recognised as duplicates, and dead letters that were replayed or resolved. Receipts that failed or
  are dead-lettered, incidents, investigations, and the audit log are never touched. (The audit
  log is append-only: database triggers reject DELETE, so it is never purged by this job.)
* Deletion runs in small batches so locks and transactions stay short.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, create_engine, delete, func, or_, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings, get_settings
from opspilot_api.observability.models import (
    DeadLetterReplay,
    DeadLetterSignal,
    ReceiptStatus,
    SignalReceipt,
)

logger = logging.getLogger(__name__)

PURGEABLE_RECEIPT_STATUSES = (ReceiptStatus.PROCESSED.value, ReceiptStatus.DUPLICATE.value)


@dataclass(frozen=True)
class RuleResult:
    name: str
    retention_days: int
    cutoff: datetime | None
    eligible: int
    deleted: int


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


def _receipt_candidates(cutoff: datetime) -> Select[UUID]:
    return select(SignalReceipt.id).where(
        SignalReceipt.status.in_(PURGEABLE_RECEIPT_STATUSES), SignalReceipt.received_at < cutoff
    )


def _dead_letter_candidates(cutoff: datetime) -> Select[UUID]:
    return select(DeadLetterSignal.id).where(
        or_(DeadLetterSignal.replayed_at.is_not(None), DeadLetterSignal.resolved_at.is_not(None)),
        DeadLetterSignal.created_at < cutoff,
    )


def _count(session: Session, candidates: Select[UUID]) -> int:
    return int(session.scalar(select(func.count()).select_from(candidates.subquery())) or 0)


def purge_receipts(session: Session, cutoff: datetime, batch_size: int) -> int:
    ids = _receipt_candidates(cutoff).limit(batch_size)
    result = session.execute(delete(SignalReceipt).where(SignalReceipt.id.in_(ids)))
    session.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def purge_dead_letters(session: Session, cutoff: datetime, batch_size: int) -> int:
    ids = list(session.scalars(_dead_letter_candidates(cutoff).limit(batch_size)))
    if not ids:
        return 0
    # Replay records reference their dead letter (ON DELETE RESTRICT), so they go first.
    session.execute(delete(DeadLetterReplay).where(DeadLetterReplay.dead_letter_id.in_(ids)))
    session.execute(delete(DeadLetterSignal).where(DeadLetterSignal.id.in_(ids)))
    session.commit()
    return len(ids)


def run_retention(
    settings: Settings, session: Session, *, apply: bool, now: datetime | None = None
) -> list[RuleResult]:
    now = now or datetime.now(UTC)
    batch = settings.RETENTION_BATCH_SIZE
    rules = (
        (
            "signal_receipts",
            settings.RETENTION_SIGNAL_RECEIPT_DAYS,
            _receipt_candidates,
            purge_receipts,
        ),
        (
            "dead_letter_signals",
            settings.RETENTION_DEAD_LETTER_DAYS,
            _dead_letter_candidates,
            purge_dead_letters,
        ),
    )
    results: list[RuleResult] = []
    for name, days, candidates, purge in rules:
        if days == 0:
            results.append(RuleResult(name, 0, None, 0, 0))
            continue
        cutoff = _cutoff(now, days)
        eligible = _count(session, candidates(cutoff))
        deleted = 0
        if apply:
            while True:
                removed = purge(session, cutoff, batch)
                deleted += removed
                if removed < batch:
                    break
            logger.info(
                "retention applied",
                extra={"rule": name, "retention_days": days, "deleted": deleted},
            )
        results.append(RuleResult(name, days, cutoff, eligible, deleted))
    return results


def format_results(results: list[RuleResult], *, apply: bool) -> str:
    lines = [("APPLIED" if apply else "DRY RUN (nothing deleted; pass --apply to delete)")]
    for result in results:
        if result.cutoff is None:
            lines.append(f"  {result.name}: disabled (retention days = 0, kept forever)")
            continue
        outcome = f"deleted {result.deleted}" if apply else f"would delete {result.eligible}"
        lines.append(
            f"  {result.name}: older than {result.retention_days} days "
            f"(before {result.cutoff:%Y-%m-%d %H:%M} UTC): {result.eligible} eligible, {outcome}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(prog="opspilot_api.maintenance.retention")
    parser.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    args = parser.parse_args()
    settings = get_settings()
    if settings.RETENTION_SIGNAL_RECEIPT_DAYS == 0 and settings.RETENTION_DEAD_LETTER_DAYS == 0:
        print(
            "Retention is disabled: set RETENTION_SIGNAL_RECEIPT_DAYS "
            "and/or RETENTION_DEAD_LETTER_DAYS."
        )
        sys.exit(0)
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            print(
                format_results(run_retention(settings, session, apply=args.apply), apply=args.apply)
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
