"""GitHub App adapter: installation-token authentication and read-only REST calls.

Only ``GET`` requests are issued for repository data. The single ``POST`` is the documented
installation-access-token exchange, requested with read-only permissions. Tokens live only in
process memory for their provider-declared lifetime and never appear in errors or logs.
"""

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from opspilot_api.config import Settings
from opspilot_api.repositories.provider import (
    ProviderAuthenticationError,
    ProviderBranch,
    ProviderCommit,
    ProviderDeployment,
    ProviderDeploymentStatus,
    ProviderError,
    ProviderFileChange,
    ProviderInstallation,
    ProviderNotFoundError,
    ProviderPullRequest,
    ProviderRateLimitError,
    ProviderRepository,
    ProviderTree,
    ProviderTreeEntry,
    ProviderUnavailableError,
    RateLimitInfo,
    RepositoryProvider,
)

# Minimum permissions requested for installation tokens. No write permission is ever requested.
READ_ONLY_PERMISSIONS = {
    "contents": "read",
    "metadata": "read",
    "pull_requests": "read",
    "deployments": "read",
}
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_MAX_PAGES = 5
_MAX_PATCH_CHARS = 20_000


def _validated_name(value: str) -> str:
    if not _NAME.match(value) or value in {".", ".."}:
        raise ProviderError("invalid repository identifier")
    return value


