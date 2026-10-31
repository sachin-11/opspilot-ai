"""Centralized permissions, the role matrix, and API-key scopes. No route checks role names."""

from enum import StrEnum

from opspilot_api.auth.models import Role


class Permission(StrEnum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_MANAGE = "workspace.manage"
    MEMBERS_READ = "members.read"
    MEMBERS_MANAGE = "members.manage"
    SERVICES_READ = "services.read"
    SERVICES_MANAGE = "services.manage"
    INCIDENTS_READ = "incidents.read"
    INCIDENTS_MANAGE = "incidents.manage"
    INVESTIGATIONS_READ = "investigations.read"
    INVESTIGATIONS_RUN = "investigations.run"
    INVESTIGATIONS_REVIEW = "investigations.review"
    KNOWLEDGE_READ = "knowledge.read"
    KNOWLEDGE_MANAGE = "knowledge.manage"
    INTEGRATIONS_READ = "integrations.read"
    INTEGRATIONS_MANAGE = "integrations.manage"
    OBSERVABILITY_READ = "observability.read"
    OBSERVABILITY_MANAGE = "observability.manage"
    REPOSITORIES_READ = "repositories.read"
    REPOSITORIES_MANAGE = "repositories.manage"
    REMEDIATIONS_READ = "remediations.read"
    REMEDIATIONS_CREATE = "remediations.create"
    REMEDIATIONS_REVIEW = "remediations.review"
    REMEDIATIONS_PUBLISH = "remediations.publish"
    API_KEYS_MANAGE = "api_keys.manage"
    AUDIT_READ = "audit.read"
    LLMOPS_READ = "llmops.read"
    LLMOPS_PROMPTS_MANAGE = "llmops.prompts.manage"
    LLMOPS_EVALS_RUN = "llmops.evals.run"
    LLMOPS_EVALS_LABEL = "llmops.evals.label"
    LLMOPS_EXPERIMENTS_MANAGE = "llmops.experiments.manage"
    LLMOPS_BUDGETS_MANAGE = "llmops.budgets.manage"


P = Permission
_READ = frozenset(
    {
        P.WORKSPACE_READ,
        P.MEMBERS_READ,
        P.SERVICES_READ,
        P.INCIDENTS_READ,
        P.INVESTIGATIONS_READ,
        P.KNOWLEDGE_READ,
        P.INTEGRATIONS_READ,
        P.OBSERVABILITY_READ,
        P.REPOSITORIES_READ,
        P.REMEDIATIONS_READ,
    }
)
_ANALYST = _READ | {P.INVESTIGATIONS_RUN}
_RESPONDER = _ANALYST | {
    P.INCIDENTS_MANAGE,
    P.REMEDIATIONS_CREATE,
    P.REMEDIATIONS_REVIEW,
    P.SERVICES_MANAGE,
    P.INVESTIGATIONS_REVIEW,
    # Reviewers may read LLMOps dashboards and label evaluation results; they cannot change
    # prompts, run paid evaluations, or alter experiments and budgets.
    P.LLMOPS_READ,
    P.LLMOPS_EVALS_LABEL,
}
_ADMIN = _RESPONDER | {
    P.WORKSPACE_MANAGE,
    P.MEMBERS_MANAGE,
    P.KNOWLEDGE_MANAGE,
    P.INTEGRATIONS_MANAGE,
    P.OBSERVABILITY_MANAGE,
    P.REPOSITORIES_MANAGE,
    P.REMEDIATIONS_PUBLISH,
    P.API_KEYS_MANAGE,
    P.AUDIT_READ,
    P.LLMOPS_PROMPTS_MANAGE,
    P.LLMOPS_EVALS_RUN,
    P.LLMOPS_EXPERIMENTS_MANAGE,
    P.LLMOPS_BUDGETS_MANAGE,
}
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _READ,
    Role.ANALYST: frozenset(_ANALYST),
    Role.RESPONDER: frozenset(_RESPONDER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(Permission),
}
# Owner-only decisions that are not expressible as a single permission.
OWNER_ONLY_ACTIONS = frozenset({"members.grant_owner", "members.modify_owner"})


def permissions_for(role: str) -> frozenset[Permission]:
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return frozenset()  # unknown role: no access (fail closed)


class ApiScope(StrEnum):
    EVENTS_WRITE = "events:write"
    INCIDENTS_READ = "incidents:read"
    KNOWLEDGE_READ = "knowledge:read"
    INVESTIGATIONS_READ = "investigations:read"


# API keys are authorized per route: a route lists the one scope that admits a key. Routes that
# list no scope are human-only, which is the default (approval, publication, members, keys, audit).
SCOPES = frozenset(scope.value for scope in ApiScope)


def matrix() -> dict[str, list[str]]:
    return {role.value: sorted(p.value for p in perms) for role, perms in ROLE_PERMISSIONS.items()}
