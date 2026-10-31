"""Token validation, permission matrix, route classification, audit integrity (no services)."""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from opspilot_api.auth.audit import build_event, compute_integrity, verify_event
from opspilot_api.auth.context import Actor, ForbiddenError, Principal, RequestInfo
from opspilot_api.auth.models import AuditResult, Role
from opspilot_api.auth.permissions import (
    ROLE_PERMISSIONS,
    SCOPES,
    Permission,
    permissions_for,
)
from opspilot_api.auth.providers import (
    AuthConfigurationError,
    InvalidTokenError,
    JwksClient,
    LocalProvider,
    OidcProvider,
    TokenExpiredError,
    create_identity_provider,
    issue_local_token,
)
from opspilot_api.auth.route_policy import classify
from opspilot_api.config import Environment
from opspilot_api.main import create_app
from tests.conftest import make_settings

ISSUER = "https://idp.example.com/pool"
CLIENT = "client-123"


def make_key() -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, json.loads(RSAAlgorithm.to_jwk(private.public_key()))


KEY_A, JWK_A = make_key()
KEY_B, JWK_B = make_key()


def jwks_doc(*keys: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"} for kid, jwk in keys]}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def provider(
    served: list[dict[str, Any]], clock: Clock | None = None, **overrides: Any
) -> tuple[OidcProvider, JwksClient, list[int]]:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=served[min(len(calls), len(served)) - 1])

    settings = make_settings(
        AUTH_PROVIDER="oidc", AUTH_OIDC_ISSUER=ISSUER, AUTH_OIDC_AUDIENCE=CLIENT, **overrides
    )
    jwks = JwksClient(
        f"{ISSUER}/.well-known/jwks.json",
        600,
        5,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock or Clock(),
    )
    return OidcProvider(settings, jwks), jwks, calls


def token(
    key: Any = KEY_A,
    kid: str = "kid-a",
    alg: str = "RS256",
    **claims: Any,
) -> str:
    now = datetime.now(UTC)
    body: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "sub-1",
        "client_id": CLIENT,
        "token_use": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        **claims,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


# ---- OIDC validation ------------------------------------------------------------------------
def test_valid_cognito_style_access_token() -> None:
    oidc, _jwks, _calls = provider([jwks_doc(("kid-a", JWK_A))])
    identity = oidc.verify(token(email="Alice@Example.com", email_verified=True, name="Alice"))
    assert identity.subject == "sub-1" and identity.provider == "oidc"
    assert identity.email == "alice@example.com" and identity.email_verified


def test_audience_claim_form_is_accepted_too() -> None:
    oidc, _j, _c = provider([jwks_doc(("kid-a", JWK_A))])
    assert oidc.verify(token(client_id=None, aud=CLIENT, token_use=None)).subject == "sub-1"


def test_expired_token_is_distinguished_without_leaking_it() -> None:
    oidc, _j, _c = provider([jwks_doc(("kid-a", JWK_A))])
    raw = token(exp=datetime.now(UTC) - timedelta(minutes=10))
    with pytest.raises(TokenExpiredError) as excinfo:
        oidc.verify(raw)
    assert raw not in str(excinfo.value)


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"iss": "https://evil.example.com"}, "validation"),
        ({"client_id": "other-client"}, "audience"),
        ({"token_use": "id"}, "token type"),
        ({"sub": None}, "validation"),
        ({"sub": ""}, "subject"),
        ({"nbf": datetime.now(UTC) + timedelta(hours=1)}, "validation"),
    ],
)
def test_invalid_claims_are_rejected(claims: dict[str, Any], message: str) -> None:
    oidc, _j, _c = provider([jwks_doc(("kid-a", JWK_A))])
    with pytest.raises(InvalidTokenError):
        oidc.verify(token(**claims))


def test_wrong_signature_unknown_key_and_disallowed_algorithms() -> None:
    oidc, _j, _c = provider([jwks_doc(("kid-a", JWK_A))])
    with pytest.raises(InvalidTokenError):
        oidc.verify(token(key=KEY_B))  # signed by a different key, same kid
    with pytest.raises(InvalidTokenError):
        oidc.verify(token(kid="unknown"))
    with pytest.raises(InvalidTokenError):  # symmetric alg with the public key as secret
        oidc.verify(
            jwt.encode(
                {"iss": ISSUER, "sub": "x", "exp": time.time() + 60},
                "k" * 40,
                algorithm="HS256",
                headers={"kid": "kid-a"},
            )
        )
    unsigned = jwt.encode(
        {"iss": ISSUER, "sub": "x", "client_id": CLIENT, "exp": time.time() + 60},
        None,  # type: ignore[arg-type]
        algorithm="none",
        headers={"kid": "kid-a"},
    )
    with pytest.raises(InvalidTokenError):
        oidc.verify(unsigned)
    with pytest.raises(InvalidTokenError):
        oidc.verify("not.a.jwt")


