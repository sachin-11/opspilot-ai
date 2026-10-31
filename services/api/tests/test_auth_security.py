from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from opspilot_api.auth.models import Role
from opspilot_api.auth.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_token,
    new_api_key,
    split_api_key,
    verify_password,
)
from opspilot_api.main import create_app
from tests.conftest import make_settings

SECRET = "unit-test-secret-0123456789abcdefghij"  # not a real credential


def test_password_hash_roundtrip_and_unknown_user() -> None:
    hashed = hash_password("correct horse battery")
    assert hashed != "correct horse battery"
    assert verify_password("correct horse battery", hashed)
    assert not verify_password("wrong password", hashed)
    assert not verify_password("anything", None)


def test_access_token_roundtrip() -> None:
    user_id = uuid4()
    token = create_access_token(user_id, SECRET, 5)
    assert decode_access_token(token, SECRET) == user_id


def test_access_token_rejects_wrong_secret_expiry_and_none_algorithm() -> None:
    user_id = uuid4()
    token = create_access_token(user_id, SECRET, 5)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, "a-different-secret-0123456789abcdefgh")
    expired = jwt.encode(
        {
            "iss": "opspilot-api",
            "sub": str(user_id),
            "typ": "access",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(expired, SECRET)
    unsigned = jwt.encode(
        {"iss": "opspilot-api", "sub": str(user_id), "typ": "access", "iat": 1, "exp": 9999999999},
        "",
        algorithm="none",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned, SECRET)
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-jwt", SECRET)


def test_refresh_style_token_is_not_an_access_token() -> None:
    wrong_type = jwt.encode(
        {
            "iss": "opspilot-api",
            "sub": str(uuid4()),
            "typ": "refresh",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(wrong_type, SECRET)


def test_api_key_format_and_hashing() -> None:
    prefix, key = new_api_key()
    assert key.startswith(f"opk_{prefix}_")
    assert split_api_key(key) == prefix
    assert hash_token(key) != key
    for bad in ("", "opk_", "opk_short_x", "other_12345678_secret", "opk_12345678_"):
        assert split_api_key(bad) is None


def test_role_ordering() -> None:
    assert Role.VIEWER < Role.RESPONDER < Role.ADMIN
    assert Role.parse("admin") is Role.ADMIN


def test_jwt_secret_is_required_and_long_enough() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        make_settings(AUTH_JWT_SECRET="too-short")


def test_protected_routes_require_authentication() -> None:
    with TestClient(create_app(make_settings()), raise_server_exceptions=False) as client:
        response = client.get(f"/api/v1/workspaces/{uuid4()}/incidents")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "unauthorized"
        assert client.get("/api/v1/workspaces").status_code == 401
        bad = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        signup = client.post(
            "/api/v1/auth/signup",
            json={"email": "a@example.com", "display_name": "A", "password": "x" * 12},
        )
        assert signup.status_code == 403
        assert client.get("/health/live").status_code == 200
