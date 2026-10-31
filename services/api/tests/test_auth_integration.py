"""Authentication, RBAC, tenant isolation, API keys, WebSockets, audit (live PostgreSQL + Redis).

Uses the deterministic local identity provider only; no Cognito, AWS, email, or GitHub access.
"""

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis import Redis
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from opspilot_api.auth.audit import verify_event
from opspilot_api.auth.models import (
    ApiKey,
    AuditEvent,
    Role,
    User,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from opspilot_api.auth.permissions import Permission, permissions_for
from opspilot_api.auth.route_policy import classify
from opspilot_api.config import Settings
from opspilot_api.incidents.models import Workspace
from opspilot_api.incidents.service import utcnow
from opspilot_api.main import create_app

from .conftest import anonymous_client, auth_headers
from .repo_helpers import live_settings

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
        yield db
    engine.dispose()


def new_subject(label: str = "u") -> str:
    return f"{label}-{uuid4().hex[:10]}"


def headers(subject: str, email: str | None = None) -> dict[str, str]:
    return auth_headers(subject, email or f"{subject}@example.test")


def make_workspace(session: Session) -> Workspace:
    suffix = uuid4().hex[:8]
    workspace = Workspace(name=f"auth {suffix}", slug=f"auth-{suffix}")
    session.add(workspace)
    session.commit()
    return workspace


def ensure_user(session: Session, subject: str) -> User:
    user = session.scalar(
        select(User).where(User.identity_provider == "local", User.external_subject == subject)
    )
    if user is None:
        user = User(
            identity_provider="local",
            external_subject=subject,
            email=f"{subject}@example.test",
            display_name=subject,
            status="active",
        )
        session.add(user)
        session.commit()
    return user


def add_member(
    session: Session, workspace: Workspace, subject: str, role: str, status: str = "active"
) -> WorkspaceMembership:
    user = ensure_user(session, subject)
    membership = WorkspaceMembership(
        workspace_id=workspace.id, user_id=user.id, role=role, status=status
    )
    session.add(membership)
    session.commit()
    return membership


Clients = Callable[..., TestClient]


@pytest.fixture
def clients(settings: Settings) -> Iterator[Clients]:
    opened: list[TestClient] = []

    def make(app_settings: Settings | None = None) -> TestClient:
        client = anonymous_client(
            create_app(app_settings or settings), raise_server_exceptions=False
        )
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def client(clients: Clients) -> TestClient:
    return clients()


# ---- authentication -------------------------------------------------------------------------
def test_unauthenticated_requests_return_401_with_challenge(client: TestClient) -> None:
    workspace = uuid4()
    for method, path in (
        ("get", "/api/v1/workspaces"),
        ("get", f"/api/v1/workspaces/{workspace}/incidents"),
        ("post", f"/api/v1/workspaces/{workspace}/knowledge/search"),
        ("get", "/api/v1/auth/me"),
        ("get", f"/api/v1/workspaces/{workspace}/audit-events"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "unauthorized"


def test_invalid_expired_and_malformed_credentials_are_rejected(client: TestClient) -> None:
    from opspilot_api.auth.providers import issue_local_token

    path = "/api/v1/auth/me"
    assert client.get(path, headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Basic abc"}).status_code == 401
    expired = issue_local_token(live_settings(), "s", ttl_seconds=-120)
    response = client.get(path, headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401 and expired not in response.text
    both = {**headers("x"), "X-API-Key": "ospk_aaaaaaaa_bbbb"}
    assert client.get(path, headers=both).status_code == 401  # exactly one credential allowed
    assert client.get(f"{path}?access_token=abc", headers=headers("x")).status_code == 401
    assert client.get(f"{path}?api_key=ospk_x", headers=headers("x")).status_code == 401


def test_identity_is_the_subject_not_the_email(client: TestClient, session: Session) -> None:
    subject = new_subject()
    first = client.get("/api/v1/auth/me", headers=headers(subject, "old@example.test")).json()
    renamed = client.get("/api/v1/auth/me", headers=headers(subject, "new@example.test")).json()
    assert first["id"] == renamed["id"] and renamed["email"] == "new@example.test"
    other = client.get("/api/v1/auth/me", headers=headers(new_subject(), "new@example.test")).json()
    assert other["id"] != first["id"]  # same email, different subject: a different person


def test_disabled_user_loses_access_immediately(client: TestClient, session: Session) -> None:
    workspace = make_workspace(session)
    subject = new_subject()
    add_member(session, workspace, subject, "owner")
    path = f"/api/v1/workspaces/{workspace.id}"
    assert client.get(path, headers=headers(subject)).status_code == 200
    session.execute(update(User).where(User.external_subject == subject).values(status="disabled"))
    session.commit()
    assert client.get(path, headers=headers(subject)).status_code == 401


def test_workspace_creation_makes_the_creator_the_only_owner(client: TestClient) -> None:
    owner, stranger = new_subject(), new_subject()
    created = client.post(
        "/api/v1/workspaces",
        json={"name": "Mine", "slug": f"m-{uuid4().hex[:8]}"},
        headers=headers(owner),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    assert client.get(f"/api/v1/workspaces/{wid}", headers=headers(owner)).status_code == 200
    assert client.get(f"/api/v1/workspaces/{wid}", headers=headers(stranger)).status_code == 404
    listed = client.get("/api/v1/workspaces", headers=headers(stranger)).json()
    assert wid not in {item["id"] for item in listed}
    mine = client.get("/api/v1/auth/workspaces", headers=headers(owner)).json()
    assert [(m["role"], m["workspace"]["id"]) for m in mine] == [("owner", wid)]
    assert "members.manage" in mine[0]["permissions"]


# ---- RBAC matrix -----------------------------------------------------------------------------
def workspace_routes(app_client: TestClient) -> list[tuple[str, str]]:
    schema = app_client.get("/openapi.json").json()
    return [
        (method.upper(), path)
        for path, item in schema["paths"].items()
        if "{workspace_id}" in path
        for method in item
    ]


def concrete(path: str, workspace: UUID) -> str:
    out = path.replace("{workspace_id}", str(workspace))
    while "{" in out:
        start = out.index("{")
        out = out[:start] + str(uuid4()) + out[out.index("}") + 1 :]
    return out


def test_every_role_against_every_workspace_route(client: TestClient, session: Session) -> None:
    workspace = make_workspace(session)
    users: dict[str, str] = {}
    for member_role in Role:
        users[member_role.value] = new_subject(member_role.value)
        add_member(session, workspace, users[member_role.value], member_role.value)
    routes = workspace_routes(client)
    assert len(routes) > 55
    checked = 0
    for method, template in routes:
        policy = classify(method, template)
        assert policy is not None and policy.permission is not None
        for role, subject in users.items():
            response = client.request(
                method,
                concrete(template, workspace.id),
                headers=headers(subject),
                json={} if method in {"POST", "PATCH", "PUT"} else None,
            )
            allowed = policy.permission in permissions_for(role)
            if allowed:
                assert response.status_code not in {401, 403}, (role, method, template)
            else:
                assert response.status_code == 403, (role, method, template, response.status_code)
            checked += 1
    assert checked == len(routes) * 5


def test_every_workspace_route_is_401_without_credentials(client: TestClient) -> None:
    for method, template in workspace_routes(client):
        response = client.request(method, concrete(template, uuid4()), json={})
        assert response.status_code == 401, (method, template)


def test_frontend_hidden_action_is_still_denied_by_the_backend(
    client: TestClient, session: Session
) -> None:
    workspace = make_workspace(session)
    viewer = new_subject()
    add_member(session, workspace, viewer, "viewer")
    base = f"/api/v1/workspaces/{workspace.id}"
    assert client.get(f"{base}/incidents", headers=headers(viewer)).status_code == 200
    for method, path in (
        ("post", f"{base}/incidents"),
        ("post", f"{base}/services"),
        ("post", f"{base}/api-keys"),
        ("post", f"{base}/invitations"),
    ):
        response = getattr(client, method)(path, headers=headers(viewer), json={"title": "x"})
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "forbidden"


def test_role_change_suspension_and_revocation_take_effect_on_the_next_request(
    client: TestClient, session: Session
) -> None:
    workspace = make_workspace(session)
    owner, member = new_subject("owner"), new_subject("member")
    add_member(session, workspace, owner, "owner")
    membership = add_member(session, workspace, member, "viewer")
    base = f"/api/v1/workspaces/{workspace.id}"
    assert (
        client.post(
            f"{base}/services",
            headers=headers(member),
            json={"name": "s", "environment": "production"},
        ).status_code
        == 403
    )
    promoted = client.patch(
        f"{base}/members/{membership.id}", headers=headers(owner), json={"role": "responder"}
    )
    assert promoted.status_code == 200 and promoted.json()["role"] == "responder"
    assert (
        client.post(
            f"{base}/services",
            headers=headers(member),
            json={"name": f"s-{uuid4().hex[:6]}", "environment": "production"},
        ).status_code
        == 201
    )
    client.patch(
        f"{base}/members/{membership.id}", headers=headers(owner), json={"status": "suspended"}
    )
    assert client.get(f"{base}/incidents", headers=headers(member)).status_code == 404
    client.patch(
        f"{base}/members/{membership.id}", headers=headers(owner), json={"status": "active"}
    )
    assert client.get(f"{base}/incidents", headers=headers(member)).status_code == 200
    assert (
        client.delete(f"{base}/members/{membership.id}", headers=headers(owner)).status_code == 204
    )
    assert client.get(f"{base}/incidents", headers=headers(member)).status_code == 404
    assert (
        client.patch(
            f"{base}/members/{membership.id}", headers=headers(owner), json={"role": "admin"}
        ).status_code
        == 409
    )  # revoked memberships are frozen


def test_final_owner_cannot_be_removed_demoted_or_suspended(
    client: TestClient, session: Session
) -> None:
    workspace = make_workspace(session)
    owner = new_subject("owner")
    mine = add_member(session, workspace, owner, "owner")
    base = f"/api/v1/workspaces/{workspace.id}/members/{mine.id}"
    for response in (
        client.patch(base, headers=headers(owner), json={"role": "admin"}),
        client.patch(base, headers=headers(owner), json={"status": "suspended"}),
        client.delete(base, headers=headers(owner)),
    ):
        assert response.status_code == 409 and "at least one active owner" in response.text
    second = add_member(session, workspace, new_subject("co"), "owner")
    assert client.patch(base, headers=headers(owner), json={"role": "admin"}).status_code == 200
    # the remaining owner is now the final one
    remaining = f"/api/v1/workspaces/{workspace.id}/members/{second.id}"
    last_owner = session.get(User, second.user_id)
    assert last_owner is not None
    assert client.delete(remaining, headers=headers(last_owner.external_subject)).status_code == 409


def test_admin_cannot_touch_owners_or_grant_ownership(client: TestClient, session: Session) -> None:
    workspace = make_workspace(session)
    owner, admin = new_subject("owner"), new_subject("admin")
    owner_membership = add_member(session, workspace, owner, "owner")
    add_member(session, workspace, new_subject("owner2"), "owner")
    add_member(session, workspace, admin, "admin")
    target = add_member(session, workspace, new_subject("v"), "viewer")
    base = f"/api/v1/workspaces/{workspace.id}"
    assert (
        client.patch(
            f"{base}/members/{owner_membership.id}", headers=headers(admin), json={"role": "viewer"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"{base}/members/{owner_membership.id}", headers=headers(admin)).status_code
        == 403
    )
    assert (
        client.patch(
            f"{base}/members/{target.id}", headers=headers(admin), json={"role": "owner"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{base}/invitations",
            headers=headers(admin),
            json={"email": "x@example.test", "role": "owner"},
        ).status_code
        == 403
    )


def test_domain_services_recheck_authorization_without_the_route_guard(session: Session) -> None:
    import asyncio

    from opspilot_api.auth.context import Actor, ForbiddenError, Principal, RequestInfo
    from opspilot_api.auth.service import create_api_key, update_member
    from opspilot_api.infrastructure import create_db_engine, create_session_factory

    settings = live_settings()
    workspace = make_workspace(session)
    viewer = ensure_user(session, new_subject())
    membership = add_member(session, workspace, viewer.external_subject, "viewer")
    actor = Actor(
        Principal("user", user_id=viewer.id),
        workspace.id,
        "viewer",
        membership.id,
        permissions_for("viewer"),
    )
    key_actor = Actor(
        Principal("api_key", api_key_id=uuid4(), scopes=("events:write",)),
        workspace.id,
        None,
        None,
        frozenset(Permission),
    )
    request = RequestInfo("req-test-1234")

    async def run() -> None:
        engine = create_db_engine(settings)
        try:
            async with create_session_factory(engine)() as db:
                for who in (actor, key_actor):
                    with pytest.raises(ForbiddenError):
                        await update_member(
                            db, settings, who, membership.id, "owner", None, request
                        )
                    with pytest.raises(ForbiddenError):
                        await create_api_key(
                            db, settings, who, "k", ["events:write"], None, request
                        )
        finally:
            await engine.dispose()

    asyncio.run(run())


# ---- tenant isolation ------------------------------------------------------------------------
def test_cross_workspace_access_is_indistinguishable_from_nonexistence(
    client: TestClient, session: Session
) -> None:
    wa, wb = make_workspace(session), make_workspace(session)
    a_user, b_user, nobody = new_subject("a"), new_subject("b"), new_subject("n")
    add_member(session, wa, a_user, "owner")
    add_member(session, wb, b_user, "owner")
    a = f"/api/v1/workspaces/{wa.id}"
    service = client.post(
        f"{a}/services", headers=headers(a_user), json={"name": "svc", "environment": "production"}
    ).json()
    incident = client.post(
        f"{a}/incidents",
        headers=headers(a_user),
        json={"title": "secret incident", "severity": "sev2", "service_id": service["id"]},
    ).json()
    # Wrong tenant, existing resource -> identical 404 to a fully random id.
    paths = [
        f"{a}/incidents/{incident['id']}",
        f"{a}/incidents/{incident['id']}/timeline",
        f"{a}/services/{service['id']}",
        f"{a}/incidents",
        f"{a}/services",
        f"{a}/members",
        f"{a}/remediations",
        f"{a}/audit-events",
        f"{a}/api-keys",
        f"{a}/knowledge/documents",
        f"{a}/repositories",
        f"{a}/observability/signals",
        f"{a}",
    ]
    for path in paths:
        for who in (b_user, nobody):
            denied = client.get(path, headers=headers(who))
            assert denied.status_code == 404, path
            assert incident["id"] not in denied.text and "secret incident" not in denied.text
    # Same UUID-shaped id used inside the caller's own workspace does not resolve either.
    b = f"/api/v1/workspaces/{wb.id}"
    own = client.get(f"{b}/incidents/{incident['id']}", headers=headers(b_user))
    random = client.get(f"{b}/incidents/{uuid4()}", headers=headers(b_user))
    assert own.status_code == random.status_code == 404
    assert own.json()["error"]["code"] == random.json()["error"]["code"]
    assert client.get(f"{b}/incidents", headers=headers(b_user)).json()["items"] == []
    assert client.get(f"{b}/services", headers=headers(b_user)).json() == []


def test_investigation_evidence_repository_and_remediation_isolation(
    clients: Clients, session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opspilot_api.remediation import router as remediation_router

    from .remediation_helpers import build_scenario

    for name in ("enqueue_remediation", "enqueue_remediation_resume", "enqueue_publish"):
        monkeypatch.setattr(remediation_router, name, lambda *a, **k: None)
    scenario = build_scenario(session, settings)
    owner = new_subject("o")
    add_member(session, scenario.workspace, owner, "owner")
    outsider = new_subject("x")
    other = make_workspace(session)
    add_member(session, other, outsider, "owner")
    app = create_app(settings)
    app.dependency_overrides[remediation_router.get_repo_provider] = lambda: scenario.provider
    with anonymous_client(app, raise_server_exceptions=False) as client:
        wa = f"/api/v1/workspaces/{scenario.workspace.id}"
        created = client.post(
            f"{wa}/investigations/{scenario.investigation.id}/remediations", headers=headers(owner)
        )
        assert created.status_code == 202, created.text
        remediation = created.json()["remediation_id"]
        resources = [
            f"{wa}/investigations/{scenario.investigation.id}",
            f"{wa}/repositories/{scenario.repository.id}",
            f"{wa}/remediations/{remediation}",
            f"{wa}/remediations/{remediation}/verification",
            f"{wa}/remediations/{remediation}/sandbox-runs",
            f"{wa}/remediations/{remediation}/pull-request",
        ]
        for path in resources:
            assert client.get(path, headers=headers(owner)).status_code in {200, 404}
            assert client.get(path, headers=headers(outsider)).status_code == 404, path
        # Nested resource under the *attacker's own* workspace id: still not found.
        wo = f"/api/v1/workspaces/{other.id}"
        for tail in (
            f"investigations/{scenario.investigation.id}",
            f"remediations/{remediation}",
            f"repositories/{scenario.repository.id}",
        ):
            assert client.get(f"{wo}/{tail}", headers=headers(outsider)).status_code == 404
        for action in ("cancel", "publish", "retry"):
            assert (
                client.post(
                    f"{wo}/remediations/{remediation}/{action}", headers=headers(outsider)
                ).status_code
                == 404
            )
        review = client.post(
            f"{wo}/remediations/{remediation}/review",
            headers=headers(outsider),
            json={"action": "approved", "comment": "x", "revision_id": str(uuid4()), "version": 1},
        )
        assert review.status_code == 404
        assert (
            client.post(
                f"{wo}/investigations/{scenario.investigation.id}/remediations",
                headers=headers(outsider),
            ).status_code
            == 404
        )
        assert client.get(f"{wo}/remediations", headers=headers(outsider)).json() == []


# ---- invitations -----------------------------------------------------------------------------
def test_invitation_lifecycle_is_single_use_expiring_and_bound_to_the_verified_email(
    client: TestClient, session: Session
) -> None:
    workspace = make_workspace(session)
    admin = new_subject("admin")
    add_member(session, workspace, admin, "admin")
    base = f"/api/v1/workspaces/{workspace.id}"
    invitee = new_subject("invitee")
    email = f"{invitee}@example.test"
    created = client.post(
        f"{base}/invitations",
        headers=headers(admin),
        json={"email": email.upper(), "role": "analyst"},
    )
    assert created.status_code == 201
    token = created.json()["token"]
    stored = session.scalar(
        select(WorkspaceInvitation).where(WorkspaceInvitation.id == UUID(created.json()["id"]))
    )
    assert stored is not None and stored.email == email  # normalized
    assert token not in stored.token_hash and len(stored.token_hash) == 64
    listed = client.get(f"{base}/invitations", headers=headers(admin)).json()
    assert "token" not in json.dumps(listed) and token not in json.dumps(listed)

    wrong = client.post(
        "/api/v1/invitations/accept", headers=headers(new_subject("evil")), json={"token": token}
    )
    unknown = client.post(
        "/api/v1/invitations/accept", headers=headers(invitee), json={"token": "x" * 43}
    )
    unverified = client.post(
        "/api/v1/invitations/accept",
        headers=auth_headers(invitee, email) | {"Authorization": _unverified(invitee, email)},
        json={"token": token},
    )
    assert {wrong.status_code, unknown.status_code, unverified.status_code} == {404}
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]  # no oracle

    accepted = client.post(
        "/api/v1/invitations/accept", headers=headers(invitee, email), json={"token": token}
    )
    assert accepted.status_code == 200 and accepted.json()["role"] == "analyst"
    assert client.get(f"{base}/incidents", headers=headers(invitee)).status_code == 200
    assert (
        client.post(
            f"{base}/services",
            headers=headers(invitee),
            json={"name": "x", "environment": "production"},
        ).status_code
        == 403
    )
    again = client.post(
        "/api/v1/invitations/accept", headers=headers(invitee, email), json={"token": token}
    )
    assert again.status_code == 404  # single use

    second = client.post(
        f"{base}/invitations",
        headers=headers(admin),
        json={"email": "late@example.test", "role": "viewer"},
    ).json()
    session.execute(
        update(WorkspaceInvitation)
        .where(WorkspaceInvitation.id == UUID(second["id"]))
        .values(expires_at=utcnow() - timedelta(minutes=1))
    )
    session.commit()
    late = new_subject("late")
    assert (
        client.post(
            "/api/v1/invitations/accept",
            headers=headers(late, "late@example.test"),
            json={"token": second["token"]},
        ).status_code
        == 404
    )
    third = client.post(
        f"{base}/invitations",
        headers=headers(admin),
        json={"email": "rev@example.test", "role": "viewer"},
    ).json()
    assert (
        client.post(f"{base}/invitations/{third['id']}/revoke", headers=headers(admin)).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/invitations/accept",
            headers=headers(new_subject("r"), "rev@example.test"),
            json={"token": third["token"]},
        ).status_code
        == 404
    )


def _unverified(subject: str, email: str) -> str:
    from opspilot_api.auth.providers import issue_local_token

    token = issue_local_token(live_settings(), subject, email, subject, email_verified=False)
    return f"Bearer {token}"


# ---- API keys --------------------------------------------------------------------------------
def create_key(
    client: TestClient, owner: str, workspace: Workspace, scopes: list[str]
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/api-keys",
        headers=headers(owner),
        json={"name": "ci", "scopes": scopes},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


def test_api_key_is_shown_once_stored_only_as_a_hash_and_never_logged(
    client: TestClient, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    workspace = make_workspace(session)
    owner = new_subject()
    add_member(session, workspace, owner, "owner")
    created = create_key(client, owner, workspace, ["events:write", "incidents:read"])
    full = created["key"]
    assert full.startswith("ospk_") and len(full) > 40
    listed = client.get(f"/api/v1/workspaces/{workspace.id}/api-keys", headers=headers(owner))
    assert full not in listed.text and "key_hash" not in listed.text and '"key"' not in listed.text
    client.get(f"/api/v1/workspaces/{workspace.id}/incidents", headers={"X-API-Key": full})
    row = session.scalar(select(ApiKey).where(ApiKey.id == UUID(created["id"])))
    assert row is not None and full not in (row.key_hash, row.key_prefix, row.name)
    assert len(row.key_hash) == 64 and row.key_prefix == "_".join(full.split("_", 2)[:2])
    for table, column in (("api_keys", "key_hash"), ("audit_events", "metadata")):
        dump = session.execute(text(f"SELECT {column}::text FROM {table}")).scalars().all()
        assert not any(full in str(item) for item in dump), table
    assert full not in caplog.text and full.split("_", 2)[2] not in caplog.text


def test_api_key_scopes_workspace_binding_and_human_only_endpoints(
    client: TestClient, session: Session
) -> None:
    wa, wb = make_workspace(session), make_workspace(session)
    owner_a, owner_b = new_subject("a"), new_subject("b")
    add_member(session, wa, owner_a, "owner")
    add_member(session, wb, owner_b, "owner")
    reader = create_key(client, owner_a, wa, ["incidents:read"])["key"]
    writer = create_key(client, owner_a, wa, ["events:write"])["key"]
    base = f"/api/v1/workspaces/{wa.id}"
    assert client.get(f"{base}/incidents", headers={"X-API-Key": reader}).status_code == 200
    assert (
        client.get(f"{base}/incidents", headers={"X-API-Key": writer}).status_code == 403
    )  # scope
    assert client.post(f"{base}/events", headers={"X-API-Key": writer}, json={}).status_code == 422
    assert client.post(f"{base}/events", headers={"X-API-Key": reader}, json={}).status_code == 403
    assert (
        client.get(
            f"/api/v1/workspaces/{wb.id}/incidents", headers={"X-API-Key": reader}
        ).status_code
        == 404
    )
    human_only = [
        ("get", f"{base}/members"),
        ("get", f"{base}/api-keys"),
        ("get", f"{base}/audit-events"),
        ("post", f"{base}/api-keys"),
        ("post", f"{base}/invitations"),
        ("post", f"{base}/remediations/{uuid4()}/review"),
        ("post", f"{base}/remediations/{uuid4()}/publish"),
        ("post", f"{base}/investigations/{uuid4()}/review"),
        ("post", f"{base}/investigations/{uuid4()}/remediations"),
        ("post", f"{base}/incidents"),
        ("get", "/api/v1/auth/workspaces"),
    ]
    for method, path in human_only:
        for key in (reader, writer):
            response = client.request(method.upper(), path, headers={"X-API-Key": key}, json={})
            assert response.status_code == 403, (method, path)
    assert (
        client.post(
            f"{base}/api-keys",
            headers=headers(owner_a),
            json={"name": "bad", "scopes": ["remediations:publish"]},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{base}/api-keys", headers=headers(owner_a), json={"name": "bad", "scopes": []}
        ).status_code
        == 422
    )


def test_api_key_rotation_revocation_and_expiry(client: TestClient, session: Session) -> None:
    workspace = make_workspace(session)
    owner = new_subject()
    add_member(session, workspace, owner, "owner")
    base = f"/api/v1/workspaces/{workspace.id}"
    created = create_key(client, owner, workspace, ["incidents:read"])
    key = created["key"]
    assert client.get(f"{base}/incidents", headers={"X-API-Key": key}).status_code == 200
    rotated = client.post(f"{base}/api-keys/{created['id']}/rotate", headers=headers(owner)).json()
    assert rotated["key"] != key
    assert client.get(f"{base}/incidents", headers={"X-API-Key": key}).status_code == 401
    assert client.get(f"{base}/incidents", headers={"X-API-Key": rotated["key"]}).status_code == 200
    revoked = client.post(f"{base}/api-keys/{rotated['id']}/revoke", headers=headers(owner))
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert client.get(f"{base}/incidents", headers={"X-API-Key": rotated["key"]}).status_code == 401
    expiring = create_key(client, owner, workspace, ["incidents:read"])
    session.execute(
        update(ApiKey)
        .where(ApiKey.id == UUID(expiring["id"]))
        .values(expires_at=utcnow() - timedelta(minutes=1))
    )
    session.commit()
    assert (
        client.get(f"{base}/incidents", headers={"X-API-Key": expiring["key"]}).status_code == 401
    )
    forged = expiring["key"][:-4] + "AAAA"
    assert client.get(f"{base}/incidents", headers={"X-API-Key": forged}).status_code == 401
    assert (
        client.post(f"{base}/api-keys/{uuid4()}/revoke", headers=headers(owner)).status_code == 404
    )


def test_api_key_requests_are_rate_limited_with_retry_after(
    clients: Clients, session: Session, settings: Settings
) -> None:
    tight = settings.model_copy(update={"AUTH_RATE_LIMIT_MULTIPLIER": 0.01})
    client = clients(tight)
    workspace = make_workspace(session)
    owner = new_subject()
    add_member(session, workspace, owner, "owner")
    key = create_key(client, owner, workspace, ["incidents:read"])["key"]
    statuses = [
        client.get(f"/api/v1/workspaces/{workspace.id}/incidents", headers={"X-API-Key": key})
        for _ in range(4)
    ]
    assert statuses[0].status_code == 200
    limited = next(r for r in statuses if r.status_code == 429)
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["error"]["code"] == "rate_limited" and "opspilot:rl" not in limited.text


# ---- rate limiting ---------------------------------------------------------------------------
def test_expensive_operations_are_rate_limited_per_identity(
    clients: Clients, session: Session, settings: Settings
) -> None:
    tight = settings.model_copy(update={"AUTH_RATE_LIMIT_MULTIPLIER": 0.01})
    client = clients(tight)
    workspace = make_workspace(session)
    owner = new_subject()
    add_member(session, workspace, owner, "owner")
    base = f"/api/v1/workspaces/{workspace.id}"
    codes = [
        client.post(
            f"{base}/knowledge/search", headers=headers(owner), json={"query": "latency spike"}
        ).status_code
        for _ in range(4)
    ]
    assert 429 in codes and codes[0] != 429
    tickets = [
        client.post(
            "/api/v1/auth/ws-ticket",
            headers=headers(owner),
            json={"workspace_id": str(workspace.id)},
        ).status_code
        for _ in range(5)
    ]
    assert 429 in tickets
    other = new_subject()
    add_member(session, workspace, other, "owner")
    assert (
        client.post(
            f"{base}/knowledge/search", headers=headers(other), json={"query": "latency spike"}
        ).status_code
        != 429
    )  # buckets are per identity


def test_repeated_authentication_failures_are_throttled(
    clients: Clients, settings: Settings
) -> None:
    client = clients(settings.model_copy(update={"AUTH_RATE_LIMIT_MULTIPLIER": 0.05}))
    codes = [
        client.get("/api/v1/auth/me", headers={"Authorization": "Bearer bad"}).status_code
        for _ in range(8)
    ]
    assert codes[0] == 401 and 429 in codes


def test_rate_limiter_fails_closed_for_high_risk_and_open_for_reads() -> None:
    import asyncio

    from redis.asyncio import Redis as AsyncRedis

    from opspilot_api.auth.context import RateLimitedError
    from opspilot_api.auth.ratelimit import RateLimiter

    dead = AsyncRedis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    limiter = RateLimiter(dead, live_settings())

    async def run() -> None:
        with pytest.raises(RateLimitedError):
            await limiter.check("approval_attempt", "user:1")
        await limiter.check("knowledge_search", "user:1")  # read-like: availability wins
        await dead.aclose()

    asyncio.run(run())


# ---- WebSockets ------------------------------------------------------------------------------
def ticket_for(
    client: TestClient, subject: str, workspace: Workspace, channel: str = "incidents"
) -> str:
    response = client.post(
        "/api/v1/auth/ws-ticket",
        headers=headers(subject),
        json={"workspace_id": str(workspace.id), "channel": channel},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["ticket"])


def ws_url(workspace: Workspace, ticket: str) -> str:
    return f"/api/v1/ws/workspaces/{workspace.id}/incidents?ticket={ticket}"


def test_websocket_ticket_lifecycle(
    client: TestClient, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    wa, wb = make_workspace(session), make_workspace(session)
    user, outsider = new_subject("u"), new_subject("o")
    add_member(session, wa, user, "viewer")
    add_member(session, wb, outsider, "viewer")
    ticket = ticket_for(client, user, wa)
    with client.websocket_connect(ws_url(wa, ticket)) as socket:
        assert socket.receive_json()["type"] == "connection.established"
    with pytest.raises(WebSocketDisconnect) as reused:  # single use
        with client.websocket_connect(ws_url(wa, ticket)):
            pass
    assert reused.value.code == 4401
    fresh = ticket_for(client, user, wa)  # reconnect = new ticket
    assert fresh != ticket
    with client.websocket_connect(ws_url(wa, fresh)) as socket:
        assert socket.receive_json()["type"] == "connection.established"
    # Wrong workspace: a ticket minted for A is useless on B (and is consumed).
    stolen = ticket_for(client, user, wa)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(ws_url(wb, stolen)):
        pass
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(ws_url(wa, stolen)):
        pass
    # Not a member: no ticket at all, and a forged ticket is rejected.
    assert (
        client.post(
            "/api/v1/auth/ws-ticket", headers=headers(outsider), json={"workspace_id": str(wa.id)}
        ).status_code
        == 404
    )
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(ws_url(wa, "forged-ticket")):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/ws/workspaces/{wa.id}/incidents"):
            pass
    # Wrong channel is refused at issuance; credentials of other kinds are not tickets.
    assert (
        client.post(
            "/api/v1/auth/ws-ticket",
            headers=headers(user),
            json={"workspace_id": str(wa.id), "channel": "admin"},
        ).status_code
        == 422
    )
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(ws_url(wa, headers(user)["Authorization"].split()[1])):
            pass
    assert ticket not in caplog.text and fresh not in caplog.text


def test_websocket_ticket_expires(clients: Clients, session: Session, settings: Settings) -> None:
    client = clients()
    workspace = make_workspace(session)
    user = new_subject()
    add_member(session, workspace, user, "viewer")
    ticket = ticket_for(client, user, workspace)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value())
    keys = [k for k in redis.scan_iter("opspilot:wst:*")]
    assert keys and all(redis.ttl(k) <= settings.AUTH_WS_TICKET_TTL_SECONDS for k in keys)
    from opspilot_api.auth.tickets import ticket_key

    redis.delete(ticket_key(ticket))  # simulate TTL expiry
    redis.close()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(ws_url(workspace, ticket)):
            pass


def test_websocket_stops_after_membership_revocation(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opspilot_api.incidents import router as incidents_router

    monkeypatch.setattr(incidents_router, "MEMBERSHIP_RECHECK_SECONDS", 0.2)
    workspace = make_workspace(session)
    user = new_subject()
    membership = add_member(session, workspace, user, "viewer")
    with client.websocket_connect(ws_url(workspace, ticket_for(client, user, workspace))) as socket:
        assert socket.receive_json()["type"] == "connection.established"
        session.execute(
            update(WorkspaceMembership)
            .where(WorkspaceMembership.id == membership.id)
            .values(status="revoked")
        )
        session.commit()
        deadline = time.time() + 10
        closed: int | None = None
        while time.time() < deadline:
            try:
                socket.receive_text()
            except WebSocketDisconnect as exc:
                closed = exc.code
                break
        assert closed == 4403
    assert (
        client.post(
            "/api/v1/auth/ws-ticket",
            headers=headers(user),
            json={"workspace_id": str(workspace.id)},
        ).status_code
        == 404
    )


def test_websocket_only_forwards_its_own_workspace_events(
    client: TestClient, session: Session, settings: Settings
) -> None:
    from opspilot_api.incidents.schemas import RealtimeEnvelope

    wa, wb = make_workspace(session), make_workspace(session)
    user = new_subject()
    add_member(session, wa, user, "viewer")
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value())
    try:
        with client.websocket_connect(ws_url(wa, ticket_for(client, user, wa))) as socket:
            socket.receive_json()
            for workspace_id, label in ((wb.id, "foreign"), (wa.id, "own")):
                envelope = RealtimeEnvelope(
                    event_id=uuid4(),
                    type=f"incident.{label}",
                    workspace_id=workspace_id,
                    incident_id=None,
                    occurred_at=utcnow(),
                    data={},
                )
                redis.publish(f"opspilot:workspace:{wa.id}:incidents", envelope.model_dump_json())
            assert (
                socket.receive_json()["type"] == "incident.own"
            )  # the foreign event never arrives
    finally:
        redis.close()


# ---- audit -----------------------------------------------------------------------------------
def audit_rows(session: Session, workspace: Workspace) -> list[AuditEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.workspace_id == workspace.id)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )


def test_sensitive_actions_and_denials_are_audited_without_secrets(
    client: TestClient, session: Session, settings: Settings
) -> None:
    workspace = make_workspace(session)
    owner, viewer = new_subject("o"), new_subject("v")
    add_member(session, workspace, owner, "owner")
    member = add_member(session, workspace, viewer, "viewer")
    base = f"/api/v1/workspaces/{workspace.id}"
    key = create_key(client, owner, workspace, ["incidents:read"])
    invitation = client.post(
        f"{base}/invitations",
        headers=headers(owner),
        json={"email": "audit@example.test", "role": "viewer"},
    ).json()
    client.patch(f"{base}/members/{member.id}", headers=headers(owner), json={"role": "analyst"})
    client.post(f"{base}/api-keys/{key['id']}/revoke", headers=headers(owner))
    client.post(
        f"{base}/services", headers=headers(viewer), json={"name": "n", "environment": "production"}
    )
    client.delete(f"{base}/members/{member.id}", headers=headers(owner))
    rows = audit_rows(session, workspace)
    actions = {(r.action, r.result) for r in rows}
    for expected in (
        ("api_key.create", "success"),
        ("invitation.create", "success"),
        ("membership.update", "success"),
        ("api_key.revoke", "success"),
        ("membership.revoke", "success"),
        ("authz.denied", "denied"),
    ):
        assert expected in actions, expected
    denied = next(r for r in rows if r.action == "authz.denied")
    assert denied.reason_code == "permission_missing" and denied.actor_user_id is not None
    blob = json.dumps([r.event_metadata for r in rows]) + " ".join(r.request_id for r in rows)
    for secret in (
        key["key"],
        invitation["token"],
        "audit@example.test",
        headers(owner)["Authorization"].split()[1],
    ):
        assert secret not in blob
    assert all(r.source_ip_hash is None or len(r.source_ip_hash) == 64 for r in rows)
    assert all(verify_event(settings, r) for r in rows)


def test_audit_log_is_append_only_and_integrity_verifiable(
    client: TestClient, session: Session, settings: Settings
) -> None:
    import asyncio

    from opspilot_api.auth.audit_verify import verify_events
    from opspilot_api.infrastructure import create_db_engine, create_session_factory

    workspace = make_workspace(session)
    owner = new_subject()
    add_member(session, workspace, owner, "owner")
    create_key(client, owner, workspace, ["incidents:read"])
    row = audit_rows(session, workspace)[0]
    original_reason = row.reason_code
    for statement in (
        f"UPDATE audit_events SET result = 'denied' WHERE id = '{row.id}'",
        f"DELETE FROM audit_events WHERE id = '{row.id}'",
        "DELETE FROM audit_events",
    ):
        with pytest.raises(DBAPIError):
            session.execute(text(statement))
        session.rollback()

    async def check() -> tuple[int, list[UUID]]:
        engine = create_db_engine(settings)
        try:
            async with create_session_factory(engine)() as db:
                return await verify_events(db, settings, workspace.id)
        finally:
            await engine.dispose()

    checked, bad = asyncio.run(check())
    assert checked >= 1 and bad == []
    with session.connection() as connection:  # tamper "outside the application"
        connection.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        connection.execute(
            text("UPDATE audit_events SET reason_code = 'tampered' WHERE id = :id"), {"id": row.id}
        )
        connection.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))
        session.commit()
    checked, bad = asyncio.run(check())
    assert bad == [row.id]
    listing = client.get(
        f"/api/v1/workspaces/{workspace.id}/audit-events", headers=headers(owner)
    ).json()
    assert [e["integrity_valid"] for e in listing["items"] if e["id"] == str(row.id)] == [False]
    with session.connection() as connection:  # restore, so the database-wide verification passes
        connection.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        connection.execute(
            text("UPDATE audit_events SET reason_code = :original WHERE id = :id"),
            {"original": original_reason, "id": row.id},
        )
        connection.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))
        session.commit()
    checked, bad = asyncio.run(check())
    assert bad == []


def test_audit_listing_filters_paginates_and_is_workspace_scoped(
    client: TestClient, session: Session
) -> None:
    wa, wb = make_workspace(session), make_workspace(session)
    owner_a, owner_b = new_subject("a"), new_subject("b")
    add_member(session, wa, owner_a, "owner")
    add_member(session, wb, owner_b, "owner")
    for _ in range(3):
        create_key(client, owner_a, wa, ["incidents:read"])
    create_key(client, owner_b, wb, ["incidents:read"])
    base = f"/api/v1/workspaces/{wa.id}/audit-events"
    first = client.get(
        base, params={"limit": 2, "action": "api_key.create"}, headers=headers(owner_a)
    ).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get(
        base,
        params={"limit": 2, "action": "api_key.create", "cursor": first["next_cursor"]},
        headers=headers(owner_a),
    ).json()
    ids = {e["id"] for e in first["items"]} | {e["id"] for e in second["items"]}
    assert len(ids) == 3 and second["next_cursor"] is None
    assert all(e["workspace_id"] == str(wa.id) for e in first["items"] + second["items"])
    assert (
        client.get(base, params={"result": "denied"}, headers=headers(owner_a)).json()["items"]
        == []
    )
    assert (
        client.get(base, params={"cursor": "garbage"}, headers=headers(owner_a)).status_code == 422
    )
    assert client.get(base, headers=headers(owner_b)).status_code == 404
    for method in ("put", "patch", "delete", "post"):
        assert getattr(client, method)(base, headers=headers(owner_a)).status_code == 405


def test_high_risk_actions_fail_closed_when_auditing_fails(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opspilot_api.auth import service as auth_service
    from opspilot_api.auth.context import AuditUnavailableError

    workspace = make_workspace(session)
    owner = new_subject()
    mine = add_member(session, workspace, owner, "owner")
    target = add_member(session, workspace, new_subject("t"), "viewer")
    base = f"/api/v1/workspaces/{workspace.id}"

    async def broken_record(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("fail_closed"):
            raise AuditUnavailableError("audit log unavailable; action refused")

    monkeypatch.setattr("opspilot_api.auth.dependencies.record_audit", broken_record)
    # Route-level pre-write audit (remediation / investigation / replay actions).
    for path in (
        f"{base}/remediations/{uuid4()}/publish",
        f"{base}/investigations/{uuid4()}/review",
        f"{base}/observability/dead-letters/{uuid4()}/replay",
    ):
        response = client.post(
            path, headers=headers(owner), json={"action": "approved", "comment": "x", "version": 1}
        )
        assert response.status_code == 503, path
        assert response.json()["error"]["code"] == "audit_unavailable"
    monkeypatch.undo()

    # Service-level atomic audit (membership, API keys): no audit row means no change.
    def failing_stage(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(auth_service, "stage_audit", failing_stage)
    changed = client.patch(
        f"{base}/members/{target.id}", headers=headers(owner), json={"role": "admin"}
    )
    assert changed.status_code == 500
    created = client.post(
        f"{base}/api-keys", headers=headers(owner), json={"name": "k", "scopes": ["incidents:read"]}
    )
    assert created.status_code == 500
    monkeypatch.undo()
    session.expire_all()
    assert session.get(WorkspaceMembership, target.id).role == "viewer"  # type: ignore[union-attr]
    assert (
        session.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.workspace_id == workspace.id)
        )
        == 0
    )
    assert mine.role == "owner"


# ---- local bootstrap -------------------------------------------------------------------------
def test_dev_bootstrap_is_idempotent_and_refused_in_production(
    session: Session, settings: Settings
) -> None:
    import asyncio

    from opspilot_api.auth.bootstrap_dev import bootstrap
    from opspilot_api.config import Environment
    from opspilot_api.infrastructure import create_db_engine, create_session_factory

    workspace = make_workspace(session)

    async def run(app_settings: Settings) -> dict[str, int]:
        engine = create_db_engine(app_settings)
        try:
            async with create_session_factory(engine)() as db:
                return await bootstrap(db, app_settings)
        finally:
            await engine.dispose()

    first = asyncio.run(run(settings))
    second = asyncio.run(run(settings))
    assert second["memberships_created"] == 0 and first["users"] == 5
    owners = session.scalar(
        select(func.count())
        .select_from(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace.id, WorkspaceMembership.role == "owner"
        )
    )
    assert owners == 1
    with pytest.raises(RuntimeError):
        asyncio.run(run(settings.model_copy(update={"APP_ENV": Environment.PRODUCTION})))


def test_login_establishment_and_rate_limit_denials_are_audited(
    clients: Clients, session: Session, settings: Settings
) -> None:
    client = clients(settings.model_copy(update={"AUTH_RATE_LIMIT_MULTIPLIER": 0.01}))
    workspace = make_workspace(session)
    owner = new_subject()
    add_member(session, workspace, owner, "owner")
    first = client.get("/api/v1/auth/me", headers=headers(owner))
    assert first.status_code == 200
    user = ensure_user(session, owner)
    session.expire_all()
    logins = session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "auth.login", AuditEvent.actor_user_id == user.id
        )
    ).all()
    assert len(logins) == 1 and logins[0].workspace_id is None
    client.get("/api/v1/auth/me", headers=headers(owner))
    session.expire_all()
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "auth.login", AuditEvent.actor_user_id == user.id)
        )
        == 1
    )  # only session establishment, not every request
    base = f"/api/v1/workspaces/{workspace.id}"
    for _ in range(4):
        client.post(f"{base}/knowledge/search", headers=headers(owner), json={"query": "latency"})
    rows = audit_rows(session, workspace)
    denied = [r for r in rows if r.action == "ratelimit.denied"]
    assert (
        denied
        and denied[0].result == "denied"
        and denied[0].event_metadata == {"rule": "knowledge_search"}
    )
    assert all(verify_event(settings, r) for r in rows)
