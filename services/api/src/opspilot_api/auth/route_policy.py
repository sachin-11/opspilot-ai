"""The single, central map from (method, route template) to the permission a route requires.

``classify`` returns ``None`` for any route not listed, and the authorization dependency denies
those: a newly added endpoint is inaccessible until it is explicitly classified. A test asserts that
every registered route is classified.
"""

import re
from dataclasses import dataclass

from opspilot_api.auth.permissions import ApiScope, Permission

P = Permission
WS = r"/api/v1/workspaces/\{workspace_id\}"
READ = frozenset({"GET", "HEAD"})
WRITE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ANY = READ | WRITE


@dataclass(frozen=True)
class RoutePolicy:
    permission: Permission | None = None
    api_scope: ApiScope | None = None  # the only scope admitting an API key; None = human only
    rate: str | None = None
    audit: str | None = None  # pre-written, fail-closed audit action for sensitive routes
    public: bool = False  # no identity required (liveness, verified webhooks, auth plumbing)
    authenticated: bool = False  # any authenticated human; no workspace in the path


def _p(
    methods: frozenset[str],
    pattern: str,
    permission: Permission | None = None,
    **kwargs: object,
) -> tuple[frozenset[str], re.Pattern[str], RoutePolicy]:
    return methods, re.compile(f"^{pattern}$"), RoutePolicy(permission, **kwargs)  # type: ignore[arg-type]


