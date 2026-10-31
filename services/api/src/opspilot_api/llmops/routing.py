"""Versioned, deterministic model routing.

``RoutingInput`` deliberately has no free-text field: only trusted, typed values can influence
selection, so user-controlled text cannot choose a provider or model. The same input and policy
version always produce the same decision. Fallbacks are filtered by the same capability, policy,
and budget constraints as the primary and never silently downgrade a protected decision.
"""

from dataclasses import dataclass, field
from typing import Literal

from opspilot_api.investigations.providers import (
    ProviderConfigurationError,
    ProviderError,
    RetryableProviderError,
    StructuredOutputError,
)

Risk = Literal["low", "medium", "high", "critical"]
BreakerState = Literal["closed", "open", "half_open"]
RISK_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
# Decisions at or above this risk may not run on a model below the policy's minimum tier.
PROTECTED_RISK = "high"


class NoEligibleModel(Exception):
    code = "no_eligible_model"

    def __init__(self, reason_codes: list[str]) -> None:
        super().__init__("no eligible model: " + ",".join(reason_codes))
        self.reason_codes = reason_codes


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    backoff_seconds: float = 0.05


@dataclass(frozen=True)
class ModelConfig:
    id: str
    version: int
    provider: str
    model: str
    workflows: frozenset[str]
    capability_tier: int
    supports_structured_output: bool
    supports_tools: bool
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: float
    pricing_rate_version: str | None
    input_per_million: float | None
    output_per_million: float | None
    enabled: bool = True
    environments: frozenset[str] = frozenset({"local", "test", "production"})
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    fallback_config_id: str | None = None

    def max_cost_usd(self, input_tokens: int) -> float | None:
        """Upper bound for one call, or None when rates are unknown (never guessed)."""
        if self.input_per_million is None or self.output_per_million is None:
            return None
        return (
            input_tokens * self.input_per_million + self.max_output_tokens * self.output_per_million
        ) / 1_000_000


@dataclass(frozen=True)
class RoutingRule:
    workflow: str
    node: str | None  # None matches every node of the workflow
    preference: tuple[str, ...]  # model config ids in order
    min_tier_by_risk: tuple[tuple[str, int], ...] = ()
    variant_overrides: tuple[tuple[str, str], ...] = ()  # experiment variant -> config id


@dataclass(frozen=True)
class RoutingPolicy:
    version: str
    rules: tuple[RoutingRule, ...]


@dataclass(frozen=True)
class RoutingInput:
    workflow: str
    node: str
    risk: Risk
    requires_structured_output: bool
    requires_tools: bool
    context_tokens: int
    environment: str = "production"
    cost_ceiling_usd: float | None = None
    latency_slo_ms: float | None = None
    allowed_providers: frozenset[str] | None = None  # workspace policy
    provider_health: tuple[tuple[str, BreakerState], ...] = ()
    experiment_variant: str | None = None
    experiment_id: str | None = None


@dataclass(frozen=True)
class RoutingDecision:
    policy_version: str
    config_id: str
    config_version: int
    provider: str
    model: str
    reason_codes: tuple[str, ...]
    fallback_chain: tuple[str, ...]
    max_cost_usd: float | None
    experiment_id: str | None
    experiment_variant: str | None


def validate_policy(policy: RoutingPolicy) -> None:
    """Every rule must pin a minimum capability tier for protected risk levels."""
    for rule in policy.rules:
        tiers = dict(rule.min_tier_by_risk)
        for risk in ("high", "critical"):
            if risk not in tiers:
                raise ValueError(
                    f"rule {rule.workflow}/{rule.node or '*'} must set min tier for {risk} risk"
                )


def _rule_for(policy: RoutingPolicy, inp: RoutingInput) -> RoutingRule:
    exact = [r for r in policy.rules if r.workflow == inp.workflow and r.node == inp.node]
    generic = [r for r in policy.rules if r.workflow == inp.workflow and r.node is None]
    for rule in (*exact, *generic):
        return rule
    raise NoEligibleModel(["no_rule_for_workflow"])


