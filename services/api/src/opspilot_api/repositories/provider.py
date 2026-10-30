"""Typed, read-only repository-provider interface and provider-neutral domain responses.

Application, worker, and LangGraph code depend on this module only; GitHub SDK/HTTP response
shapes never leave ``github_provider``. Every method is a read operation.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field


class ProviderError(Exception):
    code = "provider_error"
    retryable = False


class ProviderAuthenticationError(ProviderError):
    code = "provider_authentication_failed"


class ProviderNotFoundError(ProviderError):
    code = "provider_not_found"


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limited"
    retryable = True

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, retry_after_seconds)


class RateLimitInfo(BaseModel):
    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None


class ProviderInstallation(BaseModel):
    id: int
    account_login: str
    account_type: str
    repository_selection: str
    permissions: dict[str, str] = Field(default_factory=dict)
    suspended: bool = False
    installed_at: datetime


class ProviderRepository(BaseModel):
    id: int
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool
    archived: bool = False


class ProviderBranch(BaseModel):
    name: str
    head_sha: str


class ProviderFileChange(BaseModel):
    path: str
    previous_path: str | None = None
    change_type: str
    additions: int = 0
    deletions: int = 0
    patch: str | None = None


class ProviderCommit(BaseModel):
    sha: str
    message: str
    author_name: str | None = None
    author_email: str | None = None
    committed_at: datetime
    parents: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    url: str
    files: list[ProviderFileChange] = Field(default_factory=list)


class ProviderPullRequest(BaseModel):
    number: int
    title: str
    state: str
    merged: bool
    author_login: str | None = None
    base_branch: str
    head_sha: str
    url: str
    opened_at: datetime
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    files: list[ProviderFileChange] = Field(default_factory=list)


class ProviderDeploymentStatus(BaseModel):
    state: str
    description: str | None = None
    created_at: datetime


class ProviderDeployment(BaseModel):
    id: int
    sha: str
    environment: str
    ref: str | None = None
    description: str | None = None
    created_at: datetime
    statuses: list[ProviderDeploymentStatus] = Field(default_factory=list)


class ProviderTreeEntry(BaseModel):
    path: str
    # blob | tree | submodule | symlink
    kind: str
    size: int = 0


class ProviderTree(BaseModel):
    entries: list[ProviderTreeEntry]
    truncated: bool = False


class RepositoryProvider(ABC):
    """Read-only access to one provider. Implementations must never mutate remote state."""

    name: str

    @abstractmethod
    def get_installation(self, installation_id: int) -> ProviderInstallation: ...

    @abstractmethod
    def list_installation_repositories(self, installation_id: int) -> list[ProviderRepository]: ...

    @abstractmethod
    def get_repository(self, installation_id: int, owner: str, name: str) -> ProviderRepository: ...

    @abstractmethod
    def get_branch(
        self, installation_id: int, owner: str, name: str, branch: str
    ) -> ProviderBranch: ...

    @abstractmethod
    def list_commits(
        self,
        installation_id: int,
        owner: str,
        name: str,
        branch: str,
        since: datetime | None,
        limit: int,
    ) -> list[ProviderCommit]:
        """Commit metadata without diffs, newest first."""

    @abstractmethod
    def get_commit(
        self, installation_id: int, owner: str, name: str, sha: str, max_files: int
    ) -> ProviderCommit:
        """One commit with at most ``max_files`` changed-file entries (patches provider-bounded)."""

    @abstractmethod
    def get_pull_request(
        self, installation_id: int, owner: str, name: str, number: int, max_files: int
    ) -> ProviderPullRequest: ...

    @abstractmethod
    def list_pull_requests(
        self, installation_id: int, owner: str, name: str, limit: int
    ) -> list[ProviderPullRequest]:
        """Recently updated pull requests (metadata only, without changed files)."""

    @abstractmethod
    def list_deployments(
        self, installation_id: int, owner: str, name: str, limit: int
    ) -> list[ProviderDeployment]: ...

    @abstractmethod
    def get_tree(
        self, installation_id: int, owner: str, name: str, sha: str, max_entries: int
    ) -> ProviderTree: ...

    @abstractmethod
    def get_file_content(
        self, installation_id: int, owner: str, name: str, path: str, sha: str, max_bytes: int
    ) -> bytes: ...

    @abstractmethod
    def last_rate_limit(self) -> RateLimitInfo | None:
        """Rate-limit headers from the most recent call, if any (never credentials)."""
