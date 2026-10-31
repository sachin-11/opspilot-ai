"""Platform seed data and dataset import. Model names live here as data, never in domain logic.

Pricing for the Anthropic seed row follows the published per-million-token rates as of
2026-10-06 for prompts up to 100K tokens; tiered long-context pricing is not modelled, so cost
estimates for larger prompts would be understated and the row says so in its version label.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opspilot_api.llmops.dataset import case_hash, dataset_hash
from opspilot_api.llmops.eval_types import EvalCaseSpec
from opspilot_api.llmops.golden_ops import golden_cases
from opspilot_api.llmops.models import (
    EvalCaseRow,
    EvalDataset,
    ModelConfiguration,
    PricingRateRow,
    RoutingPolicyRow,
)

GOLDEN_DATASET_NAME = "opspilot-golden"
ALL_WORKFLOWS = ["investigation", "remediation", "knowledge"]
# (provider, model, rate version, input $/M, output $/M)
_RATES = [
    ("fake", "deterministic-v1", "fake-zero-v1", 0.0, 0.0),
    ("anthropic", "claude-haiku-5-5", "anthropic-2026-10-06-le100k", 0.10, 0.50),
]
_CONFIGS = [
    # (key, provider, model, tier, tools, max_in, max_out, envs)
    ("fake-deterministic", "fake", "deterministic-v1", 3, True, 100_000, 8_000, ["local", "test"]),
    (
        "anthropic-small",
        "anthropic",
        "claude-haiku-5-5",
        2,
        True,
        100_000,
        8_000,
        ["local", "test", "production"],
    ),
]
ROUTING_POLICY_VERSION = "routing-v1"


def seed_platform(session: Session) -> None:
    """Idempotent. Safe to call concurrently: unique constraints arbitrate."""
    if session.scalar(select(func.count()).select_from(PricingRateRow)) == 0:
        for provider, model, version, inp, out in _RATES:
            session.add(
                PricingRateRow(
                    provider=provider,
                    model=model,
                    version=version,
                    input_per_million=inp,
                    output_per_million=out,
                )
            )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
    if session.scalar(select(func.count()).select_from(ModelConfiguration)) == 0:
        rates = {(r.provider, r.model): r.id for r in session.scalars(select(PricingRateRow))}
        for key, provider, model, tier, tools, max_in, max_out, envs in _CONFIGS:
            session.add(
                ModelConfiguration(
                    config_key=key,
                    version=1,
                    provider=provider,
                    model=model,
                    workflows=ALL_WORKFLOWS,
                    capability_tier=tier,
                    supports_structured_output=True,
                    supports_tools=tools,
                    max_input_tokens=max_in,
                    max_output_tokens=max_out,
                    timeout_seconds=30.0,
                    retry_policy={"max_retries": 2, "backoff_seconds": 0.05},
                    pricing_rate_id=rates.get((provider, model)),
                    environments=envs,
                    fallback_config_key=None,
                    status="published",
                    published_at=datetime.now(UTC),
                )
            )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
    if session.scalar(select(func.count()).select_from(RoutingPolicyRow)) == 0:
        rules = [
            {
                "workflow": wf,
                "node": None,
                "preference": ["anthropic-small", "fake-deterministic"],
                # Protected (high/critical) decisions need tier >= 3; the small tier is excluded.
                "min_tier_by_risk": {"high": 3, "critical": 3},
                "variant_overrides": {},
            }
            for wf in ALL_WORKFLOWS
        ]
        session.add(RoutingPolicyRow(version=ROUTING_POLICY_VERSION, rules=rules, status="active"))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
    session.commit()


def row_to_spec(row: EvalCaseRow) -> EvalCaseSpec:
    return EvalCaseSpec.model_validate(
        {
            "case_key": row.case_key,
            "suite": row.suite,
            "split": row.split,
            "input_data": row.input_data,
            "expected_output": row.expected_output,
            "expected_tools": row.expected_tools,
            "required_evidence": row.required_evidence,
            "forbidden_claims": row.forbidden_claims,
            "tags": row.tags,
            "severity": row.severity,
            "source": row.source,
        }
    )


def import_dataset(
    session: Session,
    cases: list[EvalCaseSpec],
    *,
    name: str,
    workflow: str,
    description: str,
    workspace_id: UUID | None,
    user_id: UUID | None,
    publish: bool,
) -> EvalDataset:
    """Create a new immutable dataset version. Never modifies an existing version."""
    latest = (
        session.scalar(
            select(func.max(EvalDataset.version)).where(
                EvalDataset.name == name,
                EvalDataset.workspace_id.is_(workspace_id)
                if workspace_id is None
                else EvalDataset.workspace_id == workspace_id,
            )
        )
        or 0
    )
    dataset = EvalDataset(
        workspace_id=workspace_id,
        name=name,
        workflow=workflow,
        description=description,
        version=latest + 1,
        status="draft",
        content_hash=dataset_hash(cases),
        case_count=len(cases),
        created_by_user_id=user_id,
    )
    session.add(dataset)
    session.flush()
    for spec in cases:
        session.add(
            EvalCaseRow(
                dataset_id=dataset.id,
                case_key=spec.case_key,
                suite=spec.suite,
                split=spec.split,
                input_data=spec.input_data,
                expected_output=spec.expected_output,
                expected_tools=[t.model_dump() for t in spec.expected_tools],
                required_evidence=spec.required_evidence,
                forbidden_claims=spec.forbidden_claims,
                tags=spec.tags,
                severity=spec.severity,
                source=spec.source,
                content_hash=case_hash(spec),
            )
        )
    session.flush()
    if publish:
        dataset.status, dataset.published_at = "published", datetime.now(UTC)
    session.commit()
    return dataset


def ensure_golden_dataset(session: Session) -> EvalDataset:
    existing = session.scalar(
        select(EvalDataset)
        .where(EvalDataset.name == GOLDEN_DATASET_NAME, EvalDataset.workspace_id.is_(None))
        .order_by(EvalDataset.version.desc())
        .limit(1)
    )
    cases = golden_cases()
    if existing is not None and existing.content_hash == dataset_hash(cases):
        return existing
    # Changed golden content becomes a NEW version; the old version stays untouched.
    return import_dataset(
        session,
        cases,
        name=GOLDEN_DATASET_NAME,
        workflow="investigation",
        description="Consolidated golden set across eight suites (Modules 2-6 fixtures extended).",
        workspace_id=None,
        user_id=None,
        publish=True,
    )