def _reasons_rejected(cfg: ModelConfig, inp: RoutingInput, min_tier: int) -> str | None:
    health = dict(inp.provider_health)
    if not cfg.enabled:
        return "disabled"
    if inp.environment not in cfg.environments:
        return "environment_not_enabled"
    if inp.workflow not in cfg.workflows:
        return "workflow_not_supported"
    if inp.requires_structured_output and not cfg.supports_structured_output:
        return "missing_structured_output"
    if inp.requires_tools and not cfg.supports_tools:
        return "missing_tool_calling"
    if inp.context_tokens > cfg.max_input_tokens:
        return "context_too_large"
    if inp.allowed_providers is not None and cfg.provider not in inp.allowed_providers:
        return "workspace_policy"
    if health.get(cfg.provider, "closed") == "open":
        return "breaker_open"
    if cfg.capability_tier < min_tier:
        return "below_required_tier"
    if inp.cost_ceiling_usd is not None:
        cost = cfg.max_cost_usd(inp.context_tokens)
        if cost is None or cost > inp.cost_ceiling_usd:
            return "over_cost_ceiling"
    return None


def route(
    inp: RoutingInput, policy: RoutingPolicy, configs: dict[str, ModelConfig]
) -> RoutingDecision:
    rule = _rule_for(policy, inp)
    min_tier = dict(rule.min_tier_by_risk).get(inp.risk, 0)
    preference = list(rule.preference)
    reasons: list[str] = [f"policy:{policy.version}"]
    override = dict(rule.variant_overrides).get(inp.experiment_variant or "")
    if override and RISK_RANK[inp.risk] >= RISK_RANK[PROTECTED_RISK]:
        override = None  # protected decisions are never part of an experiment
        reasons.append("experiment_ignored:protected_risk")
    if override:
        preference = [override, *[c for c in preference if c != override]]
        reasons.append(f"experiment:{inp.experiment_variant}")
    eligible: list[ModelConfig] = []
    rejected: list[str] = []
    for config_id in preference:
        cfg = configs.get(config_id)
        if cfg is None:
            rejected.append(f"{config_id}:unknown_config")
            continue
        why = _reasons_rejected(cfg, inp, min_tier)
        if why is None:
            eligible.append(cfg)
        else:
            rejected.append(f"{config_id}:{why}")
    if not eligible:
        raise NoEligibleModel(rejected or ["empty_preference"])
    chosen, *rest = eligible
    if rejected:
        reasons.append("skipped:" + ";".join(rejected))
    if RISK_RANK[inp.risk] >= RISK_RANK[PROTECTED_RISK]:
        reasons.append("protected_decision:no_downgrade")
    return RoutingDecision(
        policy_version=policy.version,
        config_id=chosen.id,
        config_version=chosen.version,
        provider=chosen.provider,
        model=chosen.model,
        reason_codes=tuple(reasons),
        fallback_chain=tuple(c.id for c in rest),
        max_cost_usd=chosen.max_cost_usd(inp.context_tokens),
        experiment_id=inp.experiment_id if override else None,
        experiment_variant=inp.experiment_variant if override else None,
    )


@dataclass(frozen=True)
class ShadowComparison:
    active: RoutingDecision
    shadow: RoutingDecision | None
    shadow_error: str | None
    diverges: bool


def shadow_route(
    inp: RoutingInput,
    active: RoutingPolicy,
    shadow: RoutingPolicy,
    configs: dict[str, ModelConfig],
) -> ShadowComparison:
    """Evaluate a not-yet-active policy alongside the active one. Only ``active`` is executed."""
    decision = route(inp, active, configs)
    try:
        other = route(inp, shadow, configs)
    except NoEligibleModel as exc:
        return ShadowComparison(decision, None, ",".join(exc.reason_codes), True)
    return ShadowComparison(decision, other, None, other.config_id != decision.config_id)


def may_fall_back(error: BaseException) -> bool:
    """Existing retryability rules: only temporary provider failures fall back.

    Configuration/authentication failures and invalid structured output never fall back silently.
    """
    if isinstance(error, ProviderConfigurationError | StructuredOutputError):
        return False
    return isinstance(error, RetryableProviderError) and isinstance(error, ProviderError)
