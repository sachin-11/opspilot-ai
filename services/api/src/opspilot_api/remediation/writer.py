"""Narrow GitHub *write* provider: fix branch, one commit, one pull request, read PR status.

Deliberately absent: merge, force push, default-branch push, PR approval, branch-protection or
repository-settings changes, workflow dispatch, releases, and deployments. The abstract interface
below is the entire write surface; nothing else can be called from the remediation code.
"""

import hashlib
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from pydantic import BaseModel

from opspilot_api.config import Settings
from opspilot_api.repositories.fixture_provider import FixtureProvider
from opspilot_api.repositories.github_provider import GitHubProvider
from opspilot_api.repositories.provider import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderNotFoundError,
    ProviderUnavailableError,
)

# Permissions requested for the short-lived write token: contents + pull requests only.
WRITE_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
_BRANCH = re.compile(r"^[a-z0-9][a-z0-9/_.-]{3,200}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
PATCH_MARKER = "OpsPilot-Patch-Hash:"


class WritePermissionError(ProviderAuthenticationError):
    code = "write_permission_missing"


class BranchConflictError(ProviderError):
    code = "branch_conflict"


class UnsafeBranchError(ProviderError):
    code = "unsafe_branch"


class WritePullRequest(BaseModel):
    id: str
    number: int
    url: str
    state: str
    merged: bool = False
    head_branch: str
    head_sha: str | None = None


@dataclass(frozen=True)
class BranchResult:
    head_sha: str
    created: bool


def assert_fix_branch(branch: str, prefix: str, default_branch: str) -> None:
    """Provider-level guard that holds even if policy were bypassed."""
    if (
        not _BRANCH.match(branch)
        or not branch.startswith(prefix)
        or len(branch) <= len(prefix)
        or branch == default_branch
        or ".." in branch
        or branch.endswith((".lock", "/"))
    ):
        raise UnsafeBranchError("branch name is not an allowed OpsPilot fix branch")


def commit_message_for(title: str, patch_hash: str) -> str:
    return (
        f"{title[:120]}\n\n{PATCH_MARKER} {patch_hash}\nProposed by OpsPilot AI; not auto-merged."
    )


class WriteProvider(ABC):
    name: str

    @abstractmethod
    def verify_write_access(self, installation_id: int, owner: str, name: str) -> None:
        """Raise :class:`WritePermissionError` unless contents and PR write are granted."""

    @abstractmethod
    def get_branch_head(
        self, installation_id: int, owner: str, name: str, branch: str
    ) -> str | None: ...

    @abstractmethod
    def create_branch(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        base_sha: str,
        default_branch: str,
        prefix: str,
    ) -> BranchResult: ...

    @abstractmethod
    def create_commit(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        base_sha: str,
        changes: Mapping[str, str | None],
        message: str,
        patch_hash: str,
    ) -> str: ...

    @abstractmethod
    def find_open_pull_request(
        self, installation_id: int, owner: str, name: str, head_branch: str
    ) -> WritePullRequest | None: ...

    @abstractmethod
    def open_pull_request(
        self,
        installation_id: int,
        owner: str,
        name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> WritePullRequest: ...

    @abstractmethod
    def get_pull_request_status(
        self, installation_id: int, owner: str, name: str, number: int
    ) -> WritePullRequest: ...


class FixtureWriteProvider(WriteProvider):
    """In-memory write provider for CI and the credential-free demo. No network I/O."""

    name = "fixture"

    def __init__(
        self,
        read_provider: FixtureProvider,
        granted: dict[str, str] | None = None,
    ) -> None:
        self.read = read_provider
        self.granted = (
            {"contents": "write", "pull_requests": "write"} if granted is None else granted
        )
        self.branches: dict[tuple[str, str, str], dict[str, str]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.pulls: dict[tuple[str, str], list[WritePullRequest]] = {}
        self.calls: list[str] = []
        self.fail_with: Exception | None = None

    def _begin(self, method: str) -> None:
        self.calls.append(method)
        if self.fail_with is not None:
            raise self.fail_with

    def verify_write_access(self, installation_id: int, owner: str, name: str) -> None:
        self._begin("verify_write_access")
        self.read.get_repository(installation_id, owner, name)
        permissions = {**self.read.get_installation(installation_id).permissions, **self.granted}
        if permissions.get("contents") != "write" or permissions.get("pull_requests") != "write":
            raise WritePermissionError("installation lacks contents/pull_requests write access")

    def get_branch_head(
        self, installation_id: int, owner: str, name: str, branch: str
    ) -> str | None:
        self._begin("get_branch_head")
        entry = self.branches.get((owner, name, branch))
        return entry["head"] if entry else None

    def create_branch(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        base_sha: str,
        default_branch: str,
        prefix: str,
    ) -> BranchResult:
        self._begin("create_branch")
        assert_fix_branch(branch, prefix, default_branch)
        existing = self.branches.get((owner, name, branch))
        if existing is not None:
            return BranchResult(existing["head"], created=False)
        self.read.get_commit(installation_id, owner, name, base_sha, 1)
        self.branches[(owner, name, branch)] = {"head": base_sha, "base": base_sha}
        return BranchResult(base_sha, created=True)

    def create_commit(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        base_sha: str,
        changes: Mapping[str, str | None],
        message: str,
        patch_hash: str,
    ) -> str:
        self._begin("create_commit")
        entry = self.branches.get((owner, name, branch))
        if entry is None:
            raise ProviderNotFoundError("branch not found")
        sha = hashlib.sha1(f"{base_sha}:{patch_hash}".encode(), usedforsecurity=False).hexdigest()
        if entry["head"] == sha:
            return sha
        if entry["head"] != base_sha:
            raise BranchConflictError("branch head is not the approved base commit")
        self.commits[sha] = {
            "parent": base_sha,
            "changes": dict(changes),
            "message": message,
            "patch_hash": patch_hash,
        }
        entry["head"] = sha
        return sha

    def find_open_pull_request(
        self, installation_id: int, owner: str, name: str, head_branch: str
    ) -> WritePullRequest | None:
        self._begin("find_open_pull_request")
        for pull in self.pulls.get((owner, name), []):
            if pull.head_branch == head_branch and pull.state == "open":
                return pull
        return None

    def open_pull_request(
        self,
        installation_id: int,
        owner: str,
        name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> WritePullRequest:
        self._begin("open_pull_request")
        existing = self.find_open_pull_request(installation_id, owner, name, head_branch)
        if existing is not None:
            return existing
        pulls = self.pulls.setdefault((owner, name), [])
        number = 1000 + len(pulls) + 1
        pull = WritePullRequest(
            id=f"fixture-pr-{number}",
            number=number,
            url=f"https://github.com/{owner}/{name}/pull/{number}",
            state="open",
            head_branch=head_branch,
            head_sha=self.branches[(owner, name, head_branch)]["head"],
        )
        pulls.append(pull)
        return pull

    def get_pull_request_status(
        self, installation_id: int, owner: str, name: str, number: int
    ) -> WritePullRequest:
        self._begin("get_pull_request_status")
        for pull in self.pulls.get((owner, name), []):
            if pull.number == number:
                return pull
        raise ProviderNotFoundError("pull request not found")


class GitHubWriteProvider(WriteProvider):
    """GitHub REST implementation. Only GET/POST/PATCH on branches, git objects, and pulls."""

    name = "github"

    def __init__(self, settings: Settings, read_provider: GitHubProvider) -> None:
        self.settings = settings
        self._read = read_provider
        self._tokens: dict[int, tuple[str, float]] = {}

    @staticmethod
    def _path(owner: str, name: str) -> str:
        if not re.match(r"^[A-Za-z0-9_.-]{1,100}$", owner) or not re.match(
            r"^[A-Za-z0-9_.-]{1,100}$", name
        ):
            raise ProviderError("invalid repository identifier")
        return f"/repos/{owner}/{name}"

    def _token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached is not None and cached[1] - 60 > time.time():
            return cached[0]
        try:
            app_jwt = self._read._app_jwt()
        except jwt.PyJWTError as exc:
            raise ProviderAuthenticationError("GitHub App private key is invalid") from exc
        response = self._read._send(
            "POST",
            f"/app/installations/{int(installation_id)}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
            json={"permissions": WRITE_PERMISSIONS},
        )
        body = response.json()
        token = str(body["token"])
        self._tokens[installation_id] = (token, time.time() + 300)
        return token

    def _call(
        self,
        method: str,
        installation_id: int,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        if method not in {"GET", "POST", "PATCH"}:
            raise ProviderError("HTTP method not permitted for remediation writes")
        if "/merge" in path or ("/branches/" in path and path.endswith("/protection")):
            raise ProviderError("operation not permitted")
        return self._read._send(
            method,
            path,
            headers={
                "Authorization": f"Bearer {self._token(installation_id)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=json,
            params=params,
        )

    def verify_write_access(self, installation_id: int, owner: str, name: str) -> None:
        installation = self._read.get_installation(installation_id)
        permissions = installation.permissions
        if permissions.get("contents") != "write" or permissions.get("pull_requests") != "write":
            raise WritePermissionError("installation lacks contents/pull_requests write access")
        self._read.get_repository(installation_id, owner, name)

    def get_branch_head(
        self, installation_id: int, owner: str, name: str, branch: str
    ) -> str | None:
        try:
            response = self._call(
                "GET", installation_id, f"{self._path(owner, name)}/git/ref/heads/{branch}"
            )
        except ProviderNotFoundError:
            return None
        return str(response.json()["object"]["sha"])

    def create_branch(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        base_sha: str,
        default_branch: str,
        prefix: str,
    ) -> BranchResult:
        assert_fix_branch(branch, prefix, default_branch)
        if not _SHA.match(base_sha):
            raise ProviderError("invalid base commit")
        head = self.get_branch_head(installation_id, owner, name, branch)
        if head is not None:
            return BranchResult(head, created=False)
        self._call(
            "POST",
            installation_id,
            f"{self._path(owner, name)}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        return BranchResult(base_sha, created=True)

    def create_commit(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        base_sha: str,
        changes: Mapping[str, str | None],
        message: str,
        patch_hash: str,
    ) -> str:
        repo = self._path(owner, name)
        head = self.get_branch_head(installation_id, owner, name, branch)
        if head is None:
            raise ProviderNotFoundError("branch not found")
        if head != base_sha:
            existing = self._call("GET", installation_id, f"{repo}/git/commits/{head}").json()
            parents = [item["sha"] for item in existing.get("parents", [])]
            if parents == [base_sha] and f"{PATCH_MARKER} {patch_hash}" in existing.get(
                "message", ""
            ):
                return head
            raise BranchConflictError("branch head is not the approved base commit")
        base_commit = self._call("GET", installation_id, f"{repo}/git/commits/{base_sha}").json()
        entries: list[dict[str, Any]] = []
        for path, content in sorted(changes.items()):
            if content is None:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            else:
                entries.append({"path": path, "mode": "100644", "type": "blob", "content": content})
        tree = self._call(
            "POST",
            installation_id,
            f"{repo}/git/trees",
            json={"base_tree": base_commit["tree"]["sha"], "tree": entries},
        ).json()
        commit = self._call(
            "POST",
            installation_id,
            f"{repo}/git/commits",
            json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
        ).json()
        # Fast-forward of the fix branch we created; ``force`` is never set.
        self._call(
            "PATCH",
            installation_id,
            f"{repo}/git/refs/heads/{branch}",
            json={"sha": commit["sha"], "force": False},
        )
        return str(commit["sha"])

    @staticmethod
    def _pull(body: dict[str, Any]) -> WritePullRequest:
        return WritePullRequest(
            id=str(body["id"]),
            number=int(body["number"]),
            url=str(body["html_url"]),
            state=str(body["state"]),
            merged=bool(body.get("merged", False)),
            head_branch=str(body["head"]["ref"]),
            head_sha=body["head"].get("sha"),
        )

    def find_open_pull_request(
        self, installation_id: int, owner: str, name: str, head_branch: str
    ) -> WritePullRequest | None:
        response = self._call(
            "GET",
            installation_id,
            f"{self._path(owner, name)}/pulls",
            params={"head": f"{owner}:{head_branch}", "state": "open", "per_page": 5},
        )
        items = response.json()
        return self._pull(items[0]) if items else None

    def open_pull_request(
        self,
        installation_id: int,
        owner: str,
        name: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> WritePullRequest:
        existing = self.find_open_pull_request(installation_id, owner, name, head_branch)
        if existing is not None:
            return existing
        response = self._call(
            "POST",
            installation_id,
            f"{self._path(owner, name)}/pulls",
            json={
                "title": title[:240],
                "head": head_branch,
                "base": base_branch,
                "body": body,
                "maintainer_can_modify": False,
                "draft": False,
            },
        )
        return self._pull(response.json())

    def get_pull_request_status(
        self, installation_id: int, owner: str, name: str, number: int
    ) -> WritePullRequest:
        response = self._call(
            "GET", installation_id, f"{self._path(owner, name)}/pulls/{int(number)}"
        )
        return self._pull(response.json())


_WRITERS: dict[str, WriteProvider] = {}


def get_write_provider(settings: Settings, read_provider: object) -> WriteProvider:
    """One write provider per process; fixture mode never touches the network."""
    key = f"{settings.REMEDIATION_WRITE_PROVIDER}:{settings.GITHUB_PROVIDER}"
    writer = _WRITERS.get(key)
    if writer is None:
        if settings.REMEDIATION_WRITE_PROVIDER == "github":
            if not isinstance(read_provider, GitHubProvider):
                raise ProviderUnavailableError("GitHub write requires GITHUB_PROVIDER=github")
            writer = GitHubWriteProvider(settings, read_provider)
        else:
            if not isinstance(read_provider, FixtureProvider):
                raise ProviderUnavailableError("fixture writes require GITHUB_PROVIDER=fixture")
            writer = FixtureWriteProvider(read_provider)
        _WRITERS[key] = writer
    return writer


def write_enabled_for(settings: Settings, workspace_id: object, full_name: str) -> bool:
    """Writes are off by default and require explicit workspace + repository enablement."""
    if not settings.REMEDIATION_GITHUB_WRITE_ENABLED:
        return False
    return f"{workspace_id}:{full_name}".lower() in {
        item.lower() for item in settings.REMEDIATION_WRITE_ENABLED_REPOSITORIES
    }
