"""GitHub webhook verification, normalization, and worker-side processing.

Signature verification runs over the raw request bytes before any JSON parsing. Only documented
payload fields are read, and only selected normalized fields are stored (never the raw payload).
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.service import utcnow
from opspilot_api.investigations.safety import sanitize_error
from opspilot_api.repositories.events import publish_repository_event
from opspilot_api.repositories.models import (
    DeliveryStatus,
    GitHubInstallation,
    GitHubWebhookDelivery,
    InstallationStatus,
    Repository,
    RepositoryDeployment,
    RepositoryStatus,
)
from opspilot_api.repositories.provider import RepositoryProvider
from opspilot_api.repositories.sync import (
    ensure_provider_access,
    store_pull_request,
    sync_installation_repositories,
    sync_repository,
)

logger = logging.getLogger(__name__)

SIGNATURE_PREFIX = "sha256="
SUPPORTED_EVENTS = {
    "installation",
    "installation_repositories",
    "push",
    "pull_request",
    "deployment",
    "deployment_status",
}
_INSTALLATION_ACTIONS = {"created", "suspend", "unsuspend", "deleted"}
_PULL_REQUEST_ACTIONS = {"opened", "synchronize", "closed", "reopened"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time HMAC-SHA256 check of ``X-Hub-Signature-256`` over the raw bytes."""
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(SIGNATURE_PREFIX) :])


@dataclass
class NormalizedEvent:
    event_type: str
    action: str | None
    installation_id: int | None
    repository_id: int | None
    supported: bool
    ignore_reason: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


