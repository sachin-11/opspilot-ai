"""PostgreSQL budget reservations: the production counterpart of ``InMemoryBudgetStore``.

Atomicity comes from the database, not from the application: each scope is charged with one
conditional ``UPDATE ... WHERE spent + reserved + :amount <= hard_limit``. Row locks serialize
concurrent reservations of the same scope, scopes are visited in sorted order (no deadlocks), and
the whole reservation is one transaction (all scopes or none).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_api.llmops.budget import BudgetExceeded
from opspilot_api.llmops.models import BudgetPolicyRow, BudgetReservationRow


def scope_keys_for(
    workspace_id: UUID,
    *,
    now: datetime,
    investigation_id: UUID | None = None,
    remediation_id: UUID | None = None,
    user_id: UUID | None = None,
    workflow: str | None = None,
) -> dict[str, str]:
    """Map budget scope names to the concrete counter key for this call."""
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    keys = {
        "daily_workspace": f"daily_workspace:{workspace_id}:{day}",
        "monthly_workspace": f"monthly_workspace:{workspace_id}:{month}",
    }
    if investigation_id:
        keys["investigation"] = f"investigation:{investigation_id}"
    if remediation_id:
        keys["remediation"] = f"remediation:{remediation_id}"
    if user_id:
        keys["user"] = f"user:{workspace_id}:{user_id}:{day}"
    if workflow:
        keys["workflow"] = f"workflow:{workspace_id}:{workflow}:{day}"
    return keys


async def resolve_limits(
    session: AsyncSession, workspace_id: UUID, keys: dict[str, str], workflow: str | None
) -> dict[str, tuple[float, float]]:
    """Return {counter_key: (hard_limit, soft_fraction)} for enabled policies that apply."""
    rows = (
        (
            await session.execute(
                select(BudgetPolicyRow).where(
                    BudgetPolicyRow.workspace_id == workspace_id, BudgetPolicyRow.enabled.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, tuple[float, float]] = {}
    for row in rows:
        key = keys.get(row.scope)
        if key is None:
            continue
        if row.scope == "workflow" and row.workflow not in ("*", workflow):
            continue
        out[key] = (float(row.hard_limit_usd), row.soft_fraction)
    return out


async def _event(
    session: AsyncSession, workspace_id: UUID, kind: str, key: str, detail: str
) -> None:
    await session.execute(
        text(
            "INSERT INTO budget_events (id, workspace_id, kind, scope_key, detail) "
            "VALUES (:id, :ws, :kind, :key, :detail)"
        ),
        {"id": uuid4(), "ws": workspace_id, "kind": kind, "key": key, "detail": detail[:200]},
    )


async def reserve(
    session: AsyncSession,
    workspace_id: UUID,
    limits: dict[str, tuple[float, float]],
    amount_usd: float,
    *,
    ttl_seconds: int,
    now: datetime | None = None,
) -> UUID:
    """Atomically reserve ``amount_usd`` against every scope or raise ``BudgetExceeded``."""
    now = now or datetime.now(UTC)
    blocked: tuple[str, float, float] | None = None
    soft: list[tuple[str, float]] = []
    for key in sorted(limits):
        hard, _ = limits[key]
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope_key, workspace_id, hard_limit_usd, spent_usd, "
                "reserved_usd, unmetered_calls) VALUES (:k, :ws, :hard, 0, 0, 0) "
                "ON CONFLICT (scope_key) DO UPDATE SET hard_limit_usd = EXCLUDED.hard_limit_usd"
            ),
            {"k": key, "ws": workspace_id, "hard": hard},
        )
    for key in sorted(limits):
        hard, soft_fraction = limits[key]
        row = (
            await session.execute(
                text(
                    "UPDATE budget_counters SET reserved_usd = reserved_usd + :a "
                    "WHERE scope_key = :k AND workspace_id = :ws "
                    "AND spent_usd + reserved_usd + :a <= hard_limit_usd "
                    "RETURNING spent_usd, reserved_usd"
                ),
                {"a": amount_usd, "k": key, "ws": workspace_id},
            )
        ).first()
        if row is None:
            current = (
                await session.execute(
                    text("SELECT spent_usd + reserved_usd FROM budget_counters WHERE scope_key=:k"),
                    {"k": key},
                )
            ).scalar_one()
            blocked = (key, hard, float(current) + amount_usd)
            break
        total_after = float(row.spent_usd) + float(row.reserved_usd)
        if total_after - amount_usd < hard * soft_fraction <= total_after:
            soft.append((key, hard * soft_fraction))
    if blocked is not None:
        await session.rollback()  # undo every earlier scope's charge: all or nothing
        await _event(session, workspace_id, "hard_block", blocked[0], f"{blocked[2]:.6f}")
        await session.commit()
        raise BudgetExceeded(blocked[0], blocked[1], blocked[2])
    for key, threshold in soft:
        await _event(session, workspace_id, "soft_warning", key, f"threshold {threshold:.6f}")
    reservation = BudgetReservationRow(
        id=uuid4(),
        workspace_id=workspace_id,
        scope_keys=sorted(limits),
        amount_usd=amount_usd,
        status="reserved",
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(reservation)
    await session.commit()
    return reservation.id


async def reconcile(session: AsyncSession, reservation_id: UUID, actual_usd: float | None) -> str:
    """Idempotent. ``None`` keeps the reserved ceiling as spend and flags the call unmetered."""
    res = (
        await session.execute(
            select(BudgetReservationRow)
            .where(BudgetReservationRow.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one()
    if res.status in ("reconciled", "released"):
        current = res.status  # read before rollback expires the instance
        await session.rollback()
        return current
    charge = float(res.amount_usd) if actual_usd is None else actual_usd
    was_reserved = res.status == "reserved"
    for key in sorted(res.scope_keys):
        await session.execute(
            text(
                "UPDATE budget_counters SET "
                "reserved_usd = CASE WHEN :was THEN GREATEST(0, reserved_usd - :amt) "
                "ELSE reserved_usd END, spent_usd = spent_usd + :charge, "
                "unmetered_calls = unmetered_calls + :unm WHERE scope_key = :k"
            ),
            {
                "was": was_reserved,
                "amt": float(res.amount_usd),
                "charge": charge,
                "unm": 1 if actual_usd is None else 0,
                "k": key,
            },
        )
    if not was_reserved:
        await _event(
            session,
            res.workspace_id,
            "late_charge",
            ",".join(res.scope_keys)[:150],
            f"{charge:.6f}",
        )
    res.status, res.actual_usd, res.cost_unavailable = "reconciled", charge, actual_usd is None
    await session.commit()
    return "reconciled"


async def release(session: AsyncSession, reservation_id: UUID) -> str:
    res = (
        await session.execute(
            select(BudgetReservationRow)
            .where(BudgetReservationRow.id == reservation_id)
            .with_for_update()
        )
    ).scalar_one()
    if res.status != "reserved":
        current = res.status  # read before rollback expires the instance
        await session.rollback()
        return current
    for key in sorted(res.scope_keys):
        await session.execute(
            text(
                "UPDATE budget_counters SET reserved_usd = GREATEST(0, reserved_usd - :a) "
                "WHERE scope_key = :k"
            ),
            {"a": float(res.amount_usd), "k": key},
        )
    res.status = "released"
    await session.commit()
    return "released"


async def expire_stale(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Release headroom held by reservations whose call never reconciled."""
    now = now or datetime.now(UTC)
    stale = (
        (
            await session.execute(
                select(BudgetReservationRow)
                .where(
                    BudgetReservationRow.status == "reserved",
                    BudgetReservationRow.expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for res in stale:
        for key in sorted(res.scope_keys):
            await session.execute(
                text(
                    "UPDATE budget_counters SET reserved_usd = GREATEST(0, reserved_usd - :a) "
                    "WHERE scope_key = :k"
                ),
                {"a": float(res.amount_usd), "k": key},
            )
        res.status = "expired"
        await _event(
            session, res.workspace_id, "expired", ",".join(res.scope_keys)[:150], str(res.id)
        )
    await session.commit()
    return len(stale)


async def reconcile_against_ledger(session: AsyncSession) -> list[tuple[str, float, float]]:
    """Report counters whose spend differs from the append-only usage ledger (drift detection).

    Only workspace-day/month and investigation scopes are comparable to the ledger.
    """
    rows = (
        await session.execute(
            text(
                "SELECT c.scope_key, c.spent_usd::float, COALESCE(("
                " SELECT SUM(u.estimated_cost_usd)::float FROM usage_events u"
                " WHERE u.workspace_id = c.workspace_id AND ("
                "  (c.scope_key LIKE 'investigation:%' AND u.investigation_id::text = "
                "   substring(c.scope_key from 15))"
                "  OR (c.scope_key LIKE 'daily_workspace:%' "
                "   AND to_char(u.occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD') "
                "   = right(c.scope_key, 10))"
                "  OR (c.scope_key LIKE 'monthly_workspace:%' "
                "   AND to_char(u.occurred_at AT TIME ZONE 'UTC','YYYY-MM') "
                "   = right(c.scope_key, 7)))), 0) AS ledger "
                "FROM budget_counters c"
            )
        )
    ).all()
    return [(r[0], r[1], r[2]) for r in rows if abs(r[1] - r[2]) > 1e-6]