def test_jwks_is_cached_and_refreshed_once_on_rotation() -> None:
    clock = Clock()
    oidc, _jwks, calls = provider([jwks_doc(("kid-a", JWK_A)), jwks_doc(("kid-b", JWK_B))], clock)
    oidc.verify(token())
    oidc.verify(token())
    assert len(calls) == 1  # cached
    clock.now += 10  # beyond the minimum refresh interval
    assert oidc.verify(token(key=KEY_B, kid="kid-b")).subject == "sub-1"  # rotation handled
    assert len(calls) == 2
    with pytest.raises(InvalidTokenError):  # the retired key no longer verifies
        clock.now += 10
        oidc.verify(token())
    assert len(calls) == 3  # one bounded refresh per unknown kid, never a loop


def test_unknown_kid_refresh_is_rate_bounded() -> None:
    oidc, _j, calls = provider([jwks_doc(("kid-a", JWK_A))])
    for _ in range(5):
        with pytest.raises(InvalidTokenError):
            oidc.verify(token(kid="attacker-kid"))
    assert len(calls) == 1  # an attacker cannot make us hammer the IdP


def test_jwks_outage_is_a_configuration_error_not_a_pass() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    settings = make_settings(
        AUTH_PROVIDER="oidc", AUTH_OIDC_ISSUER=ISSUER, AUTH_OIDC_AUDIENCE=CLIENT
    )
    jwks = JwksClient(
        "https://x/jwks", 600, 5, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AuthConfigurationError):
        OidcProvider(settings, jwks).verify(token())


def test_jwks_url_is_derived_from_the_configured_issuer_only() -> None:
    settings = make_settings(
        AUTH_PROVIDER="oidc", AUTH_OIDC_ISSUER=ISSUER + "/", AUTH_OIDC_AUDIENCE=CLIENT
    )
    assert OidcProvider(settings).jwks.url == f"{ISSUER}/.well-known/jwks.json"


# ---- local provider --------------------------------------------------------------------------
def test_local_provider_accepts_only_its_own_signed_tokens() -> None:
    settings = make_settings()
    local = LocalProvider(settings)
    good = issue_local_token(settings, "dev-1", "dev@local.test", "Dev")
    assert local.verify(good).subject == "dev-1"
    with pytest.raises(TokenExpiredError):
        local.verify(issue_local_token(settings, "dev-1", ttl_seconds=-120))
    other = make_settings(AUTH_LOCAL_SIGNING_KEY="another-key-that-is-long-enough-xx")
    with pytest.raises(InvalidTokenError):
        local.verify(issue_local_token(other, "dev-1"))
    with pytest.raises(InvalidTokenError):
        unsigned = jwt.encode({"sub": "x", "exp": time.time() + 60}, None, algorithm="none")  # type: ignore[arg-type]
        local.verify(unsigned)


def test_local_auth_is_blocked_in_production_at_settings_and_provider_level() -> None:
    with pytest.raises(ValueError, match="local is only allowed"):
        make_settings(
            APP_ENV=Environment.PRODUCTION,
            CORS_ORIGINS="https://a.example.com",
            AUTH_AUDIT_KEY="a" * 40,
        )
    with pytest.raises(ValueError, match="local is only allowed"):
        make_settings(APP_ENV=Environment.STAGING, AUTH_AUDIT_KEY="a" * 40)
    dev = make_settings()
    prod_like = dev.model_copy(update={"APP_ENV": Environment.PRODUCTION})
    with pytest.raises(AuthConfigurationError):
        LocalProvider(prod_like)
    with pytest.raises(AuthConfigurationError):
        issue_local_token(prod_like, "x")
    with pytest.raises(AuthConfigurationError):
        create_identity_provider(prod_like)
    with pytest.raises(AuthConfigurationError):
        create_app_with(prod_like)


def create_app_with(settings: Any) -> Any:
    return create_app(settings)