_RULES = [
    # Public infrastructure
    _p(READ, r"/health/(live|ready)", public=True),
    _p(READ, r"/openapi\.json|/docs|/docs/oauth2-redirect|/redoc", public=True),
    _p(READ, r"/api/v1/meta", public=True),
    _p(frozenset({"POST"}), r"/api/v1/ingest/webhooks/\{public_id\}", public=True, rate="webhook"),
    _p(frozenset({"POST"}), r"/api/v1/integrations/github/webhook", public=True, rate="webhook"),
    _p(ANY, r"/api/v1/auth/local/(authorize|token)", public=True),
    _p(READ, r"/api/v1/auth/local/(\.well-known/openid-configuration|users)", public=True),
    # Authenticated, workspace-less
    _p(READ, r"/api/v1/auth/(me|workspaces|permission-matrix)", authenticated=True),
    # Process-level counters (no payloads); any signed-in user, never anonymous.
    _p(READ, r"/api/v1/metrics", authenticated=True),
    _p(frozenset({"POST"}), r"/api/v1/auth/ws-ticket", authenticated=True, rate="ws_ticket"),
    _p(
        frozenset({"POST"}),
        r"/api/v1/invitations/accept",
        authenticated=True,
        rate="invitation_accept",
    ),
    _p(READ, r"/api/v1/workspaces", authenticated=True),
    _p(frozenset({"POST"}), r"/api/v1/workspaces", authenticated=True),  # service audits atomically
    # Workspace
    _p(READ, WS, P.WORKSPACE_READ),
    # Members, invitations, keys, audit
    _p(READ, WS + r"/members", P.MEMBERS_READ),
    _p(frozenset({"PATCH", "DELETE"}), WS + r"/members/\{membership_id\}", P.MEMBERS_MANAGE),
    _p(frozenset({"POST"}), WS + r"/invitations", P.MEMBERS_MANAGE, rate="invitation"),
    _p(READ, WS + r"/invitations", P.MEMBERS_MANAGE),
    _p(frozenset({"POST"}), WS + r"/invitations/\{invitation_id\}/revoke", P.MEMBERS_MANAGE),
    _p(frozenset({"POST"}), WS + r"/api-keys", P.API_KEYS_MANAGE),
    _p(READ, WS + r"/api-keys", P.API_KEYS_MANAGE),
    _p(frozenset({"POST"}), WS + r"/api-keys/\{api_key_id\}/(rotate|revoke)", P.API_KEYS_MANAGE),
    _p(READ, WS + r"/audit-events", P.AUDIT_READ),
    # Services
    _p(
        READ,
        WS + r"/services(/\{service_id\})?",
        P.SERVICES_READ,
        api_scope=ApiScope.INCIDENTS_READ,
    ),
    _p(READ, WS + r"/services/\{service_id\}/repositories", P.SERVICES_READ),
    _p(frozenset({"POST"}), WS + r"/services", P.SERVICES_MANAGE),
    _p(
        frozenset({"POST", "DELETE"}),
        WS + r"/services/\{service_id\}/repositories(/\{mapping_id\})?",
        P.SERVICES_MANAGE,
    ),
    # Incidents and events
    _p(
        frozenset({"POST"}),
        WS + r"/events",
        P.INCIDENTS_MANAGE,
        api_scope=ApiScope.EVENTS_WRITE,
    ),
    _p(
        READ,
        WS + r"/incidents(/\{incident_id\}(/timeline)?)?",
        P.INCIDENTS_READ,
        api_scope=ApiScope.INCIDENTS_READ,
    ),
    _p(frozenset({"POST"}), WS + r"/incidents", P.INCIDENTS_MANAGE),
    _p(
        frozenset({"POST", "PATCH"}),
        WS + r"/incidents/\{incident_id\}/(notes|severity|status)",
        P.INCIDENTS_MANAGE,
    ),
    _p(READ, WS + r"/incidents/\{incident_id\}/observability", P.OBSERVABILITY_READ),
    _p(READ, WS + r"/incidents/\{incident_id\}/suspect-changes", P.REPOSITORIES_READ),
    # Investigations
    _p(
        READ,
        WS + r"/incidents/\{incident_id\}/investigations",
        P.INVESTIGATIONS_READ,
        api_scope=ApiScope.INVESTIGATIONS_READ,
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/incidents/\{incident_id\}/investigations",
        P.INVESTIGATIONS_RUN,
        rate="investigation_start",
        audit="investigation.start",
    ),
    _p(
        READ,
        WS + r"/investigations/\{investigation_id\}",
        P.INVESTIGATIONS_READ,
        api_scope=ApiScope.INVESTIGATIONS_READ,
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/investigations/\{investigation_id\}/(cancel|retry)",
        P.INVESTIGATIONS_RUN,
        rate="llm_operation",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/investigations/\{investigation_id\}/review",
        P.INVESTIGATIONS_REVIEW,
        rate="approval_attempt",
        audit="investigation.review",
    ),
    # Remediation
    _p(
        frozenset({"POST"}),
        WS + r"/investigations/\{investigation_id\}/remediations",
        P.REMEDIATIONS_CREATE,
        rate="remediation_create",
        audit="remediation.create",
    ),
    _p(READ, WS + r"/remediations(/.*)?", P.REMEDIATIONS_READ),
    _p(
        frozenset({"POST"}),
        WS + r"/remediations/\{remediation_id\}/(cancel|retry)",
        P.REMEDIATIONS_CREATE,
        rate="remediation_create",
        audit="remediation.change",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/remediations/\{remediation_id\}/review",
        P.REMEDIATIONS_REVIEW,
        rate="approval_attempt",
        audit="remediation.review",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/remediations/\{remediation_id\}/publish",
        P.REMEDIATIONS_PUBLISH,
        rate="approval_attempt",
        audit="remediation.publish",
    ),
    # LLMOps (prompts, evaluations, experiments, usage, budgets). All human-only: no API scopes.
    _p(READ, WS + r"/llmops/.*", P.LLMOPS_READ),
    _p(
        frozenset({"POST"}),
        WS + r"/llmops/prompts/\{prompt_id\}/(versions|publish|rollback)",
        P.LLMOPS_PROMPTS_MANAGE,
        rate="approval_attempt",
        audit="prompt.change",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/llmops/eval-runs(/\{eval_run_id\}/cancel)?",
        P.LLMOPS_EVALS_RUN,
        rate="remediation_create",
        audit="eval.run",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/llmops/eval-results/\{result_id\}/human-label",
        P.LLMOPS_EVALS_LABEL,
        audit="eval.label",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/llmops/experiments",
        P.LLMOPS_EXPERIMENTS_MANAGE,
        audit="experiment.create",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/llmops/experiments/\{experiment_id\}/(start|pause|stop)",
        P.LLMOPS_EXPERIMENTS_MANAGE,
        rate="approval_attempt",
        audit="experiment.change",
    ),
    _p(
        frozenset({"PATCH"}),
        WS + r"/llmops/budgets/\{budget_id\}",
        P.LLMOPS_BUDGETS_MANAGE,
        rate="approval_attempt",
        audit="budget.change",
    ),
    # Knowledge
    _p(
        frozenset({"POST"}),
        WS + r"/knowledge/search",
        P.KNOWLEDGE_READ,
        api_scope=ApiScope.KNOWLEDGE_READ,
        rate="knowledge_search",
    ),
    _p(READ, WS + r"/knowledge/.*", P.KNOWLEDGE_READ, api_scope=ApiScope.KNOWLEDGE_READ),
    _p(frozenset({"POST"}), WS + r"/knowledge/.*", P.KNOWLEDGE_MANAGE),
    # Repositories and integrations
    _p(
        frozenset({"POST"}),
        WS + r"/repositories/search",
        P.REPOSITORIES_READ,
        rate="repository_search",
    ),
    _p(READ, WS + r"/repositories(/.*)?", P.REPOSITORIES_READ),
    _p(
        frozenset({"POST"}),
        WS + r"/repositories/\{repository_id\}/(reindex|sync)",
        P.REPOSITORIES_MANAGE,
    ),
    _p(READ, WS + r"/integrations/github/installations", P.INTEGRATIONS_READ),
    _p(
        frozenset({"POST"}),
        WS + r"/integrations/github/installations/sync",
        P.INTEGRATIONS_MANAGE,
        audit="integration.sync",
    ),
    # Observability
    _p(
        frozenset({"POST"}),
        WS + r"/observability/dead-letters/\{dead_letter_id\}/replay",
        P.OBSERVABILITY_MANAGE,
        audit="deadletter.replay",
    ),
    _p(
        frozenset({"POST"}),
        WS + r"/observability/sources/\{source_id\}/rotate-secret",
        P.OBSERVABILITY_MANAGE,
        audit="integration.rotate_secret",
    ),
    _p(
        frozenset({"POST", "PATCH"}),
        WS + r"/observability/(sources.*|demo/generate)",
        P.OBSERVABILITY_MANAGE,
        audit="integration.configure",
    ),
    _p(READ, WS + r"/observability/.*", P.OBSERVABILITY_READ),
]


def classify(method: str, template: str) -> RoutePolicy | None:
    method = method.upper()
    for methods, pattern, policy in _RULES:
        if method in methods and pattern.match(template):
            return policy
    return None
