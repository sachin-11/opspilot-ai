"""Auth, RBAC, API-key, and rate-limit behavior against live PostgreSQL + Redis."""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from opspilot_api.config import get_settings
from opspilot_api.main import create_app

from .test_integration import (
    TEST_USER_PASSWORD,
    create_service,
    create_workspace,
    live_client,  # noqa: F401 - pytest fixture
    random_client_ip,
    signup_and_login,
)

pytestmark = pytest.mark.integration


def event_body(service_id: object) -> dict[str, object]:
    return {
        "service_id": service_id,
        "source": "test-monitor",
        "event_type": "http_error_rate",
        "severity": "sev2",
        "summary": "5xx rate exceeded threshold",
        "error_signature": "checkout.5xx",
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def add_member(client: TestClient, workspace_id: str, email: str, role: str) -> None:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/members", json={"email": email, "role": role}
    )
    assert response.status_code == 200, response.text


def new_user(client: TestClient, prefix: str) -> tuple[str, dict[str, str]]:
    """Return (email, auth header) for a fresh user, without disturbing the client defaults."""
    headers = signup_and_login(client, prefix)
    me = client.get("/api/v1/auth/me", headers=headers).json()
    return str(me["user"]["email"]), headers


def test_signup_login_me_and_duplicate_email(live_client: TestClient) -> None:  # noqa: F811
    email = f"dup-{uuid4().hex[:8]}@example.com"
    body = {"email": email, "display_name": "Dup", "password": TEST_USER_PASSWORD}
    assert live_client.post("/api/v1/auth/signup", json=body).status_code == 201
    duplicate = live_client.post("/api/v1/auth/signup", json={**body, "email": email.upper()})
    assert duplicate.status_code == 409
    wrong = live_client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-pass"})
    missing = live_client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong-pass"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]
    me = live_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"].startswith("admin-")
    short = live_client.post("/api/v1/auth/signup", json={**body, "password": "short"})
    assert short.status_code == 422


def test_refresh_rotation_and_reuse_detection(live_client: TestClient) -> None:  # noqa: F811
    email = f"refresh-{uuid4().hex[:8]}@example.com"
    live_client.post(
        "/api/v1/auth/signup",
        json={"email": email, "display_name": "R", "password": TEST_USER_PASSWORD},
    )
    first = live_client.post(
        "/api/v1/auth/login", json={"email": email, "password": TEST_USER_PASSWORD}
    ).json()
    second = live_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert second.status_code == 200
    rotated = second.json()
    assert rotated["refresh_token"] != first["refresh_token"]
    # Replaying the already-rotated token revokes the whole family, including the new token.
    replay = live_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert replay.status_code == 401
    after = live_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    )
    assert after.status_code == 401