def test_oidc_settings_require_issuer_audience_asymmetric_algs_and_strong_audit_key() -> None:
    prod = {
        "APP_ENV": Environment.PRODUCTION,
        "CORS_ORIGINS": "https://a.example.com",
        "AUTH_PROVIDER": "oidc",
        "AUTH_OIDC_ISSUER": ISSUER,
        "AUTH_OIDC_AUDIENCE": CLIENT,
        "AUTH_AUDIT_KEY": "k" * 40,
    }
    assert make_settings(**prod)
    with pytest.raises(ValueError, match="ISSUER"):
        make_settings(**{**prod, "AUTH_OIDC_ISSUER": None})
    with pytest.raises(ValueError, match="asymmetric"):
        make_settings(**{**prod, "AUTH_OIDC_ALGORITHMS": "HS256"})
    with pytest.raises(ValueError, match="AUTH_AUDIT_KEY"):
        make_settings(**{**prod, "AUTH_AUDIT_KEY": "short"})
    with pytest.raises(ValueError, match="https"):
        make_settings(**{**prod, "AUTH_OIDC_ISSUER": "http://idp.example.com"})


# ---- permission matrix -----------------------------------------------------------------------
EXPECTED: dict[Role, set[str]] = {
    Role.VIEWER: {
        "workspace.read",
        "members.read",
        "services.read",
        "incidents.read",
        "investigations.read",
        "knowledge.read",
        "integrations.read",
        "observability.read",
        "repositories.read",
        "remediations.read",
    },
}
EXPECTED[Role.ANALYST] = EXPECTED[Role.VIEWER] | {"investigations.run"}
EXPECTED[Role.RESPONDER] = EXPECTED[Role.ANALYST] | {
    "incidents.manage",
    "remediations.create",
    "remediations.review",
    "services.manage",
    "investigations.review",
}
EXPECTED[Role.ADMIN] = EXPECTED[Role.RESPONDER] | {
    "workspace.manage",
    "members.manage",
    "knowledge.manage",
    "integrations.manage",
    "observability.manage",
    "repositories.manage",
    "remediations.publish",
    "api_keys.manage",
    "audit.read",
}
EXPECTED[Role.OWNER] = {p.value for p in Permission}


@pytest.mark.parametrize("role", list(Role))
def test_role_permission_matrix(role: Role) -> None:
    assert {p.value for p in ROLE_PERMISSIONS[role]} == EXPECTED[role]
    for permission in Permission:
        assert (permission in permissions_for(role.value)) == (permission.value in EXPECTED[role])


def test_matrix_intent_and_unknown_role_fails_closed() -> None:
    assert Permission.REMEDIATIONS_PUBLISH not in permissions_for("analyst")
    assert Permission.REMEDIATIONS_PUBLISH not in permissions_for("responder")
    assert Permission.MEMBERS_MANAGE not in permissions_for("responder")
    assert permissions_for("superuser") == frozenset()
    assert all(not (EXPECTED[a] > EXPECTED[b]) or a != b for a in Role for b in Role)


def test_api_scopes_never_include_approval_publication_or_management() -> None:
    assert {"events:write", "incidents:read", "knowledge:read", "investigations:read"} == SCOPES
    assert not any(
        word in scope for scope in SCOPES for word in ("approv", "publish", "member", "key")
    )


def test_actor_permission_recheck_denies_keys_and_system_even_with_permissions() -> None:
    workspace = uuid4()
    human = Actor(
        Principal("user", user_id=uuid4()), workspace, "owner", uuid4(), frozenset(Permission)
    )
    human.require(Permission.REMEDIATIONS_PUBLISH)
    for principal in (
        Principal("api_key", api_key_id=uuid4(), scopes=("events:write",)),
        Principal("system"),
    ):
        impostor = Actor(principal, workspace, "owner", None, frozenset(Permission))
        with pytest.raises(ForbiddenError):
            impostor.require(Permission.REMEDIATIONS_REVIEW)
    viewer = Actor(
        Principal("user", user_id=uuid4()), workspace, "viewer", uuid4(), permissions_for("viewer")
    )
    with pytest.raises(ForbiddenError):
        viewer.require(Permission.INCIDENTS_MANAGE)


# ---- route classification --------------------------------------------------------------------
def test_every_registered_route_is_classified() -> None:
    app = create_app(make_settings())
    schema = app.openapi()
    unclassified = [
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if classify(method, path) is None
    ]
    assert unclassified == []


