"""Immutable checkout of one repository revision, read through the Module 4 read-only provider.

Files are fetched by blob (never via ``git clone``), so no credentials are ever written into a
Git remote or config, and no repository hook, submodule, or filter can execute. Symlinks and
submodules are never materialized.
"""

import hashlib
from dataclasses import dataclass, field

from opspilot_api.config import Settings
from opspilot_api.repositories.policy import safe_repository_path
from opspilot_api.repositories.provider import (
    ProviderError,
    ProviderNotFoundError,
    RepositoryProvider,
)

_SKIPPED_DIRECTORIES = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache"}


class CheckoutError(Exception):
    code = "checkout_failed"


class CheckoutTooLargeError(CheckoutError):
    code = "checkout_too_large"


class UnsafeTreeError(CheckoutError):
    code = "unsafe_tree_entry"


@dataclass
class Checkout:
    owner: str
    name: str
    commit_sha: str
    files: dict[str, bytes]
    tree_hash: str
    skipped_symlinks: list[str] = field(default_factory=list)
    skipped_submodules: list[str] = field(default_factory=list)
    skipped_other: int = 0


def compute_tree_hash(files: dict[str, bytes]) -> str:
    """Stable hash over sorted (path, content-hash) pairs; verifies reproducible checkouts."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(files[path]).digest())
    return digest.hexdigest()


def fetch_checkout(
    settings: Settings,
    provider: RepositoryProvider,
    installation_id: int,
    owner: str,
    name: str,
    commit_sha: str,
) -> Checkout:
    """Read the exact revision's tree. The commit must exist in *this* repository."""
    try:
        provider.get_commit(installation_id, owner, name, commit_sha, 1)
        tree = provider.get_tree(installation_id, owner, name, commit_sha, settings.REPO_MAX_FILES)
    except ProviderNotFoundError as exc:
        raise CheckoutError("base commit does not exist in the repository") from exc
    except ProviderError as exc:
        raise CheckoutError(f"repository provider failed: {exc.code}") from exc
    if tree.truncated:
        raise CheckoutTooLargeError("repository tree exceeds the checkout file limit")
    files: dict[str, bytes] = {}
    checkout = Checkout(owner, name, commit_sha, files, "")
    total = 0
    for entry in tree.entries:
        if not safe_repository_path(entry.path):
            raise UnsafeTreeError("repository tree contains an unsafe path")
        if entry.kind == "symlink":
            checkout.skipped_symlinks.append(entry.path)
            continue
        if entry.kind == "submodule":
            checkout.skipped_submodules.append(entry.path)
            continue
        parts = entry.path.split("/")
        if entry.kind != "blob" or any(part in _SKIPPED_DIRECTORIES for part in parts[:-1]):
            checkout.skipped_other += 1
            continue
        if entry.size > settings.REPO_MAX_FILE_BYTES:
            checkout.skipped_other += 1
            continue
        total += entry.size
        if total > settings.REPO_MAX_TOTAL_BYTES:
            raise CheckoutTooLargeError("repository checkout exceeds the byte limit")
        try:
            files[entry.path] = provider.get_file_content(
                installation_id,
                owner,
                name,
                entry.path,
                commit_sha,
                settings.REPO_MAX_FILE_BYTES,
            )
        except ProviderNotFoundError:
            checkout.skipped_other += 1
        except ProviderError as exc:
            raise CheckoutError(f"repository provider failed: {exc.code}") from exc
    checkout.tree_hash = compute_tree_hash(files)
    return checkout


def verify_checkout(checkout: Checkout) -> None:
    """Fail closed if the in-memory checkout no longer matches the recorded tree hash."""
    if compute_tree_hash(checkout.files) != checkout.tree_hash:
        raise CheckoutError("checkout tree hash mismatch")
