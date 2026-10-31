"""Retention against live PostgreSQL: only finished data older than the window is removed."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.maintenance.retention import format_results, run_retention
from opspilot_api.observability.models import (
    DeadLetterReplay,
    DeadLetterSignal,
    ObservabilitySource,
    ReceiptStatus,
    SignalReceipt,
)

from .obs_helpers import make_source, make_workspace, obs_settings

pytestmark = pytest.mark.integration

OLD = datetime.now(UTC) - timedelta(days=40)
RECENT = datetime.now(UTC) - timedelta(days=2)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(obs_settings().database_url)
    with Session(engine) as db:
        yield db
    engine.dispose()


def retention_settings(**overrides: int) -> Settings:
    return obs_settings(
        RETENTION_SIGNAL_RECEIPT_DAYS=30, RETENTION_DEAD_LETTER_DAYS=30, **overrides
    )


def add_receipt(
    session: Session, source: ObservabilitySource, status: ReceiptStatus, received_at: datetime
) -> uuid.UUID:
    key = uuid.uuid4().hex
    receipt = SignalReceipt(
        workspace_id=source.workspace_id,
        service_id=source.service_id,
        source_id=source.id,
        signal_id=uuid.uuid4(),
        provider="generic_webhook",
        ingress_key=key,
        deduplication_key=key,
        payload_hash="0" * 64,
        schema_version="1",
        status=status.value,
        received_at=received_at,
    )
    session.add(receipt)
    session.flush()
    return receipt.id


def add_dead_letter(
    session: Session,
    source: ObservabilitySource,
    created_at: datetime,
    *,
    replayed: bool = False,
    resolved: bool = False,
) -> uuid.UUID:
    letter = DeadLetterSignal(
        workspace_id=source.workspace_id,
        source_id=source.id,
        original_topic="raw",
        payload_hash="1" * 64,
        failure_code="unsupported_signal_type",
        failure_message="test",
        first_failed_at=created_at,
        last_failed_at=created_at,
        created_at=created_at,
        replayed_at=created_at if replayed else None,
        resolved_at=created_at if resolved else None,
    )
    session.add(letter)
    session.flush()
    if replayed:
        session.add(
            DeadLetterReplay(
                workspace_id=source.workspace_id, dead_letter_id=letter.id, outcome="replayed"
            )
        )
        session.flush()
    return letter.id


def present(
    session: Session, model: type[SignalReceipt] | type[DeadLetterSignal], ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    return set(session.scalars(select(model.id).where(model.id.in_(ids))))


def test_retention_is_off_unless_configured(session: Session) -> None:
    workspace, service = make_workspace(session)
    source, _ = make_source(session, obs_settings(), workspace, service)
    old = add_receipt(session, source, ReceiptStatus.PROCESSED, OLD)
    session.commit()
    results = run_retention(obs_settings(), session, apply=True)  # days default to 0
    assert all(result.cutoff is None and result.deleted == 0 for result in results)
    assert present(session, SignalReceipt, [old]) == {old}
    assert "disabled" in format_results(results, apply=True)


def test_a_dry_run_counts_but_deletes_nothing(session: Session) -> None:
    workspace, service = make_workspace(session)
    source, _ = make_source(session, obs_settings(), workspace, service)
    old = add_receipt(session, source, ReceiptStatus.PROCESSED, OLD)
    session.commit()
    results = {r.name: r for r in run_retention(retention_settings(), session, apply=False)}
    assert results["signal_receipts"].eligible >= 1
    assert results["signal_receipts"].deleted == 0
    assert present(session, SignalReceipt, [old]) == {old}
    assert "DRY RUN" in format_results(list(results.values()), apply=False)


def test_apply_removes_only_old_finished_receipts(session: Session) -> None:
    workspace, service = make_workspace(session)
    source, _ = make_source(session, obs_settings(), workspace, service)
    purged = [
        add_receipt(session, source, ReceiptStatus.PROCESSED, OLD),
        add_receipt(session, source, ReceiptStatus.DUPLICATE, OLD),
    ]
    kept = [
        add_receipt(session, source, ReceiptStatus.FAILED, OLD),
        add_receipt(session, source, ReceiptStatus.DEAD_LETTERED, OLD),
        add_receipt(session, source, ReceiptStatus.RECEIVED, OLD),
        add_receipt(session, source, ReceiptStatus.PROCESSED, RECENT),
    ]
    session.commit()
    run_retention(retention_settings(), session, apply=True)
    assert present(session, SignalReceipt, purged) == set()
    assert present(session, SignalReceipt, kept) == set(kept)


def test_apply_removes_old_replayed_or_resolved_dead_letters_with_their_replays(
    session: Session,
) -> None:
    workspace, service = make_workspace(session)
    source, _ = make_source(session, obs_settings(), workspace, service)
    purged = [
        add_dead_letter(session, source, OLD, replayed=True),
        add_dead_letter(session, source, OLD, resolved=True),
    ]
    kept = [
        add_dead_letter(session, source, OLD),  # still needs attention
        add_dead_letter(session, source, RECENT, replayed=True),
    ]
    session.commit()
    run_retention(retention_settings(), session, apply=True)
    assert present(session, DeadLetterSignal, purged) == set()
    assert present(session, DeadLetterSignal, kept) == set(kept)
    orphans = session.scalar(
        select(func.count())
        .select_from(DeadLetterReplay)
        .where(DeadLetterReplay.dead_letter_id.in_(purged))
    )
    assert orphans == 0


def test_deletion_runs_in_batches_until_nothing_is_left(session: Session) -> None:
    workspace, service = make_workspace(session)
    source, _ = make_source(session, obs_settings(), workspace, service)
    ids = [add_receipt(session, source, ReceiptStatus.PROCESSED, OLD) for _ in range(25)]
    session.commit()
    # The minimum allowed batch is 10, so 25 rows need three passes.
    results = {
        r.name: r
        for r in run_retention(retention_settings(RETENTION_BATCH_SIZE=10), session, apply=True)
    }
    assert results["signal_receipts"].deleted >= 25
    assert present(session, SignalReceipt, ids) == set()
