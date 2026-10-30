"""Local fixture provider: the same domain responses as GitHub, from committed synthetic data.

Used by CI, automated tests, and the credential-free local demo. It performs no network I/O.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opspilot_api.repositories.provider import (
    ProviderAuthenticationError,
    ProviderBranch,
    ProviderCommit,
    ProviderDeployment,
    ProviderDeploymentStatus,
    ProviderFileChange,
    ProviderInstallation,
    ProviderNotFoundError,
    ProviderPullRequest,
    ProviderRepository,
    ProviderTree,
    ProviderTreeEntry,
    RateLimitInfo,
    RepositoryProvider,
)

DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "demo_repositories.json"
# The planted incident window in the fixture is centered on this moment.
FIXTURE_ANCHOR = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
# Key/certificate headers are expanded at load time so the committed JSON holds no PEM blocks.
_PLACEHOLDERS = {
    "@@PEM_BEGIN@@": "-----BEGIN RSA PRIVATE KEY-----",
    "@@PEM_END@@": "-----END RSA PRIVATE KEY-----",
    "@@CERT_BEGIN@@": "-----BEGIN CERTIFICATE-----",
    "@@CERT_END@@": "-----END CERTIFICATE-----",
}


def _expand(text: str) -> str:
    for token, value in _PLACEHOLDERS.items():
        text = text.replace(token, value)
    return text


class FixtureProvider(RepositoryProvider):
    name = "fixture"

    def __init__(
        self,
        path: Path | None = None,
        data: dict[str, Any] | None = None,
        shift: timedelta = timedelta(0),
    ) -> None:
        self.shift = shift
        if data is None:
            data = json.loads((path or DEFAULT_FIXTURE).read_text(encoding="utf-8"))
        self.data: dict[str, Any] = data
        self.calls: list[str] = []
        # Tests set this to simulate provider failures (rate limit, authentication, outage).
        self.fail_with: Exception | None = None

    # -- lookup helpers -------------------------------------------------------------------
    def _begin(self, method: str) -> None:
        self.calls.append(method)
        if self.fail_with is not None:
            raise self.fail_with

    def _installation(self, installation_id: int) -> dict[str, Any]:
        for item in self.data["installations"]:
            if item["id"] == installation_id:
                return dict(item)
        raise ProviderNotFoundError("installation not found")

    def _repo(self, installation_id: int, owner: str, name: str) -> dict[str, Any]:
        installation = self._installation(installation_id)
        if installation.get("suspended"):
            raise ProviderAuthenticationError("installation is suspended")
        for repo in installation["repositories"]:
            if repo["owner"].lower() == owner.lower() and repo["name"].lower() == name.lower():
                return dict(repo)
        raise ProviderNotFoundError("repository not found for installation")

    def _time(self, value: str) -> datetime:
        return datetime.fromisoformat(value) + self.shift

    @staticmethod
    def _sorted_commits(repo: dict[str, Any]) -> list[dict[str, Any]]:
        return sorted(
            repo["commits"], key=lambda item: (item["committed_at"], item["sha"]), reverse=True
        )

    @staticmethod
    def _convert_repo(repo: dict[str, Any], owner: str | None = None) -> ProviderRepository:
        return ProviderRepository(
            id=repo["id"],
            owner=repo["owner"],
            name=repo["name"],
            full_name=f"{repo['owner']}/{repo['name']}",
            default_branch=repo["default_branch"],
            private=repo["private"],
            archived=repo.get("archived", False),
        )

    def _convert_commit(
        self, item: dict[str, Any], max_files: int, with_files: bool
    ) -> ProviderCommit:
        files = [ProviderFileChange(**entry) for entry in item.get("files", [])][:max_files]
        return ProviderCommit(
            sha=item["sha"],
            message=item["message"],
            author_name=item.get("author_name"),
            author_email=item.get("author_email"),
            committed_at=self._time(item["committed_at"]),
            parents=list(item.get("parents", [])),
            additions=sum(entry["additions"] for entry in item.get("files", [])),
            deletions=sum(entry["deletions"] for entry in item.get("files", [])),
            url=f"https://github.com/fixture/commit/{item['sha']}",
            files=files if with_files else [],
        )

    # -- RepositoryProvider ---------------------------------------------------------------
    def get_installation(self, installation_id: int) -> ProviderInstallation:
        self._begin("get_installation")
        item = self._installation(installation_id)
        return ProviderInstallation(
            id=item["id"],
            account_login=item["account_login"],
            account_type=item["account_type"],
            repository_selection=item["repository_selection"],
            permissions=item["permissions"],
            suspended=item.get("suspended", False),
            installed_at=self._time(item["installed_at"]),
        )

    def list_installation_repositories(self, installation_id: int) -> list[ProviderRepository]:
        self._begin("list_installation_repositories")
        item = self._installation(installation_id)
        if item.get("suspended"):
            raise ProviderAuthenticationError("installation is suspended")
        return [self._convert_repo(repo) for repo in item["repositories"]]

    def get_repository(self, installation_id: int, owner: str, name: str) -> ProviderRepository:
        self._begin("get_repository")
        return self._convert_repo(self._repo(installation_id, owner, name))

    def get_branch(
        self, installation_id: int, owner: str, name: str, branch: str
    ) -> ProviderBranch:
        self._begin("get_branch")
        repo = self._repo(installation_id, owner, name)
        if branch != repo["default_branch"]:
            raise ProviderNotFoundError("branch not found")
        commits = self._sorted_commits(repo)
        if not commits:
            raise ProviderNotFoundError("branch has no commits")
        return ProviderBranch(name=branch, head_sha=commits[0]["sha"])

    def list_commits(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        since: datetime | None,
        limit: int,
    ) -> list[ProviderCommit]:
        self._begin("list_commits")
        repo = self._repo(installation_id, owner, name)
        if branch != repo["default_branch"]:
            return []
        result = []
        for item in self._sorted_commits(repo):
            commit = self._convert_commit(item, 0, with_files=False)
            if since is not None and commit.committed_at < since:
                continue
            result.append(commit)
        return result[:limit]

    def get_commit(
        self, installation_id: int, owner: str, name: str, sha: str, max_files: int
    ) -> ProviderCommit:
        self._begin("get_commit")
        repo = self._repo(installation_id, owner, name)
        for item in repo["commits"]:
            if item["sha"] == sha:
                return self._convert_commit(item, max_files, with_files=True)
        raise ProviderNotFoundError("commit not found")

    def _convert_pull(self, item: dict[str, Any], max_files: int) -> ProviderPullRequest:
        return ProviderPullRequest(
            number=item["number"],
            title=item["title"],
            state=item["state"],
            merged=item["merged"],
            author_login=item.get("author_login"),
            base_branch=item["base_branch"],
            head_sha=item["head_sha"],
            url=item["url"],
            opened_at=self._time(item["opened_at"]),
            closed_at=self._time(item["closed_at"]) if item.get("closed_at") else None,
            merged_at=self._time(item["merged_at"]) if item.get("merged_at") else None,
            files=[ProviderFileChange(**entry) for entry in item.get("files", [])][:max_files],
        )

    def get_pull_request(
        self, installation_id: int, owner: str, name: str, number: int, max_files: int
    ) -> ProviderPullRequest:
        self._begin("get_pull_request")
        repo = self._repo(installation_id, owner, name)
        for item in repo.get("pull_requests", []):
            if item["number"] == number:
                return self._convert_pull(item, max_files)
        raise ProviderNotFoundError("pull request not found")

    def list_pull_requests(
        self, installation_id: int, owner: str, name: str, limit: int
    ) -> list[ProviderPullRequest]:
        self._begin("list_pull_requests")
        repo = self._repo(installation_id, owner, name)
        items = sorted(repo.get("pull_requests", []), key=lambda p: p["number"], reverse=True)
        return [self._convert_pull(item, 0) for item in items[:limit]]

    def list_deployments(
        self, installation_id: int, owner: str, name: str, limit: int
    ) -> list[ProviderDeployment]:
        self._begin("list_deployments")
        repo = self._repo(installation_id, owner, name)
        items = sorted(
            repo.get("deployments", []), key=lambda d: (d["created_at"], d["id"]), reverse=True
        )
        return [
            ProviderDeployment(
                id=item["id"],
                sha=item["sha"],
                environment=item["environment"],
                ref=item.get("ref"),
                description=item.get("description"),
                created_at=self._time(item["created_at"]),
                statuses=[
                    ProviderDeploymentStatus(
                        state=status["state"],
                        description=status.get("description"),
                        created_at=self._time(status["created_at"]),
                    )
                    for status in item.get("statuses", [])
                ],
            )
            for item in items[:limit]
        ]

    def get_tree(
        self, installation_id: int, owner: str, name: str, sha: str, max_entries: int
    ) -> ProviderTree:
        self._begin("get_tree")
        repo = self._repo(installation_id, owner, name)
        if not any(item["sha"] == sha for item in repo["commits"]):
            raise ProviderNotFoundError("commit not found")
        entries: list[ProviderTreeEntry] = []
        for path, spec in sorted(repo["files"].items()):
            if "symlink_target" in spec:
                entries.append(ProviderTreeEntry(path=path, kind="symlink"))
            elif spec.get("submodule"):
                entries.append(ProviderTreeEntry(path=path, kind="submodule"))
            else:
                entries.append(
                    ProviderTreeEntry(path=path, kind="blob", size=len(self._content(spec)))
                )
        return ProviderTree(entries=entries[:max_entries], truncated=len(entries) > max_entries)

    @staticmethod
    def _content(spec: dict[str, Any]) -> bytes:
        if "content_base64" in spec:
            return base64.b64decode(spec["content_base64"])
        return (_expand(spec["content"]) * int(spec.get("repeat", 1))).encode("utf-8")

    def get_file_content(
        self, installation_id: int, owner: str, name: str, path: str, sha: str, max_bytes: int
    ) -> bytes:
        self._begin("get_file_content")
        repo = self._repo(installation_id, owner, name)
        spec = repo["files"].get(path)
        if spec is None or "symlink_target" in spec or spec.get("submodule"):
            raise ProviderNotFoundError("file not found")
        content = self._content(spec)
        if len(content) > max_bytes:
            raise ProviderNotFoundError("file exceeds the requested byte bound")
        return content

    def last_rate_limit(self) -> RateLimitInfo | None:
        return RateLimitInfo(limit=5000, remaining=4999, reset_at=None)