def _validated_sha(value: str) -> str:
    if not _SHA.match(value):
        raise ProviderError("invalid git revision")
    return value


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubProvider(RepositoryProvider):
    name = "github"

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if settings.GITHUB_APP_ID is None:
            raise ProviderAuthenticationError("GitHub App ID is not configured")
        self.settings = settings
        self._app_id = settings.GITHUB_APP_ID
        self._clock = clock
        self._client = client or httpx.Client(
            base_url=settings.GITHUB_API_BASE_URL,
            timeout=settings.GITHUB_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        self._tokens: dict[int, tuple[str, float]] = {}
        self._rate_limit: RateLimitInfo | None = None

    # -- authentication -------------------------------------------------------------------
    def _private_key(self) -> str:
        if self.settings.GITHUB_APP_PRIVATE_KEY is not None:
            return self.settings.GITHUB_APP_PRIVATE_KEY.get_secret_value().replace("\\n", "\n")
        if self.settings.GITHUB_APP_PRIVATE_KEY_FILE:
            try:
                return Path(self.settings.GITHUB_APP_PRIVATE_KEY_FILE).read_text(encoding="utf-8")
            except OSError as exc:
                raise ProviderAuthenticationError("GitHub App private key is unreadable") from exc
        raise ProviderAuthenticationError("GitHub App private key is not configured")

    def _app_jwt(self) -> str:
        now = int(self._clock())
        try:
            return jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)},
                self._private_key(),
                algorithm="RS256",
            )
        except (ValueError, TypeError, jwt.PyJWTError) as exc:
            raise ProviderAuthenticationError("GitHub App private key is invalid") from exc

    def _installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached is not None and cached[1] - 60 > self._clock():
            return cached[0]
        response = self._send(
            "POST",
            f"/app/installations/{int(installation_id)}/access_tokens",
            headers={"Authorization": f"Bearer {self._app_jwt()}"},
            json={"permissions": READ_ONLY_PERMISSIONS},
        )
        body = response.json()
        token = str(body["token"])
        expires = _timestamp(body.get("expires_at"))
        lifetime = expires.timestamp() if expires else self._clock() + 300
        self._tokens[installation_id] = (token, lifetime)
        return token

    # -- transport ------------------------------------------------------------------------
    def _record_rate_limit(self, response: httpx.Response) -> None:
        headers = response.headers
        if "x-ratelimit-remaining" not in headers:
            return
        reset = headers.get("x-ratelimit-reset")
        self._rate_limit = RateLimitInfo(
            limit=int(headers["x-ratelimit-limit"]) if "x-ratelimit-limit" in headers else None,
            remaining=int(headers["x-ratelimit-remaining"]),
            reset_at=datetime.fromtimestamp(int(reset), UTC) if reset else None,
        )

    def _send(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, headers=headers, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError("GitHub request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"GitHub request failed: {type(exc).__name__}") from exc
        self._record_rate_limit(response)
        status = response.status_code
        if status < 400:
            return response
        retry_after = response.headers.get("retry-after")
        exhausted = response.headers.get("x-ratelimit-remaining") == "0"
        if status == 429 or (status == 403 and (retry_after or exhausted)):
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 60
            if exhausted and self._rate_limit and self._rate_limit.reset_at:
                wait = max(1, int(self._rate_limit.reset_at.timestamp() - self._clock()))
            raise ProviderRateLimitError("GitHub rate limit reached", wait)
        if status in {401, 403}:
            raise ProviderAuthenticationError(f"GitHub rejected the request ({status})")
        if status in {404, 410}:
            raise ProviderNotFoundError("GitHub resource not found")
        if status >= 500:
            raise ProviderUnavailableError(f"GitHub is unavailable ({status})")
        raise ProviderError(f"GitHub rejected the request ({status})")

    def _get(
        self, installation_id: int, path: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        token = self._installation_token(installation_id)
        return self._send(
            "GET",
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params=params,
        )

    @staticmethod
    def _repo_path(owner: str, name: str) -> str:
        return f"/repos/{_validated_name(owner)}/{_validated_name(name)}"

    # -- conversions ----------------------------------------------------------------------
    @staticmethod
    def _repository(item: dict[str, Any]) -> ProviderRepository:
        return ProviderRepository(
            id=item["id"],
            owner=item["owner"]["login"],
            name=item["name"],
            full_name=item["full_name"],
            default_branch=item.get("default_branch") or "main",
            private=bool(item.get("private", True)),
            archived=bool(item.get("archived", False)),
        )

    @staticmethod
    def _file_change(item: dict[str, Any]) -> ProviderFileChange:
        patch = item.get("patch")
        status = {"added": "added", "removed": "removed", "renamed": "renamed"}.get(
            str(item.get("status")), "modified"
        )
        return ProviderFileChange(
            path=item["filename"],
            previous_path=item.get("previous_filename"),
            change_type=status,
            additions=int(item.get("additions", 0)),
            deletions=int(item.get("deletions", 0)),
            patch=patch[:_MAX_PATCH_CHARS] if isinstance(patch, str) else None,
        )

    @staticmethod
    def _commit(item: dict[str, Any], max_files: int) -> ProviderCommit:
        commit = item["commit"]
        author = commit.get("author") or {}
        stats = item.get("stats") or {}
        return ProviderCommit(
            sha=item["sha"],
            message=commit["message"],
            author_name=author.get("name"),
            author_email=author.get("email"),
            committed_at=_timestamp(author.get("date")) or datetime.now(UTC),
            parents=[parent["sha"] for parent in item.get("parents", [])],
            additions=int(stats.get("additions", 0)),
            deletions=int(stats.get("deletions", 0)),
            url=item.get("html_url", ""),
            files=[GitHubProvider._file_change(entry) for entry in item.get("files", [])][
                :max_files
            ],
        )

    # -- RepositoryProvider ---------------------------------------------------------------
    def get_installation(self, installation_id: int) -> ProviderInstallation:
        response = self._send(
            "GET",
            f"/app/installations/{int(installation_id)}",
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
        )
        body = response.json()
        account = body.get("account") or {}
        return ProviderInstallation(
            id=body["id"],
            account_login=account.get("login", "unknown"),
            account_type=account.get("type", "Organization"),
            repository_selection=body.get("repository_selection", "selected"),
            permissions={str(k): str(v) for k, v in (body.get("permissions") or {}).items()},
            suspended=body.get("suspended_at") is not None,
            installed_at=_timestamp(body.get("created_at")) or datetime.now(UTC),
        )

    def list_installation_repositories(self, installation_id: int) -> list[ProviderRepository]:
        repositories: list[ProviderRepository] = []
        for page in range(1, _MAX_PAGES + 1):
            response = self._get(
                installation_id, "/installation/repositories", {"per_page": 100, "page": page}
            )
            items = response.json().get("repositories", [])
            repositories.extend(self._repository(item) for item in items)
            if len(items) < 100:
                break
        return repositories

    def get_repository(self, installation_id: int, owner: str, name: str) -> ProviderRepository:
        return self._repository(self._get(installation_id, self._repo_path(owner, name)).json())

    def get_branch(
        self, installation_id: int, owner: str, name: str, branch: str
    ) -> ProviderBranch:
        path = f"{self._repo_path(owner, name)}/branches/{quote(branch, safe='')}"
        body = self._get(installation_id, path).json()
        return ProviderBranch(name=body["name"], head_sha=body["commit"]["sha"])

    def list_commits(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        since: datetime | None,
        limit: int,
    ) -> list[ProviderCommit]:
        params: dict[str, Any] = {"sha": branch, "per_page": min(limit, 100)}
        if since is not None:
            params["since"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        items = self._get(installation_id, f"{self._repo_path(owner, name)}/commits", params).json()
        return [self._commit(item, 0) for item in items[:limit]]

    def get_commit(
        self, installation_id: int, owner: str, name: str, sha: str, max_files: int
    ) -> ProviderCommit:
        path = f"{self._repo_path(owner, name)}/commits/{_validated_sha(sha)}"
        return self._commit(self._get(installation_id, path).json(), max_files)

    def get_pull_request(
        self, installation_id: int, owner: str, name: str, number: int, max_files: int
    ) -> ProviderPullRequest:
        base = f"{self._repo_path(owner, name)}/pulls/{int(number)}"
        body = self._get(installation_id, base).json()
        files = self._get(installation_id, f"{base}/files", {"per_page": min(max_files, 100)})
        return self._pull(body, [self._file_change(entry) for entry in files.json()][:max_files])

    @staticmethod
    def _pull(body: dict[str, Any], files: list[ProviderFileChange]) -> ProviderPullRequest:
        return ProviderPullRequest(
            number=body["number"],
            title=body["title"],
            state=body["state"],
            merged=bool(body.get("merged", body.get("merged_at") is not None)),
            author_login=(body.get("user") or {}).get("login"),
            base_branch=body["base"]["ref"],
            head_sha=body["head"]["sha"],
            url=body.get("html_url", ""),
            opened_at=_timestamp(body.get("created_at")) or datetime.now(UTC),
            closed_at=_timestamp(body.get("closed_at")),
            merged_at=_timestamp(body.get("merged_at")),
            files=files,
        )

    def list_pull_requests(
        self, installation_id: int, owner: str, name: str, limit: int
    ) -> list[ProviderPullRequest]:
        items = self._get(
            installation_id,
            f"{self._repo_path(owner, name)}/pulls",
            {"state": "all", "sort": "updated", "direction": "desc", "per_page": min(limit, 50)},
        ).json()
        return [self._pull(item, []) for item in items[:limit]]

    def list_deployments(
        self, installation_id: int, owner: str, name: str, limit: int
    ) -> list[ProviderDeployment]:
        base = f"{self._repo_path(owner, name)}/deployments"
        items = self._get(installation_id, base, {"per_page": min(limit, 30)}).json()
        deployments: list[ProviderDeployment] = []
        for item in items[:limit]:
            statuses = self._get(
                installation_id, f"{base}/{int(item['id'])}/statuses", {"per_page": 5}
            )
            deployments.append(
                ProviderDeployment(
                    id=item["id"],
                    sha=item["sha"],
                    environment=item.get("environment") or "unknown",
                    ref=item.get("ref"),
                    description=item.get("description"),
                    created_at=_timestamp(item.get("created_at")) or datetime.now(UTC),
                    statuses=[
                        ProviderDeploymentStatus(
                            state=status["state"],
                            description=status.get("description"),
                            created_at=_timestamp(status.get("created_at")) or datetime.now(UTC),
                        )
                        for status in statuses.json()
                    ],
                )
            )
        return deployments

    def get_tree(
        self, installation_id: int, owner: str, name: str, sha: str, max_entries: int
    ) -> ProviderTree:
        path = f"{self._repo_path(owner, name)}/git/trees/{_validated_sha(sha)}"
        body = self._get(installation_id, path, {"recursive": "1"}).json()
        entries: list[ProviderTreeEntry] = []
        for item in body.get("tree", []):
            if item["type"] == "tree":
                continue
            if item["type"] == "commit":
                kind = "submodule"
            elif item.get("mode") == "120000":
                kind = "symlink"
            else:
                kind = "blob"
            entries.append(
                ProviderTreeEntry(path=item["path"], kind=kind, size=int(item.get("size", 0)))
            )
        truncated = bool(body.get("truncated")) or len(entries) > max_entries
        return ProviderTree(entries=entries[:max_entries], truncated=truncated)

    def get_file_content(
        self, installation_id: int, owner: str, name: str, path: str, sha: str, max_bytes: int
    ) -> bytes:
        segments = [segment for segment in path.split("/") if segment]
        if not segments or any(segment in {".", ".."} for segment in segments) or "\\" in path:
            raise ProviderError("invalid repository path")
        encoded = "/".join(quote(segment, safe="") for segment in segments)
        token = self._installation_token(installation_id)
        url = f"{self._repo_path(owner, name)}/contents/{encoded}"
        try:
            with self._client.stream(
                "GET",
                url,
                params={"ref": _validated_sha(sha)},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.raw+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            ) as response:
                self._record_rate_limit(response)
                if response.status_code >= 400:
                    response.read()
                    self._raise_for_status(response)
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ProviderNotFoundError("file exceeds the requested byte bound")
                return bytes(content)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError("GitHub request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"GitHub request failed: {type(exc).__name__}") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        retry_after = response.headers.get("retry-after")
        exhausted = response.headers.get("x-ratelimit-remaining") == "0"
        if status == 429 or (status == 403 and (retry_after or exhausted)):
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 60
            raise ProviderRateLimitError("GitHub rate limit reached", wait)
        if status in {401, 403}:
            raise ProviderAuthenticationError(f"GitHub rejected the request ({status})")
        if status in {404, 410}:
            raise ProviderNotFoundError("GitHub resource not found")
        if status >= 500:
            raise ProviderUnavailableError(f"GitHub is unavailable ({status})")
        raise ProviderError(f"GitHub rejected the request ({status})")

    def last_rate_limit(self) -> RateLimitInfo | None:
        return self._rate_limit
