"""Webhook secrets: generation, encryption at rest, and timestamped HMAC signature checks.

The server must recompute HMACs, so the secret cannot be one-way hashed; it is stored encrypted
(Fernet) under ``OBS_SECRET_ENCRYPTION_KEY`` and never returned after creation or rotation.
Production-grade key management (KMS/rotation of the encryption key) is deferred to a later
deployment/security module.
"""

import hashlib
import hmac
import secrets
from enum import StrEnum

from cryptography.fernet import Fernet, InvalidToken

from opspilot_api.config import Settings

TIMESTAMP_HEADER = "x-opspilot-timestamp"
SIGNATURE_HEADER = "x-opspilot-signature"
ENCRYPTED_PREFIX = "enc:v1:"


class SecretsConfigurationError(Exception):
    code = "secrets_not_configured"


class SignatureResult(StrEnum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    EXPIRED = "expired"


def generate_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def generate_public_id() -> str:
    """Unguessable, URL-safe source identifier (192 bits of entropy)."""
    return secrets.token_urlsafe(24)


def _fernet(settings: Settings) -> Fernet:
    key = settings.OBS_SECRET_ENCRYPTION_KEY
    if key is None:
        raise SecretsConfigurationError("OBS_SECRET_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(key.get_secret_value().encode())
    except ValueError as exc:
        raise SecretsConfigurationError("OBS_SECRET_ENCRYPTION_KEY is invalid") from exc


def encrypt_secret(settings: Settings, plaintext: str) -> str:
    return ENCRYPTED_PREFIX + _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_secret(settings: Settings, reference: str) -> str:
    if not reference.startswith(ENCRYPTED_PREFIX):
        raise SecretsConfigurationError("unsupported secret reference")
    try:
        return _fernet(settings).decrypt(reference[len(ENCRYPTED_PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise SecretsConfigurationError("stored secret cannot be decrypted") from exc


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 over ``"{timestamp}.{raw body}"``."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float,
    window_seconds: int,
) -> SignatureResult:
    if not timestamp or not signature:
        return SignatureResult.MISSING
    try:
        sent = int(timestamp)
    except ValueError:
        return SignatureResult.INVALID
    provided = signature.removeprefix("sha256=").strip().lower()
    expected = compute_signature(secret, timestamp, body)
    # Compare first so that timing does not reveal whether the timestamp was acceptable.
    matches = hmac.compare_digest(expected, provided)
    if not matches:
        return SignatureResult.INVALID
    if abs(now - sent) > window_seconds:
        return SignatureResult.EXPIRED
    return SignatureResult.OK
