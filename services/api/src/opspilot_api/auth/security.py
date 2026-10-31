"""Password hashing, JWT access tokens, and opaque token helpers.

Pure functions only: no I/O, so they are cheap to unit test.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "opspilot-api"
API_KEY_PREFIX = "opk"

_hasher = PasswordHasher()
# Verified against when the account does not exist, so response time does not reveal it.
_DUMMY_HASH = _hasher.hash("opspilot-dummy-password")


class InvalidTokenError(Exception):
    """Access token is missing, malformed, expired, or not signed by us."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def create_access_token(user_id: UUID, secret: str, ttl_minutes: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": JWT_ISSUER,
        "sub": str(user_id),
        "typ": "access",
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> UUID:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
        if claims.get("typ") != "access":
            raise InvalidTokenError("wrong token type")
        return UUID(str(claims["sub"]))
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidTokenError(type(exc).__name__) from exc


def hash_token(token: str) -> str:
    """SHA-256 of a high-entropy random token (refresh tokens and API keys)."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def new_api_key() -> tuple[str, str]:
    """Return ``(prefix, full_key)``. The full key is shown once and never stored."""
    prefix = secrets.token_hex(4)
    return prefix, f"{API_KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"


def split_api_key(key: str) -> str | None:
    """Return the lookup prefix of a well-formed key, else None."""
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX or len(parts[1]) != 8 or not parts[2]:
        return None
    return parts[1]


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())