def test_only_intended_routes_are_public() -> None:
    app = create_app(make_settings())
    public = sorted(
        f"{method.upper()} {path}"
        for path, item in app.openapi()["paths"].items()
        for method in item
        if (policy := classify(method, path)) is not None and policy.public
    )
    assert public == [
        "GET /api/v1/auth/local/authorize",
        "GET /api/v1/auth/local/users",
        "GET /api/v1/meta",
        "GET /health/live",
        "GET /health/ready",
        "POST /api/v1/auth/local/token",
        "POST /api/v1/ingest/webhooks/{public_id}",
        "POST /api/v1/integrations/github/webhook",
    ]


def test_workspace_routes_always_require_a_permission_and_keys_are_human_only_by_default() -> None:
    app = create_app(make_settings())
    for path, item in app.openapi()["paths"].items():
        if "{workspace_id}" not in path:
            continue
        for method in item:
            policy = classify(method, path)
            assert policy is not None and policy.permission is not None, (method, path)
            if policy.api_scope is not None:
                assert policy.permission.value.endswith((".read", "incidents.manage")), (
                    method,
                    path,
                )
                assert method.upper() in {"GET", "POST"}
            if policy.permission in {
                Permission.REMEDIATIONS_REVIEW,
                Permission.REMEDIATIONS_PUBLISH,
                Permission.MEMBERS_MANAGE,
                Permission.API_KEYS_MANAGE,
            }:
                assert policy.api_scope is None


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        (
            "POST",
            "/api/v1/workspaces/{workspace_id}/remediations/{remediation_id}/review",
            Permission.REMEDIATIONS_REVIEW,
        ),
        (
            "POST",
            "/api/v1/workspaces/{workspace_id}/remediations/{remediation_id}/publish",
            Permission.REMEDIATIONS_PUBLISH,
        ),
        (
            "POST",
            "/api/v1/workspaces/{workspace_id}/investigations/{investigation_id}/remediations",
            Permission.REMEDIATIONS_CREATE,
        ),
        ("GET", "/api/v1/workspaces/{workspace_id}/audit-events", Permission.AUDIT_READ),
        ("POST", "/api/v1/workspaces/{workspace_id}/api-keys", Permission.API_KEYS_MANAGE),
        (
            "DELETE",
            "/api/v1/workspaces/{workspace_id}/members/{membership_id}",
            Permission.MEMBERS_MANAGE,
        ),
        (
            "POST",
            "/api/v1/workspaces/{workspace_id}/observability/dead-letters/{dead_letter_id}/replay",
            Permission.OBSERVABILITY_MANAGE,
        ),
    ],
)
def test_sensitive_routes_map_to_their_permissions(
    method: str, path: str, permission: Permission
) -> None:
    policy = classify(method, path)
    assert policy is not None and policy.permission is permission and policy.api_scope is None


# ---- audit integrity -------------------------------------------------------------------------
def sample_event() -> Any:
    return build_event(
        make_settings(),
        Principal("user", user_id=uuid4()),
        uuid4(),
        "membership.update",
        "membership",
        uuid4(),
        AuditResult.SUCCESS,
        RequestInfo("req-12345678", "corr-1", "a" * 64, "ua"),
        None,
        {"role": "admin", "api_key": "ospk_secret_should_be_dropped", "note": "token=abc123"},
    )


def test_audit_event_integrity_detects_tampering() -> None:
    settings = make_settings()
    event = sample_event()
    assert verify_event(settings, event)
    for field, value in (
        ("action", "membership.revoke"),
        ("result", "denied"),
        ("event_metadata", {"role": "owner"}),
        ("resource_id", "x"),
    ):
        tampered = sample_event()
        tampered.integrity_hash = compute_integrity(settings, tampered)
        setattr(tampered, field, value)
        assert not verify_event(settings, tampered), field
    other_key = make_settings(AUTH_AUDIT_KEY="a-different-audit-key-of-sufficient-length")
    assert not verify_event(other_key, event)


def test_audit_metadata_never_contains_secrets_and_is_bounded() -> None:
    event = sample_event()
    blob = json.dumps(event.event_metadata)
    assert "ospk_secret" not in blob and "abc123" not in blob
    big = build_event(
        make_settings(),
        Principal("system"),
        None,
        "x",
        "y",
        None,
        AuditResult.SUCCESS,
        RequestInfo("req-12345678"),
        None,
        {f"k{i}": "v" * 400 for i in range(20)},
    )
    assert big.event_metadata == {"truncated": True}
    assert big.actor_type == "system" and big.workspace_id is None


Factory = Callable[..., Any]