def test_logout_revokes_refresh_token(live_client: TestClient) -> None:  # noqa: F811
    email = f"logout-{uuid4().hex[:8]}@example.com"
    live_client.post(
        "/api/v1/auth/signup",
        json={"email": email, "display_name": "L", "password": TEST_USER_PASSWORD},
    )
    tokens = live_client.post(
        "/api/v1/auth/login", json={"email": email, "password": TEST_USER_PASSWORD}
    ).json()
    assert (
        live_client.post(
            "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 204
    )
    assert (
        live_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )


def test_tenant_isolation_and_role_enforcement(live_client: TestClient) -> None:  # noqa: F811
    workspace_id = str(create_workspace(live_client)["id"])
    service = create_service(live_client, workspace_id)
    outsider_email, outsider = new_user(live_client, "outsider")
    viewer_email, viewer = new_user(live_client, "viewer")
    responder_email, responder = new_user(live_client, "responder")
    add_member(live_client, workspace_id, viewer_email, "viewer")
    add_member(live_client, workspace_id, responder_email, "responder")
    base = f"/api/v1/workspaces/{workspace_id}"
    incident_body = {"title": "Checkout down", "service_id": service["id"], "severity": "sev2"}

    # Non-members cannot tell the workspace exists.
    for path in ("", "/incidents", "/services", "/members", "/api-keys"):
        denied = live_client.get(base + path, headers=outsider)
        assert denied.status_code == 404, (path, denied.text)
    assert (
        live_client.post(base + "/incidents", json=incident_body, headers=outsider).status_code
        == 404
    )
    assert live_client.get("/api/v1/workspaces", headers=outsider).json() == []

    # Viewer: read-only.
    assert live_client.get(base + "/incidents", headers=viewer).status_code == 200
    assert live_client.get(base + "/services", headers=viewer).status_code == 200
    forbidden = live_client.post(base + "/incidents", json=incident_body, headers=viewer)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"
    assert live_client.get(base + "/members", headers=viewer).status_code == 403

    # Responder: can work incidents, cannot administer.
    created = live_client.post(base + "/incidents", json=incident_body, headers=responder)
    assert created.status_code == 201, created.text
    incident_id = created.json()["id"]
    timeline = live_client.get(f"{base}/incidents/{incident_id}/timeline", headers=viewer).json()
    assert timeline[0]["actor_id"].startswith("user:")
    assert (
        live_client.post(
            base + "/services",
            json={"name": "X", "slug": "x-svc", "environment": "production"},
            headers=responder,
        ).status_code
        == 403
    )
    assert (
        live_client.post(base + "/api-keys", json={"name": "k"}, headers=responder).status_code
        == 403
    )
    assert (
        live_client.post(
            f"{base}/incidents/{incident_id}/investigations",
            json={"trigger": "manual"},
            headers=viewer,
        ).status_code
        == 403
    )

    # Admin (the creator) can do everything and sees the member list.
    members = live_client.get(base + "/members")
    assert members.status_code == 200
    assert {m["role"] for m in members.json()} == {"admin", "viewer", "responder"}
    assert outsider_email not in {m["email"] for m in members.json()}


def test_last_admin_cannot_be_removed_or_demoted(live_client: TestClient) -> None:  # noqa: F811
    workspace_id = str(create_workspace(live_client)["id"])
    me = live_client.get("/api/v1/auth/me").json()
    admin_id, admin_email = me["user"]["id"], me["user"]["email"]
    base = f"/api/v1/workspaces/{workspace_id}/members"
    assert live_client.delete(f"{base}/{admin_id}").status_code == 409
    demote = live_client.post(base, json={"email": admin_email, "role": "viewer"})
    assert demote.status_code == 409
    other_email, _ = new_user(live_client, "second-admin")
    add_member(live_client, workspace_id, other_email, "admin")
    assert live_client.delete(f"{base}/{admin_id}").status_code == 204
    assert live_client.get(f"/api/v1/workspaces/{workspace_id}").status_code == 404


def test_api_key_ingestion_lifecycle(live_client: TestClient) -> None:  # noqa: F811
    workspace_id = str(create_workspace(live_client)["id"])
    other_workspace_id = str(create_workspace(live_client)["id"])
    service = create_service(live_client, workspace_id)
    created = live_client.post(f"/api/v1/workspaces/{workspace_id}/api-keys", json={"name": "prom"})
    assert created.status_code == 201, created.text
    key, key_id = created.json()["key"], created.json()["id"]
    listing = live_client.get(f"/api/v1/workspaces/{workspace_id}/api-keys").json()
    assert "key" not in listing[0]
    assert key not in str(listing)

    def ingest(workspace: str, api_key: str, idem: str) -> int:
        return live_client.post(
            f"/api/v1/workspaces/{workspace}/events",
            json=event_body(service["id"]),
            headers={"X-API-Key": api_key, "Idempotency-Key": idem, "Authorization": ""},
        ).status_code

    assert ingest(workspace_id, key, f"k-{uuid4().hex}") == 201
    assert ingest(workspace_id, key + "x", f"k-{uuid4().hex}") == 401
    assert ingest(workspace_id, "garbage", f"k-{uuid4().hex}") == 401
    # A key only works in the workspace that issued it.
    assert ingest(other_workspace_id, key, f"k-{uuid4().hex}") == 401
    # A key is not a login credential and cannot read data.
    read = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/incidents",
        headers={"X-API-Key": key, "Authorization": ""},
    )
    assert read.status_code == 401
    # No credentials at all.
    anonymous = live_client.post(
        f"/api/v1/workspaces/{workspace_id}/events",
        json=event_body(service["id"]),
        headers={"Idempotency-Key": f"k-{uuid4().hex}", "Authorization": ""},
    )
    assert anonymous.status_code == 401

    revoked = live_client.delete(f"/api/v1/workspaces/{workspace_id}/api-keys/{key_id}")
    assert revoked.status_code == 204
    assert ingest(workspace_id, key, f"k-{uuid4().hex}") == 401
    assert (
        live_client.delete(f"/api/v1/workspaces/{workspace_id}/api-keys/{key_id}").status_code
        == 404
    )

    events = live_client.get(f"/api/v1/workspaces/{workspace_id}/incidents").json()["items"]
    timeline = live_client.get(
        f"/api/v1/workspaces/{workspace_id}/incidents/{events[0]['id']}/timeline"
    ).json()
    assert any(str(item["actor_id"]).startswith("api_key:") for item in timeline)


@pytest.fixture
def rate_limited_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("AUTH_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("INGEST_RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    with TestClient(create_app(get_settings()), client=(random_client_ip(), 50000)) as client:
        client.headers.update(signup_and_login(client, "limited"))
        yield client
    get_settings.cache_clear()


def test_ingestion_and_login_rate_limits(rate_limited_client: TestClient) -> None:
    client = rate_limited_client
    workspace_id = str(create_workspace(client)["id"])
    service = create_service(client, workspace_id)
    statuses = [
        client.post(
            f"/api/v1/workspaces/{workspace_id}/events",
            json=event_body(service["id"]),
            headers={"Idempotency-Key": f"rl-{uuid4().hex}"},
        )
        for _ in range(5)
    ]
    assert [r.status_code for r in statuses] == [201, 201, 201, 429, 429]
    assert int(statuses[-1].headers["retry-after"]) >= 1
    assert statuses[-1].json()["error"]["code"] == "rate_limited"

    email = f"brute-{uuid4().hex[:8]}@example.com"
    attempts = [
        client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
        for _ in range(5)
    ]
    assert [r.status_code for r in attempts] == [401, 401, 401, 429, 429]


def test_websocket_requires_auth_and_membership(live_client: TestClient) -> None:  # noqa: F811
    workspace_id = str(create_workspace(live_client)["id"])
    url = f"/api/v1/ws/workspaces/{workspace_id}/incidents"
    token = live_client.headers["Authorization"].removeprefix("Bearer ")
    _, outsider = new_user(live_client, "ws-outsider")
    outsider_token = outsider["Authorization"].removeprefix("Bearer ")

    def close_code(message: dict[str, str] | None) -> int:
        with live_client.websocket_connect(url) as socket:
            if message is not None:
                socket.send_json(message)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            return int(closed.value.code)

    assert close_code({"type": "auth", "token": "garbage"}) == 4401
    assert close_code({"type": "hello"}) == 4401
    assert close_code({"type": "auth", "token": outsider_token}) == 4404
    with live_client.websocket_connect(url) as socket:
        socket.send_json({"type": "auth", "token": token})
        assert socket.receive_json()["type"] == "connection.established"
