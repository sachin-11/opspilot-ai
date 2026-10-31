"""LLMOps against live PostgreSQL + Redis: DB-level immutability, atomic budgets, API flows, RBAC.

Uses the deterministic fake provider and the local identity provider only. No paid API is called.
"""

import asyncio
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import Workspace
from opspilot_api.llmops import budget_pg
from opspilot_api.llmops.budget import BudgetExceeded
from opspilot_api.llmops.jobs import execute_eval_run
from opspilot_api.llmops.models import (
    BudgetCounter,
    EvalCaseRow,
    EvalDataset,
    EvalRun,
    PricingRateRow,
    PromptDefinition,
    PromptVersion,
    UsageEventRow,
)
from opspilot_api.llmops.seed import ensure_golden_dataset, seed_platform
from opspilot_api.llmops.usage import UsageEvent
from opspilot_api.llmops.usage_pg import record_usage
from opspilot_api.main import create_app

from .conftest import anonymous_client
from .repo_helpers import live_settings
from .test_auth_integration import add_member, ensure_user, headers, make_workspace, new_subject

pytestmark = pytest.mark.integration


@pytest.fixture
def settings() -> Settings:
    overrides: dict[str, Any] = {}
    url = os.environ.get("REMEDIATION_TEST_REDIS_URL")
    if url:
        overrides["REDIS_URL"] = SecretStr(url)
    return live_settings(**overrides)


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    engine = create_engine(settings.database_url)
    with Session(engine) as db:
        seed_platform(db)
        yield db
    engine.dispose()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with anonymous_client(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def _rejected(session: Session, statement: str, params: dict[str, Any] | None = None) -> str:
    with pytest.raises(DBAPIError) as err:
        session.execute(text(statement), params or {})
        session.commit()
    session.rollback()
    return str(err.value)


def _workspace_with_owner(session: Session) -> tuple[Workspace, str]:
    workspace, subject = make_workspace(session), new_subject("owner")
    add_member(session, workspace, subject, "owner")
    return workspace, subject


def _usage(ws: Workspace, key: str, **kw: Any) -> UsageEvent:
    base: dict[str, Any] = dict(
        id=str(uuid4()),
        idempotency_key=key,
        workspace_id=str(ws.id),
        workflow="investigation",
        node_name="report",
        provider="fake",
        model="deterministic-v1",
        input_tokens=100,
        output_tokens=50,
        latency_ms=12.0,
        status="succeeded",
        occurred_at=datetime.now(UTC),
        pricing_rate_version="fake-zero-v1",
        estimated_cost_usd=0.001,
    )
    return UsageEvent(**{**base, **kw})


# ------------------------------------------------------------------ database-level guarantees
def test_usage_events_are_append_only_and_idempotent(session: Session, settings: Settings) -> None:
    ws = make_workspace(session)
    key = f"req-{uuid4().hex}"

    async def ingest() -> tuple[bool, bool]:
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                first = await record_usage(s, _usage(ws, key, estimated_cost_usd=0.5))
                replay = await record_usage(s, _usage(ws, key, estimated_cost_usd=99.0))
                return first, replay
        finally:
            await engine.dispose()

    assert asyncio.run(ingest()) == (True, False)
    row = session.scalar(select(UsageEventRow).where(UsageEventRow.idempotency_key == key))
    assert row is not None and float(row.estimated_cost_usd or 0) == 0.5  # replay changed nothing
    assert "append-only" in _rejected(
        session, "UPDATE usage_events SET input_tokens = 1 WHERE idempotency_key = :k", {"k": key}
    )
    assert "append-only" in _rejected(
        session, "DELETE FROM usage_events WHERE idempotency_key = :k", {"k": key}
    )


def test_pricing_rates_are_immutable(session: Session) -> None:
    rate = session.scalar(select(PricingRateRow).limit(1))
    assert rate is not None
    assert "immutable" in _rejected(
        session, "UPDATE pricing_rates SET input_per_million = 999 WHERE id = :i", {"i": rate.id}
    )


def _make_prompt(session: Session, ws: Workspace, user_id: UUID) -> PromptDefinition:
    pd = PromptDefinition(
        workspace_id=ws.id,
        name=f"p-{uuid4().hex[:6]}",
        workflow="investigation",
        node_name="report",
        description="d",
        status="active",
    )
    session.add(pd)
    session.commit()
    return pd


def _version(
    pd: PromptDefinition, user_id: UUID, semver: str, status: str, default: bool = False
) -> PromptVersion:
    return PromptVersion(
        prompt_definition_id=pd.id,
        semantic_version=semver,
        status=status,
        is_default=default,
        system_template="You are careful.",
        developer_template=None,
        input_schema={"fields": {}},
        output_schema={},
        change_notes="n",
        content_hash="a" * 64,
        created_by_user_id=user_id,
    )


def test_published_prompt_content_is_immutable_and_single_default(session: Session) -> None:
    ws, subject = _workspace_with_owner(session)
    user = ensure_user(session, subject)
    pd = _make_prompt(session, ws, user.id)
    v1 = _version(pd, user.id, "1.0.0", "published", default=True)
    v2 = _version(pd, user.id, "1.0.1", "published", default=False)
    session.add_all([v1, v2])
    session.commit()
    assert "immutable" in _rejected(
        session, "UPDATE prompt_versions SET system_template = 'evil' WHERE id = :i", {"i": v1.id}
    )
    assert "cannot be deleted" in _rejected(
        session, "DELETE FROM prompt_versions WHERE id = :i", {"i": v1.id}
    )
    with pytest.raises(IntegrityError):  # a second default for the same prompt
        session.execute(
            update(PromptVersion).where(PromptVersion.id == v2.id).values(is_default=True)
        )
        session.commit()
    session.rollback()
    # Rollback is only a pointer flip: content stays untouched and the update is allowed.
    session.execute(update(PromptVersion).where(PromptVersion.id == v1.id).values(is_default=False))
    session.execute(update(PromptVersion).where(PromptVersion.id == v2.id).values(is_default=True))
    session.commit()
    assert session.scalar(
        select(PromptVersion.system_template).where(PromptVersion.id == v1.id)
    ) == ("You are careful.")


def test_published_dataset_and_its_cases_are_immutable(session: Session) -> None:
    dataset = ensure_golden_dataset(session)
    assert dataset.status == "published" and dataset.case_count >= 103
    assert (
        session.scalar(
            select(func.count())
            .select_from(EvalCaseRow)
            .where(EvalCaseRow.dataset_id == dataset.id)
        )
        == dataset.case_count
    )
    assert "immutable" in _rejected(
        session, "UPDATE eval_cases SET severity = 'low' WHERE dataset_id = :d", {"d": dataset.id}
    )
    assert "immutable" in _rejected(
        session, "DELETE FROM eval_cases WHERE dataset_id = :d", {"d": dataset.id}
    )
    assert "immutable" in _rejected(
        session, "UPDATE eval_datasets SET case_count = 1 WHERE id = :d", {"d": dataset.id}
    )
    again = ensure_golden_dataset(session)  # unchanged content must not create a new version
    assert again.id == dataset.id


# ------------------------------------------------------------------ atomic budgets
def test_concurrent_reservations_cannot_overspend_in_postgres(
    session: Session, settings: Settings
) -> None:
    ws = make_workspace(session)
    key_a, key_b = f"investigation:{uuid4()}", f"daily_workspace:{ws.id}:2026-10-31"
    limits = {key_a: (1.0, 0.8), key_b: (100.0, 0.8)}

    async def scenario() -> tuple[int, int, list[str]]:
        engine = create_async_engine(settings.database_url, pool_size=30, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        outcomes: list[str] = []

        async def attempt() -> None:
            async with factory() as s:
                try:
                    await budget_pg.reserve(s, ws.id, limits, 0.3, ttl_seconds=60)
                    outcomes.append("ok")
                except BudgetExceeded:
                    outcomes.append("blocked")

        try:
            await asyncio.gather(*(attempt() for _ in range(24)))
            async with factory() as s:
                reserved = (
                    await s.execute(
                        select(BudgetCounter.reserved_usd).where(BudgetCounter.scope_key == key_a)
                    )
                ).scalar_one()
                other = (
                    await s.execute(
                        select(BudgetCounter.reserved_usd).where(BudgetCounter.scope_key == key_b)
                    )
                ).scalar_one()
            return outcomes.count("ok"), outcomes.count("blocked"), [str(reserved), str(other)]
        finally:
            await engine.dispose()

    ok, blocked, reserved = asyncio.run(scenario())
    assert ok == 3 and blocked == 21  # 3 x 0.30 fits under 1.00; a 4th never does
    assert float(reserved[0]) == pytest.approx(0.9) and float(reserved[1]) == pytest.approx(0.9)


def test_reconcile_release_expire_and_ledger_drift_in_postgres(
    session: Session, settings: Settings
) -> None:
    ws = make_workspace(session)
    key = f"investigation:{uuid4()}"
    limits = {key: (1.0, 0.5)}

    async def scenario() -> dict[str, Any]:
        engine = create_async_engine(settings.database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                r1 = await budget_pg.reserve(s, ws.id, limits, 0.4, ttl_seconds=60)
                await budget_pg.reconcile(s, r1, 0.15)
                await budget_pg.reconcile(s, r1, 0.9)  # replay ignored
                r2 = await budget_pg.reserve(s, ws.id, limits, 0.3, ttl_seconds=60)
                await budget_pg.release(s, r2)
                r3 = await budget_pg.reserve(s, ws.id, limits, 0.2, ttl_seconds=-1)  # already stale
                expired = await budget_pg.expire_stale(s)
                r4 = await budget_pg.reserve(
                    s, ws.id, limits, 0.4, ttl_seconds=60
                )  # crosses soft 0.5
                await budget_pg.reconcile(s, r4, None)  # unknown cost: ceiling kept, flagged
                counter_row = (
                    await s.execute(select(BudgetCounter).where(BudgetCounter.scope_key == key))
                ).scalar_one()
                snapshot = (
                    float(counter_row.spent_usd),
                    float(counter_row.reserved_usd),
                    counter_row.unmetered_calls,
                )
                try:
                    await budget_pg.reserve(s, ws.id, limits, 0.9, ttl_seconds=60)
                    hard = False
                except BudgetExceeded:
                    hard = True
                events = (
                    (
                        await s.execute(
                            text("SELECT kind FROM budget_events WHERE scope_key = :k"), {"k": key}
                        )
                    )
                    .scalars()
                    .all()
                )
                drift = await budget_pg.reconcile_against_ledger(s)
                return {
                    "spent": snapshot[0],
                    "reserved": snapshot[1],
                    "unmetered": snapshot[2],
                    "expired": expired,
                    "events": events,
                    "hard": hard,
                    "drift": [d for d in drift if d[0] == key],
                    "r3": r3,
                }
        finally:
            await engine.dispose()

    out = asyncio.run(scenario())
    assert out["spent"] == pytest.approx(0.55) and out["reserved"] == pytest.approx(0.0)
    assert out["unmetered"] == 1 and out["expired"] >= 1 and out["hard"] is True
    assert {"soft_warning", "hard_block", "expired"} <= set(out["events"])
    assert out["drift"] and out["drift"][0][2] == 0.0  # counters spent > ledger (nothing ingested)


# ------------------------------------------------------------------ API flow
def _run_eval(
    client: TestClient,
    session: Session,
    ws: Workspace,
    hdr: dict[str, str],
    dataset_id: str,
    variant: str,
    prompt_versions: dict[str, str],
    control: str | None = "baseline",
    run_type: str = "candidate_comparison",
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/workspaces/{ws.id}/llmops/eval-runs",
        headers=hdr,
        json={
            "dataset_id": dataset_id,
            "variant": variant,
            "control_variant": control,
            "run_type": run_type,
            "purpose": "gate",
            "repeat_count": 3,
            "prompt_versions": prompt_versions,
        },
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["id"])
    execute_eval_run(session, run_id)  # the RQ job body, run inline
    detail = client.get(f"/api/v1/workspaces/{ws.id}/llmops/eval-runs/{run_id}", headers=hdr)
    assert detail.status_code == 200
    return {"id": str(run_id), **detail.json()}


def test_prompt_publication_is_gated_by_evaluation_and_rollback_preserves_history(
    client: TestClient, session: Session
) -> None:
    ws, subject = _workspace_with_owner(session)
    hdr = headers(subject)
    base = f"/api/v1/workspaces/{ws.id}/llmops"
    prompts = client.get(f"{base}/prompts", headers=hdr).json()
    assert {p["name"] for p in prompts} >= {"triage", "plan", "hypotheses", "critique", "report"}
    report = next(p for p in prompts if p["name"] == "report")
    assert report["default_version"] == "1.0.0"
    datasets = client.get(f"{base}/datasets", headers=hdr).json()
    golden = next(d for d in datasets if d["name"] == "opspilot-golden")
    assert golden["case_count"] >= 103 and len(golden["suite_counts"]) == 8

    # Credentials are rejected at creation.
    bad = client.post(
        f"{base}/prompts/{report['id']}/versions",
        headers=hdr,
        json={
            "system_template": "Use api_key=sk-live-AbCdEf1234567890xyz when calling.",
            "change_notes": "oops",
        },
    )
    assert bad.status_code == 422

    good = client.post(
        f"{base}/prompts/{report['id']}/versions",
        headers=hdr,
        json={
            "system_template": "You write careful incident reports and cite only the given ids.",
            "change_notes": "tighter citation instruction",
        },
    )
    assert good.status_code == 201 and good.json()["semantic_version"] == "1.0.1"
    v_good = good.json()["id"]

    ok_run = _run_eval(client, session, ws, hdr, golden["id"], "candidate", {"report": v_good})
    assert ok_run["status"] == "completed" and ok_run["gate_report"]["passed"] is True
    assert ok_run["metrics_source"] == "synthetic" and ok_run["violations"]["tenant_leaks"] == 0

    published = client.post(
        f"{base}/prompts/{report['id']}/publish",
        headers=hdr,
        json={"version_id": v_good, "eval_run_id": ok_run["id"]},
    )
    assert published.status_code == 200 and published.json()["is_default"] is True
    detail = client.get(f"{base}/prompts/{report['id']}", headers=hdr).json()
    assert detail["default_version"] == "1.0.1"
    assert sum(1 for v in detail["versions"] if v["is_default"]) == 1

    # A regressing candidate is blocked, and an override cannot clear safety invariants.
    worse = client.post(
        f"{base}/prompts/{report['id']}/versions",
        headers=hdr,
        json={
            "system_template": "You write reports quickly without worrying about evidence at all.",
            "change_notes": "faster but sloppy",
        },
    ).json()["id"]
    bad_run = _run_eval(client, session, ws, hdr, golden["id"], "regressing", {"report": worse})
    assert bad_run["gate_report"]["passed"] is False and bad_run["violations"]["tenant_leaks"] > 0
    blocked = client.post(
        f"{base}/prompts/{report['id']}/publish",
        headers=hdr,
        json={"version_id": worse, "eval_run_id": bad_run["id"]},
    )
    assert blocked.status_code == 409
    override = client.post(
        f"{base}/prompts/{report['id']}/publish",
        headers=hdr,
        json={
            "version_id": worse,
            "eval_run_id": bad_run["id"],
            "override_reason": "ship it anyway, demo tomorrow",
        },
    )
    assert override.status_code == 409 and "safety invariants" in override.text
    assert (
        client.get(f"{base}/prompts/{report['id']}", headers=hdr).json()["default_version"]
        == "1.0.1"
    )

    # Publishing with an evaluation run that did not evaluate this version is refused.
    mismatch = client.post(
        f"{base}/prompts/{report['id']}/publish",
        headers=hdr,
        json={"version_id": worse, "eval_run_id": ok_run["id"]},
    )
    assert mismatch.status_code == 409

    rolled = client.post(
        f"{base}/prompts/{report['id']}/rollback",
        headers=hdr,
        json={
            "target_version_id": next(
                v["id"] for v in detail["versions"] if v["semantic_version"] == "1.0.0"
            ),
            "reason": "restore the seeded prompt after review",
        },
    )
    assert rolled.status_code == 200 and rolled.json()["semantic_version"] == "1.0.0"
    after = client.get(f"{base}/prompts/{report['id']}", headers=hdr).json()
    assert after["default_version"] == "1.0.0"
    assert {v["semantic_version"] for v in after["versions"]} >= {"1.0.0", "1.0.1", "1.0.2"}


def test_eval_run_report_comparison_and_human_labels(client: TestClient, session: Session) -> None:
    ws, subject = _workspace_with_owner(session)
    hdr = headers(subject)
    base = f"/api/v1/workspaces/{ws.id}/llmops"
    golden = next(
        d
        for d in client.get(f"{base}/datasets", headers=hdr).json()
        if d["name"] == "opspilot-golden"
    )
    run = _run_eval(client, session, ws, hdr, golden["id"], "candidate", {})
    assert run["total_cases"] >= 103 and run["failed_cases"] == 0 and run["flaky_cases"] == []
    assert run["comparison"]["control_variant"] == "baseline"
    assert set(run["by_suite"]) == set(golden["suite_counts"])
    assert run["markdown_report"] and "Safety violations" in run["markdown_report"]

    bad = _run_eval(client, session, ws, hdr, golden["id"], "regressing", {})
    assert bad["failed_results"] and bad["label_queue"]
    target = bad["label_queue"][0]
    label = client.post(
        f"{base}/eval-results/{target['id']}/human-label",
        headers=hdr,
        json={
            "rubric_version": "judge-rubric-v1",
            "criterion_scores": {"relevance": 2},
            "passed": False,
            "notes": "answer cites an unknown id",
        },
    )
    assert label.status_code == 200 and label.json()["passed"] is False
    updated = client.post(
        f"{base}/eval-results/{target['id']}/human-label",
        headers=hdr,
        json={
            "rubric_version": "judge-rubric-v1",
            "criterion_scores": {"relevance": 3},
            "passed": False,
        },
    )
    assert updated.json()["id"] == label.json()["id"]  # the same reviewer edits their own label
    wrong_rubric = client.post(
        f"{base}/eval-results/{target['id']}/human-label",
        headers=hdr,
        json={"rubric_version": "made-up", "criterion_scores": {"relevance": 3}, "passed": True},
    )
    assert wrong_rubric.status_code == 422
    refreshed = client.get(f"{base}/eval-runs/{bad['id']}", headers=hdr).json()
    assert target["id"] not in {r["id"] for r in refreshed["label_queue"]}  # now labelled


def test_experiment_lifecycle_and_budget_guardrails(client: TestClient, session: Session) -> None:
    ws, subject = _workspace_with_owner(session)
    hdr = headers(subject)
    base = f"/api/v1/workspaces/{ws.id}/llmops"
    body = {
        "name": "report prompt trial",
        "workflow": "investigation",
        "control_variant": {"label": "control", "eval_variant": "baseline"},
        "candidate_variant": {"label": "candidate", "eval_variant": "candidate"},
        "allocation_percentage": 10,
        "primary_metric": "pass_rate",
        "guardrail_metrics": ["safety_rate"],
        "minimum_sample_size": 100,
    }
    bad = client.post(f"{base}/experiments", headers=hdr, json={**body, "primary_metric": "vibes"})
    assert bad.status_code == 422
    created = client.post(f"{base}/experiments", headers=hdr, json=body)
    assert created.status_code == 201 and created.json()["status"] == "draft"
    exp = created.json()["id"]
    assert client.post(f"{base}/experiments/{exp}/pause", headers=hdr).status_code == 409
    started = client.post(f"{base}/experiments/{exp}/start", headers=hdr)
    assert started.status_code == 200 and started.json()["status"] == "running"
    detail = client.get(f"{base}/experiments/{exp}", headers=hdr).json()
    assert detail["analysis"]["sample_size_reached"] is False
    assert any("minimum sample size" in w for w in detail["analysis"]["warnings"])
    assert detail["analysis"]["promotable_by_data"] is False
    stopped = client.post(
        f"{base}/experiments/{exp}/stop",
        headers=hdr,
        json={"reason": "candidate leaks in shadow", "safety": True},
    )
    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    assert stopped.json()["stop_reason"].startswith("SAFETY")

    budgets = client.get(f"{base}/budgets", headers=hdr).json()
    daily = next(b for b in budgets if b["scope"] == "daily_workspace")
    assert daily["state"] == "ok" and daily["spent_usd"] == 0
    lower = client.patch(f"{base}/budgets/{daily['id']}", headers=hdr, json={"hard_limit_usd": 5})
    assert lower.status_code == 200 and lower.json()["hard_limit_usd"] == 5
    no_reason = client.patch(
        f"{base}/budgets/{daily['id']}", headers=hdr, json={"hard_limit_usd": 50}
    )
    assert no_reason.status_code == 422  # raising a limit is an override and needs a reason
    raised = client.patch(
        f"{base}/budgets/{daily['id']}",
        headers=hdr,
        json={"hard_limit_usd": 50, "reason": "extended sev1 investigation"},
    )
    assert raised.status_code == 200 and raised.json()["hard_limit_usd"] == 50


def test_usage_costs_and_provider_health_endpoints(
    client: TestClient, session: Session, settings: Settings
) -> None:
    ws, subject = _workspace_with_owner(session)
    hdr = headers(subject)
    base = f"/api/v1/workspaces/{ws.id}/llmops"
    empty = client.get(f"{base}/usage?group_by=model", headers=hdr).json()
    assert (
        empty["total_requests"] == 0 and empty["latency_p95_ms"] is None and empty["buckets"] == []
    )

    async def ingest() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                await record_usage(s, _usage(ws, f"a-{uuid4().hex}", estimated_cost_usd=0.2))
                await record_usage(
                    s,
                    _usage(
                        ws,
                        f"b-{uuid4().hex}",
                        estimated_cost_usd=None,
                        pricing_rate_version=None,
                        model="unpriced",
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(ingest())
    usage = client.get(f"{base}/usage?group_by=model", headers=hdr).json()
    assert usage["total_requests"] == 2 and {b["key"] for b in usage["buckets"]} == {
        "deterministic-v1",
        "unpriced",
    }
    costs = client.get(f"{base}/costs", headers=hdr).json()
    assert costs["total_cost_usd"] == pytest.approx(0.2) and costs["unavailable_cost_requests"] == 1
    filtered = client.get(f"{base}/costs?model=unpriced", headers=hdr).json()
    assert filtered["total_cost_usd"] == 0 and filtered["unavailable_cost_requests"] == 1
    configs = client.get(f"{base}/model-configurations", headers=hdr).json()
    assert {c["provider"] for c in configs} >= {"fake", "anthropic"}
    assert all(c["pricing_rate_version"] for c in configs)
    health = client.get(f"{base}/providers/health", headers=hdr).json()
    assert {h["provider"] for h in health} >= {"fake", "anthropic"}
    assert all(h["breaker_state"] in ("closed", "open", "half_open", "unknown") for h in health)


# ------------------------------------------------------------------ RBAC and tenant isolation
def test_llmops_rbac_matrix_and_cross_workspace_isolation(
    client: TestClient, session: Session
) -> None:
    ws, owner = _workspace_with_owner(session)
    members = {role: new_subject(role) for role in ("viewer", "analyst", "responder", "admin")}
    for role, subject in members.items():
        add_member(session, ws, subject, role)
    base = f"/api/v1/workspaces/{ws.id}/llmops"
    prompt = client.get(f"{base}/prompts", headers=headers(owner)).json()[0]["id"]

    for role in ("viewer", "analyst"):  # no LLMOps access at all
        assert client.get(f"{base}/prompts", headers=headers(members[role])).status_code == 403
        assert client.get(f"{base}/usage", headers=headers(members[role])).status_code == 403
    responder = headers(members["responder"])
    assert client.get(f"{base}/usage", headers=responder).status_code == 200
    assert (
        client.post(
            f"{base}/prompts/{prompt}/publish",
            headers=responder,
            json={"version_id": str(uuid4()), "eval_run_id": str(uuid4())},
        ).status_code
        == 403
    )
    assert client.post(f"{base}/experiments", headers=responder, json={}).status_code == 403
    assert client.patch(f"{base}/budgets/{uuid4()}", headers=responder, json={}).status_code == 403
    admin = headers(members["admin"])
    created = client.post(
        f"{base}/prompts/{prompt}/versions",
        headers=admin,
        json={
            "system_template": "A valid long enough system template for the test.",
            "change_notes": "admin edit",
        },
    )
    assert created.status_code == 201
    # Overrides are owner-only even though admins can manage prompts.
    run = _run_eval(
        client,
        session,
        ws,
        headers(owner),
        next(
            d
            for d in client.get(f"{base}/datasets", headers=headers(owner)).json()
            if d["name"] == "opspilot-golden"
        )["id"],
        "regressing",
        {"x": created.json()["id"]},
    )
    denied = client.post(
        f"{base}/prompts/{prompt}/publish",
        headers=admin,
        json={
            "version_id": created.json()["id"],
            "eval_run_id": run["id"],
            "override_reason": "an admin trying to force this through",
        },
    )
    assert denied.status_code == 409

    # Another workspace cannot see or touch this workspace's resources.
    other_ws, other_owner = _workspace_with_owner(session)
    other = headers(other_owner)
    assert client.get(f"{base}/prompts", headers=other).status_code in (403, 404)
    other_base = f"/api/v1/workspaces/{other_ws.id}/llmops"
    assert client.get(f"{other_base}/prompts/{prompt}", headers=other).status_code == 404
    assert client.get(f"{other_base}/eval-runs/{run['id']}", headers=other).status_code == 404
    assert (
        client.post(f"{other_base}/eval-runs/{run['id']}/cancel", headers=other).status_code == 404
    )
    assert client.get(f"{base}/prompts", headers={}).status_code == 401


def test_every_llmops_endpoint_requires_authentication(client: TestClient) -> None:
    ws = uuid4()
    base = f"/api/v1/workspaces/{ws}/llmops"
    for method, path in (
        ("get", "/prompts"),
        ("get", "/datasets"),
        ("get", "/eval-runs"),
        ("get", "/experiments"),
        ("get", "/usage"),
        ("get", "/costs"),
        ("get", "/budgets"),
        ("get", "/providers/health"),
        ("get", "/model-configurations"),
        ("post", "/eval-runs"),
        ("post", "/experiments"),
    ):
        assert getattr(client, method)(base + path).status_code == 401, path


def test_eval_run_counts_in_database_match_the_api(session: Session) -> None:
    dataset = ensure_golden_dataset(session)
    run = EvalRun(
        dataset_id=dataset.id,
        dataset_version=dataset.version,
        prompt_versions={},
        run_type="offline",
        purpose="gate",
        variant="baseline",
        repeat_count=1,
        seed_configuration={},
        status="queued",
    )
    session.add(run)
    session.commit()
    execute_eval_run(session, run.id)
    session.refresh(run)
    assert run.status == "completed" and run.total_cases == dataset.case_count
    assert run.failed_cases == 1 and run.metrics_source == "synthetic"  # the known weak case
    assert session.get(EvalDataset, dataset.id) is not None
    _ = Callable


# ------------------------------------------------------------------ Redis-shared circuit breaker
def test_circuit_breaker_state_is_shared_through_redis_with_half_open_recovery(
    settings: Settings,
) -> None:
    from redis import Redis

    from opspilot_api.llmops.breaker import BreakerConfig, CircuitBreaker, RedisBreakerStore

    client = Redis.from_url(settings.REDIS_URL.get_secret_value())
    prefix = f"opspilot:test-breaker:{uuid4().hex}:"
    clock = {"now": 5000.0}
    events: list[tuple[str, str, str, str]] = []
    config = BreakerConfig(
        consecutive_failures=2, open_seconds=30, half_open_probes=2, probes_to_close=2
    )

    def make() -> CircuitBreaker:  # two independent "processes" sharing one Redis
        return CircuitBreaker(
            "provider-x",
            RedisBreakerStore(client, prefix),
            config,
            clock=lambda: clock["now"],
            on_transition=lambda *a: events.append(a),
        )

    a, b = make(), make()
    try:
        a.record("retryable_failure")
        b.record("timeout")  # the second process completes the failure streak
        assert a.current_state() == "open" and not a.allow() and not b.allow()
        clock["now"] += 31
        assert b.current_state() == "half_open"
        assert a.allow() and b.allow() and not a.allow()  # bounded probes across processes
        a.record("success")
        b.record("success")
        assert a.current_state() == "closed" and b.allow()
        assert [e[2] for e in events] == ["open", "half_open", "closed"]
    finally:
        for key in client.scan_iter(prefix + "*"):
            client.delete(key)
        client.close()
