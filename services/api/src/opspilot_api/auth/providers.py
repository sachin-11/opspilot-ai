"""Typed identity providers: OIDC/Cognito-compatible JWT validation and a local dev provider.

Tokens are always signature-verified against an explicit algorithm allowlist. Issuer and JWKS URL
come only from server configuration, never from the token or the request. Errors never contain
token material.
"""

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from opspilot_api.config import Environment, Settings

LOCAL_ISSUER = "opspilot-local"
LOCAL_AUDIENCE = "opspilot-api"
LOCAL_PROVIDER_NAME = "local"
OIDC_PROVIDER_NAME = "oidc"
_JWKS_MIN_REFRESH_SECONDS = 5.0


class AuthError(Exception):
    code = "unauthorized"
    status_code = 401


class TokenExpiredError(AuthError):
    code = "token_expired"


class InvalidTokenError(AuthError):
    code = "invalid_token"


class AuthConfigurationError(AuthError):
    code = "auth_misconfigured"
    status_code = 503


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None
    avatar_url: str | None


class IdentityProvider(ABC):
    name: str

    @abstractmethod
    def verify(self, token: str) -> VerifiedIdentity:
        """Return the verified identity or raise an :class:`AuthError`."""


def _identity(provider: str, claims: dict[str, Any]) -> VerifiedIdentity:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
        raise InvalidTokenError("token has no usable subject")
    email = claims.get("email")
    name = claims.get("name") or claims.get("preferred_username") or claims.get("username")
    picture = claims.get("picture")
    return VerifiedIdentity(
        provider=provider,
        subject=subject,
        email=email.strip().lower()[:320] if isinstance(email, str) and email.strip() else None,
        email_verified=claims.get("email_verified") in {True, "true"},
        display_name=str(name)[:200] if isinstance(name, str) and name else None,
        avatar_url=str(picture)[:500] if isinstance(picture, str) and picture else None,
    )


class JwksClient:
    """Fetches the issuer's JWKS with a bounded cache and one-shot refresh on unknown ``kid``."""

    def __init__(
        self,
        url: str,
        cache_seconds: int,
        timeout: float,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.cache_seconds = cache_seconds
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()
        self.fetches = 0

    def _refresh(self) -> None:
        self.fetches += 1
        try:
            response = self._client.get(self.url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthConfigurationError("identity provider keys are unavailable") from exc
        keys: dict[str, Any] = {}
        for item in document.get("keys", []):
            kid = item.get("kid")
            if isinstance(kid, str) and item.get("kty") == "RSA":
                keys[kid] = RSAAlgorithm.from_jwk(item)
        if not keys:
            raise AuthConfigurationError("identity provider returned no usable signing keys")
        self._keys = keys
        self._fetched_at = self._clock()

    def get_key(self, kid: str) -> Any:
        with self._lock:
            now = self._clock()
            if self._fetched_at is None or now - self._fetched_at > self.cache_seconds:
                self._refresh()
            elif kid not in self._keys and now - self._fetched_at >= _JWKS_MIN_REFRESH_SECONDS:
                # Key rotation: an unseen kid triggers one bounded refresh before rejecting.
                self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise InvalidTokenError("unknown signing key")
        return key


class OidcProvider(IdentityProvider):
    name = OIDC_PROVIDER_NAME

    def __init__(self, settings: Settings, jwks: JwksClient | None = None) -> None:
        if not settings.AUTH_OIDC_ISSUER or not settings.AUTH_OIDC_AUDIENCE:
            raise AuthConfigurationError("OIDC issuer and audience are not configured")
        self.issuer = settings.AUTH_OIDC_ISSUER.rstrip("/")
        self.audience = settings.AUTH_OIDC_AUDIENCE
        self.algorithms = list(settings.AUTH_OIDC_ALGORITHMS)
        self.token_use = settings.AUTH_OIDC_TOKEN_USE
        self.skew = settings.AUTH_CLOCK_SKEW_SECONDS
        # The JWKS URL comes from server configuration only: an explicit setting, else
        # derived from the configured issuer (Cognito convention). Never from a token.
        self.jwks = jwks or JwksClient(
            settings.AUTH_OIDC_JWKS_URL or f"{self.issuer}/.well-known/jwks.json",
            settings.AUTH_JWKS_CACHE_SECONDS,
            settings.AUTH_JWKS_TIMEOUT_SECONDS,
        )

    def verify(self, token: str) -> VerifiedIdentity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("malformed token") from exc
        algorithm = header.get("alg")
        kid = header.get("kid")
        if algorithm not in self.algorithms or not isinstance(kid, str):
            raise InvalidTokenError("unsupported token header")
        key = self.jwks.get_key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                leeway=self.skew,
                options={"require": ["exp", "sub", "iss"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("token expired") from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("token validation failed") from exc
        self._check_audience_and_use(claims)
        return _identity(self.name, claims)

    def _check_audience_and_use(self, claims: dict[str, Any]) -> None:
        use = claims.get("token_use")
        if use is not None and use != self.token_use:
            raise InvalidTokenError("wrong token type")
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else list(audience or [])
        # Cognito access tokens carry the app client in ``client_id`` instead of ``aud``.
        if self.audience not in audiences and claims.get("client_id") != self.audience:
            raise InvalidTokenError("wrong audience")


class LocalProvider(IdentityProvider):
    """Deterministic provider for development and tests; refused in production."""

    name = LOCAL_PROVIDER_NAME

    def __init__(self, settings: Settings) -> None:
        if settings.APP_ENV not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise AuthConfigurationError("local authentication is disabled outside development")
        self._key = settings.AUTH_LOCAL_SIGNING_KEY.get_secret_value()
        self.skew = settings.AUTH_CLOCK_SKEW_SECONDS

    def verify(self, token: str) -> VerifiedIdentity:
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=["HS256"],
                issuer=LOCAL_ISSUER,
                audience=LOCAL_AUDIENCE,
                leeway=self.skew,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("token expired") from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("token validation failed") from exc
        return _identity(self.name, claims)


def issue_local_token(
    settings: Settings,
    subject: str,
    email: str | None = None,
    name: str | None = None,
    ttl_seconds: int | None = None,
    email_verified: bool = True,
    nonce: str | None = None,
) -> str:
    """Sign a short-lived local token. Only usable where the local provider is permitted."""
    if settings.APP_ENV not in {Environment.DEVELOPMENT, Environment.TEST}:
        raise AuthConfigurationError("local tokens cannot be issued outside development")
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": LOCAL_ISSUER,
        "aud": LOCAL_AUDIENCE,
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds or settings.AUTH_LOCAL_TOKEN_TTL_SECONDS),
    }
    if email:
        claims["email"] = email
        claims["email_verified"] = email_verified
    if name:
        claims["name"] = name
    if nonce:
        claims["nonce"] = nonce
    return jwt.encode(claims, settings.AUTH_LOCAL_SIGNING_KEY.get_secret_value(), algorithm="HS256")


def create_identity_provider(settings: Settings) -> IdentityProvider:
    if settings.AUTH_PROVIDER == "oidc":
        return OidcProvider(settings)
    return LocalProvider(settings)