def _text(value: object, limit: int = 300) -> str | None:
    return str(value)[:limit] if isinstance(value, str | int) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def normalize_event(event_type: str, payload: dict[str, Any]) -> NormalizedEvent:
    """Extract documented fields only; unknown events/actions are marked unsupported."""
    action = _text(payload.get("action"), 60)
    installation = payload.get("installation")
    repository = payload.get("repository")
    installation_id = _int(installation.get("id")) if isinstance(installation, dict) else None
    repository_id = _int(repository.get("id")) if isinstance(repository, dict) else None
    event = NormalizedEvent(event_type, action, installation_id, repository_id, supported=False)
    if event_type not in SUPPORTED_EVENTS:
        event.ignore_reason = "unsupported_event"
        return event
    fields: dict[str, Any] = {}
    if event_type == "installation":
        if action not in _INSTALLATION_ACTIONS:
            event.ignore_reason = "unsupported_action"
            return event
    elif event_type == "installation_repositories":
        for key in ("repositories_added", "repositories_removed"):
            items = payload.get(key)
            fields[key] = [
                {"id": _int(item.get("id")), "full_name": _text(item.get("full_name"), 320)}
                for item in (items if isinstance(items, list) else [])[:100]
                if isinstance(item, dict) and _int(item.get("id")) is not None
            ]
    elif event_type == "push":
        ref = _text(payload.get("ref"), 255) or ""
        commits = payload.get("commits")
        fields = {
            "ref": ref,
            "branch": ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None,
            "before": _text(payload.get("before"), 64),
            "after": _text(payload.get("after"), 64),
            "deleted": bool(payload.get("deleted", False)),
            "commit_shas": [
                str(item["id"])[:64]
                for item in (commits if isinstance(commits, list) else [])[:20]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ],
        }
    elif event_type == "pull_request":
        if action not in _PULL_REQUEST_ACTIONS:
            event.ignore_reason = "unsupported_action"
            return event
        pull = payload.get("pull_request")
        number = _int(payload.get("number")) or (
            _int(pull.get("number")) if isinstance(pull, dict) else None
        )
        if number is None:
            event.ignore_reason = "malformed_payload"
            return event
        fields = {"number": number}
    elif event_type in {"deployment", "deployment_status"}:
        deployment = payload.get("deployment")
        if not isinstance(deployment, dict) or _int(deployment.get("id")) is None:
            event.ignore_reason = "malformed_payload"
            return event
        fields = {
            "deployment_id": _int(deployment.get("id")),
            "sha": _text(deployment.get("sha"), 64),
            "environment": _text(deployment.get("environment"), 100),
            "description": _text(deployment.get("description"), 500),
            "created_at": _text(deployment.get("created_at"), 40),
        }
        if event_type == "deployment_status":
            status = payload.get("deployment_status")
            if not isinstance(status, dict):
                event.ignore_reason = "malformed_payload"
                return event
            fields["state"] = _text(status.get("state"), 30)
            fields["status_created_at"] = _text(status.get("created_at"), 40)
    event.supported = True
    event.fields = fields
    return event


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class WebhookIgnored(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _repository_for(
    session: Session, installation: GitHubInstallation, provider_repository_id: int | None
) -> Repository:
    if provider_repository_id is None:
        raise WebhookIgnored("missing_repository")
    repository = session.scalar(
        select(Repository).where(
            Repository.workspace_id == installation.workspace_id,
            Repository.github_installation_id == installation.id,
            Repository.provider_repository_id == provider_repository_id,
        )
    )
    if repository is None:
        raise WebhookIgnored("unknown_repository")
    return repository


def process_delivery(
    session: Session, settings: Settings, provider: RepositoryProvider, delivery_id: UUID
) -> str:
    """Apply one verified delivery. Idempotent: processed/ignored deliveries are skipped."""
    delivery = session.get(GitHubWebhookDelivery, delivery_id)
    if delivery is None:
        raise LookupError("webhook delivery not found")
    if delivery.status in {DeliveryStatus.PROCESSED.value, DeliveryStatus.IGNORED.value}:
        return delivery.status
    workspace_id: UUID | None = None
    repository_id: UUID | None = None
    try:
        installation = (
            session.scalar(
                select(GitHubInstallation).where(
                    GitHubInstallation.provider_installation_id == delivery.installation_id
                )
            )
            if delivery.installation_id is not None
            else None
        )
        if installation is None:
            raise WebhookIgnored("unknown_installation")
        workspace_id = installation.workspace_id
        repository_id = _apply_event(session, settings, provider, installation, delivery)
        delivery.status = DeliveryStatus.PROCESSED.value
        delivery.failure_code = None
        delivery.failure_message = None
    except WebhookIgnored as ignored:
        session.rollback()
        delivery = session.get(GitHubWebhookDelivery, delivery_id)
        if delivery is None:
            raise LookupError("webhook delivery not found") from None
        delivery.status = DeliveryStatus.IGNORED.value
        delivery.failure_code = ignored.reason
    except Exception as exc:
        session.rollback()
        delivery = session.get(GitHubWebhookDelivery, delivery_id)
        if delivery is None:
            raise LookupError("webhook delivery not found") from None
        delivery.status = DeliveryStatus.FAILED.value
        delivery.failure_code = str(getattr(exc, "code", type(exc).__name__.lower()))[:80]
        delivery.failure_message = sanitize_error(exc)
        delivery.processed_at = utcnow()
        session.commit()
        raise
    delivery.processed_at = utcnow()
    session.commit()
    if workspace_id is not None:
        publish_repository_event(
            settings,
            workspace_id,
            "repository.webhook_processed",
            {
                "repository_id": repository_id,
                "event_type": delivery.event_type,
                "status": delivery.status,
            },
        )
    return delivery.status


def _apply_event(
    session: Session,
    settings: Settings,
    provider: RepositoryProvider,
    installation: GitHubInstallation,
    delivery: GitHubWebhookDelivery,
) -> UUID | None:
    event_type, action, data = delivery.event_type, delivery.action, delivery.normalized
    now = utcnow()
    if event_type == "installation":
        if action == "suspend":
            installation.status = InstallationStatus.SUSPENDED.value
            installation.suspended_at = now
        elif action == "unsuspend":
            installation.status = InstallationStatus.ACTIVE.value
            installation.suspended_at = None
        elif action == "deleted":
            installation.status = InstallationStatus.REMOVED.value
            installation.removed_at = now
            for repository in session.scalars(
                select(Repository).where(
                    Repository.workspace_id == installation.workspace_id,
                    Repository.github_installation_id == installation.id,
                )
            ):
                repository.status = RepositoryStatus.REMOVED.value
        elif action == "created":
            installation.status = InstallationStatus.ACTIVE.value
            installation.removed_at = None
            session.commit()
            sync_installation_repositories(
                session, settings, provider, installation.workspace_id, installation.id
            )
        return None
    if event_type == "installation_repositories":
        removed_ids = [item["id"] for item in data.get("repositories_removed", [])]
        for repository in session.scalars(
            select(Repository).where(
                Repository.workspace_id == installation.workspace_id,
                Repository.github_installation_id == installation.id,
                Repository.provider_repository_id.in_(removed_ids),
            )
        ):
            repository.status = RepositoryStatus.REMOVED.value
        session.commit()
        if data.get("repositories_added"):
            sync_installation_repositories(
                session, settings, provider, installation.workspace_id, installation.id
            )
        return None
    repository = _repository_for(session, installation, delivery.repository_id)
    if event_type == "push":
        if data.get("deleted") or not data.get("after"):
            raise WebhookIgnored("branch_deleted")
        if data.get("branch") != repository.default_branch:
            raise WebhookIgnored("non_default_branch")
        ensure_provider_access(installation, repository)
        # Safer than patching a live snapshot: build a new bounded snapshot at the new head;
        # unchanged chunk embeddings are reused through the content-keyed cache.
        sync_repository(
            session,
            settings,
            provider,
            installation.workspace_id,
            repository.id,
            shas=[data["after"], *data.get("commit_shas", [])],
        )
        return repository.id
    if event_type == "pull_request":
        ensure_provider_access(installation, repository)
        pull = provider.get_pull_request(
            installation.provider_installation_id,
            repository.owner,
            repository.name,
            int(data["number"]),
            60,
        )
        store_pull_request(session, settings, repository, pull)
        session.commit()
        return repository.id
    if event_type in {"deployment", "deployment_status"}:
        _apply_deployment(session, repository, data)
        session.commit()
        return repository.id
    raise WebhookIgnored("unsupported_event")


def _apply_deployment(session: Session, repository: Repository, data: dict[str, Any]) -> None:
    provider_id = int(data["deployment_id"])
    existing = session.scalar(
        select(RepositoryDeployment).where(
            RepositoryDeployment.workspace_id == repository.workspace_id,
            RepositoryDeployment.repository_id == repository.id,
            RepositoryDeployment.provider_deployment_id == provider_id,
        )
    )
    created_at = _parse_time(data.get("created_at")) or utcnow()
    if existing is None:
        if not data.get("sha") or not data.get("environment"):
            raise WebhookIgnored("malformed_payload")
        existing = RepositoryDeployment(
            id=uuid4(),
            workspace_id=repository.workspace_id,
            repository_id=repository.id,
            provider_deployment_id=provider_id,
            commit_sha=data["sha"],
            environment=data["environment"],
            status="pending",
            description=data.get("description"),
            deployed_at=created_at,
        )
        session.add(existing)
    state = data.get("state")
    if state:
        existing.status = state
        if state in {"success", "failure", "error", "inactive"}:
            existing.completed_at = _parse_time(data.get("status_created_at")) or utcnow()
