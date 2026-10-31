"""Immutable checkout: exact revision, no symlinks/submodules, bounded size, verified tree hash."""

import pytest

from opspilot_api.remediation.checkout import (
    CheckoutError,
    CheckoutTooLargeError,
    UnsafeTreeError,
    compute_tree_hash,
    fetch_checkout,
    verify_checkout,
)
from opspilot_api.repositories.fixture_provider import FixtureProvider
from opspilot_api.repositories.provider import ProviderTree, ProviderTreeEntry
from tests.conftest import make_settings


def demo() -> tuple[FixtureProvider, int, str, str, str]:
    provider = FixtureProvider()
    installation = provider.data["installations"][0]
    repo = installation["repositories"][0]
    head = provider.get_branch(installation["id"], repo["owner"], repo["name"], "main").head_sha
    return provider, installation["id"], repo["owner"], repo["name"], head


def test_checkout_never_materializes_symlinks_or_submodules() -> None:
    provider, installation, owner, name, head = demo()
    checkout = fetch_checkout(make_settings(), provider, installation, owner, name, head)
    assert "docs/link" in checkout.skipped_symlinks
    assert "vendor/sub" in checkout.skipped_submodules
    assert "docs/link" not in checkout.files and "vendor/sub" not in checkout.files
    assert "node_modules/leftpad/index.js" not in checkout.files
    assert checkout.commit_sha == head and checkout.files


def test_checkout_tree_hash_is_stable_and_verified() -> None:
    provider, installation, owner, name, head = demo()
    first = fetch_checkout(make_settings(), provider, installation, owner, name, head)
    second = fetch_checkout(make_settings(), provider, installation, owner, name, head)
    assert first.tree_hash == second.tree_hash == compute_tree_hash(first.files)
    verify_checkout(first)
    first.files["src/checkout/db.py"] = b"tampered"
    with pytest.raises(CheckoutError, match="hash mismatch"):
        verify_checkout(first)


def test_unknown_commit_for_the_repository_is_rejected() -> None:
    provider, installation, owner, name, _head = demo()
    with pytest.raises(CheckoutError, match="does not exist"):
        fetch_checkout(make_settings(), provider, installation, owner, name, "f" * 40)


def test_oversized_checkouts_fail_closed() -> None:
    provider, installation, owner, name, head = demo()
    with pytest.raises(CheckoutTooLargeError):
        fetch_checkout(
            make_settings(REPO_MAX_TOTAL_BYTES=2048), provider, installation, owner, name, head
        )


def test_path_traversal_in_tree_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, installation, owner, name, head = demo()
    monkeypatch.setattr(
        provider,
        "get_tree",
        lambda *_a, **_k: ProviderTree(
            entries=[ProviderTreeEntry(path="../evil.py", kind="blob", size=3)]
        ),
    )
    with pytest.raises(UnsafeTreeError):
        fetch_checkout(make_settings(), provider, installation, owner, name, head)


def test_truncated_tree_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, installation, owner, name, head = demo()
    monkeypatch.setattr(
        provider, "get_tree", lambda *_a, **_k: ProviderTree(entries=[], truncated=True)
    )
    with pytest.raises(CheckoutTooLargeError):
        fetch_checkout(make_settings(), provider, installation, owner, name, head)


def test_no_credentials_are_part_of_a_checkout() -> None:
    provider, installation, owner, name, head = demo()
    checkout = fetch_checkout(make_settings(), provider, installation, owner, name, head)
    assert not any(path.startswith(".git/") for path in checkout.files)
    assert not hasattr(checkout, "token") and not hasattr(checkout, "remote_url")
