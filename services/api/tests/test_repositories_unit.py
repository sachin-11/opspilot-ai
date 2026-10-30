import hashlib
import hmac
import inspect
import json
import re
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from opspilot_api.config import Settings
from opspilot_api.repositories import github_provider
from opspilot_api.repositories.code_chunking import chunk_file
from opspilot_api.repositories.fixture_provider import FixtureProvider
from opspilot_api.repositories.github_provider import READ_ONLY_PERMISSIONS, GitHubProvider
from opspilot_api.repositories.policy import (
    decode_text,
    evaluate_path,
    is_generated,
    redact_code,
    safe_repository_path,
)
from opspilot_api.repositories.provider import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    RepositoryProvider,
)
from opspilot_api.repositories.webhooks import normalize_event, verify_signature

from .conftest import make_settings

SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature_valid_invalid_and_missing() -> None:
    body = b'{"zen": "ok"}'
    assert verify_signature(SECRET, body, _sign(body))
    assert not verify_signature(SECRET, body, _sign(body, "other-secret"))
    assert not verify_signature(SECRET, body + b" ", _sign(body))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature(SECRET, body, _sign(body).removeprefix("sha256="))
    assert not verify_signature(SECRET, body, "sha1=" + "0" * 40)


def test_normalize_event_selects_only_documented_fields() -> None:
    event = normalize_event(
        "push",
        {
            "ref": "refs/heads/main",
            "after": "a" * 40,
            "before": "b" * 40,
            "commits": [{"id": "c" * 40, "message": "never stored"}],
            "repository": {"id": 7, "full_name": "acme/x"},
            "installation": {"id": 99},
            "sender": {"login": "ignored"},
        },
    )
    assert event.supported and event.installation_id == 99 and event.repository_id == 7
    assert event.fields["branch"] == "main"
    assert event.fields["commit_shas"] == ["c" * 40]
    assert "message" not in json.dumps(event.fields)


@pytest.mark.parametrize(
    ("event_type", "payload", "reason"),
    [
        ("ping", {"zen": "x"}, "unsupported_event"),
        ("issues", {"action": "opened"}, "unsupported_event"),
        ("installation", {"action": "new_permissions_accepted"}, "unsupported_action"),
        ("pull_request", {"action": "labeled", "number": 1}, "unsupported_action"),
        ("pull_request", {"action": "opened"}, "malformed_payload"),
        ("deployment", {"deployment": {}}, "malformed_payload"),
    ],
)
def test_unsupported_or_malformed_events_are_marked_ignored(
    event_type: str, payload: dict[str, object], reason: str
) -> None:
    event = normalize_event(event_type, payload)
    assert not event.supported
    assert event.ignore_reason == reason


def test_deployment_status_event_is_normalized() -> None:
    event = normalize_event(
        "deployment_status",
        {
            "deployment": {"id": 5, "sha": "d" * 40, "environment": "production"},
            "deployment_status": {"state": "success", "created_at": "2026-03-10T11:00:00Z"},
            "repository": {"id": 1},
            "installation": {"id": 2},
        },
    )
    assert event.supported and event.fields["state"] == "success"
    assert event.fields["deployment_id"] == 5


@pytest.mark.parametrize(
    ("path", "kind", "size", "included", "reason"),
    [
        ("src/app.py", "blob", 100, True, "allowed"),
        ("web/index.tsx", "blob", 100, True, "allowed"),
        ("Dockerfile", "blob", 100, True, "allowed"),
        ("infra/main.tf", "blob", 100, True, "allowed"),
        (".env", "blob", 10, False, "environment_file"),
        (".env.production", "blob", 10, False, "environment_file"),
        ("deploy/server.pem", "blob", 10, False, "denied_suffix"),
        ("deploy/id_rsa", "blob", 10, False, "lockfile_or_credential"),
        ("package-lock.json", "blob", 10, False, "lockfile_or_credential"),
        ("uv.lock", "blob", 10, False, "lockfile_or_credential"),
        ("node_modules/x/index.js", "blob", 10, False, "denied_directory"),
        ("dist/app.js", "blob", 10, False, "denied_directory"),
        ("static/app.min.js", "blob", 10, False, "denied_suffix"),
        ("static/app.js.map", "blob", 10, False, "denied_suffix"),
        ("assets/logo.png", "blob", 10, False, "denied_suffix"),
        ("data/dump.csv", "blob", 10, False, "denied_suffix"),
        ("src/app.py", "blob", 10_000_000, False, "too_large"),
        ("src/empty.py", "blob", 0, False, "empty"),
        ("docs/link", "symlink", 10, False, "symlink"),
        ("vendor/sub", "submodule", 10, False, "submodule"),
        ("notes.xyz", "blob", 10, False, "unsupported_type"),
        ("a/../b.py", "blob", 10, False, "unsafe_path"),
        ("/etc/passwd", "blob", 10, False, "unsafe_path"),
    ],
)
def test_indexing_policy(path: str, kind: str, size: int, included: bool, reason: str) -> None:
    decision = evaluate_path(path, kind, size, 262_144)
    assert (decision.include, decision.reason) == (included, reason)


def test_path_safety_binary_and_generated_detection() -> None:
    assert safe_repository_path("src/a.py")
    assert not safe_repository_path("a\\b.py")
    assert not safe_repository_path("a/./b.py")
    assert decode_text(b"hello\n") == "hello\n"
    assert decode_text(b"abc\x00def") is None
    assert decode_text(b"\xff\xfe\xfa") is None
    assert is_generated("# Generated by the protocol buffer compiler.  DO NOT EDIT!\n")
    assert not is_generated("def handwritten():\n    pass\n")


def test_redaction_removes_secrets_and_preserves_line_count() -> None:
    private_key = "-----BEGIN RSA PRIVATE KEY-----"
    text = "\n".join(
        [
            'DATABASE_URL = "postgresql://user:hunter2secret@db:5432/app"',
            'API_KEY = "sk-fakefakefakefakefakefake"',
            'TOKEN = "ghp_FAKEFAKEFAKEFAKEFAKEFAKE"',
            "aws = AKIAABCDEFGHIJKLMNOP",
            "password: hunter22",
            "Authorization: Bearer abcdef123456789",
            private_key,
            "MIIEfakekeymaterial",
            "-----END RSA PRIVATE KEY-----",
            "contact me@example.com",
            "max_tokens = 1000",
            "token_count = compute()",
        ]
    )
    redacted = redact_code(text)
    assert redacted.count("\n") == text.count("\n")
    for secret in ("hunter2secret", "sk-fake", "ghp_FAKE", "AKIAABC", "hunter22", "abcdef12345"):
        assert secret not in redacted
    assert "MIIEfake" not in redacted and "me@example.com" not in redacted
    assert "max_tokens = 1000" in redacted and "token_count = compute()" in redacted


PYTHON_SOURCE = '''"""Module docstring."""

import os

LIMIT = 3


def first(value: int) -> int:
    """First function."""
    return value + LIMIT


class Worker:
    def run(self) -> str:
        return "run"

    def stop(self) -> str:
        return "stop"


async def last() -> None:
    return None
'''


def test_python_chunking_is_symbol_aware_exact_and_deterministic() -> None:
    first = chunk_file("pkg/mod.py", "python", PYTHON_SOURCE, 80, 5)
    second = chunk_file("pkg/mod.py", "python", PYTHON_SOURCE, 80, 5)
    assert [c.content_hash for c in first.chunks] == [c.content_hash for c in second.chunks]
    names = [(c.symbol_name, c.symbol_type) for c in first.chunks]
    assert ("first", "function") in names and ("Worker", "class") in names
    assert ("last", "function") in names and (None, "module") in names
    lines = PYTHON_SOURCE.split("\n")
    for chunk in first.chunks:
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
    worker = next(c for c in first.chunks if c.symbol_name == "Worker")
    assert lines[worker.start_line - 1].startswith("class Worker")


def test_oversized_symbol_is_split_with_bounded_overlap_and_methods_extracted() -> None:
    body = "\n".join(f"    value_{i} = {i}" for i in range(40))
    source = f"def big():\n{body}\n"
    result = chunk_file("big.py", "python", source, 20, 4)
    assert len(result.chunks) > 1
    for previous, current in zip(result.chunks, result.chunks[1:], strict=False):
        assert current.start_line <= previous.end_line  # overlap only because a split occurred
        assert current.end_line - current.start_line + 1 <= 20
    methods = "\n".join(f"    def m{i}(self):\n        return {i}" for i in range(12))
    cls = chunk_file("cls.py", "python", f"class Big:\n{methods}\n", 10, 2)
    assert {c.symbol_name for c in cls.chunks} >= {"Big.m0", "Big.m11"}


def test_parse_failure_falls_back_to_line_chunks_and_is_flagged() -> None:
    result = chunk_file("broken.py", "python", "def broken(:\n    pass\n", 80, 5)
    assert result.parse_failed
    assert result.chunks and result.chunks[0].start_line == 1


def test_typescript_and_terraform_and_sql_symbols() -> None:
    ts = "export function load(): void {\n  return;\n}\n\nexport class Box {\n  x = 1;\n}\n"
    names = {c.symbol_name for c in chunk_file("a.ts", "typescript", ts, 80, 5).chunks}
    assert {"load", "Box"} <= names
    tf = 'resource "aws_db_instance" "main" {\n  size = 1\n}\n'
    assert (
        chunk_file("m.tf", "terraform", tf, 80, 5).chunks[0].symbol_name == "aws_db_instance.main"
    )
    sql = "CREATE TABLE orders (\n  id int\n);\n"
    assert chunk_file("m.sql", "sql", sql, 80, 5).chunks[0].symbol_name == "orders"


def _github_settings(**overrides: object) -> Settings:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return make_settings(
        GITHUB_PROVIDER="github",
        GITHUB_APP_ID=123,
        GITHUB_APP_PRIVATE_KEY=pem,
        GITHUB_WEBHOOK_SECRET=SECRET,
        **overrides,
    )


def _provider(handler: httpx.MockTransport) -> tuple[GitHubProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler.handle_request(request)

    client = httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(record))
    return GitHubProvider(_github_settings(), client=client, clock=lambda: 1_000_000.0), seen


def _ok_handler(extra: dict[str, httpx.Response] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "ghs_secret_installation_token",
                    "expires_at": "2026-03-10T12:00:00Z",
                },
            )
        if extra and path in extra:
            return extra[path]
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 1,
                            "name": "r",
                            "full_name": "o/r",
                            "owner": {"login": "o"},
                            "default_branch": "main",
                            "private": True,
                        }
                    ]
                },
                headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4990"},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(handler)


def test_github_provider_requests_read_only_token_once_and_records_rate_limit() -> None:
    provider, seen = _provider(_ok_handler())
    # Token expiry is in the past relative to the fixed clock, so each call re-exchanges;
    # use a far-future expiry clock to verify caching separately.
    repositories = provider.list_installation_repositories(55)
    assert repositories[0].full_name == "o/r"
    exchange = next(r for r in seen if r.url.path.endswith("/access_tokens"))
    assert exchange.method == "POST"
    assert json.loads(exchange.content) == {"permissions": READ_ONLY_PERMISSIONS}
    assert all(value == "read" for value in READ_ONLY_PERMISSIONS.values())
    assert exchange.headers["authorization"].startswith("Bearer ")
    info = provider.last_rate_limit()
    assert info is not None and info.remaining == 4990 and info.limit == 5000
    gets = [r for r in seen if r.url.path == "/installation/repositories"]
    assert gets[0].headers["authorization"] == "Bearer ghs_secret_installation_token"


def test_github_installation_token_is_cached_for_its_lifetime() -> None:
    settings = _github_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(200, json={"repositories": []})

    calls: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        return handler(request)

    client = httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(record))
    provider = GitHubProvider(settings, client=client)
    provider.list_installation_repositories(1)
    provider.list_installation_repositories(1)
    assert sum(1 for c in calls if c.startswith("POST")) == 1


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, json={}), ProviderAuthenticationError),
        (httpx.Response(403, json={}), ProviderAuthenticationError),
        (httpx.Response(404, json={}), ProviderNotFoundError),
        (httpx.Response(500, json={}), ProviderUnavailableError),
        (
            httpx.Response(
                403, json={}, headers={"x-ratelimit-remaining": "0", "retry-after": "42"}
            ),
            ProviderRateLimitError,
        ),
        (httpx.Response(429, json={}, headers={"retry-after": "7"}), ProviderRateLimitError),
    ],
)
def test_github_provider_maps_errors_without_leaking_credentials(
    response: httpx.Response, expected: type[ProviderError]
) -> None:
    provider, _ = _provider(_ok_handler({"/repos/o/r": response}))
    with pytest.raises(expected) as caught:
        provider.get_repository(1, "o", "r")
    message = str(caught.value)
    assert "ghs_secret_installation_token" not in message and "PRIVATE KEY" not in message
    if expected is ProviderRateLimitError:
        assert isinstance(caught.value, ProviderRateLimitError)
        assert caught.value.retry_after_seconds in {7, 42}


def test_github_provider_rejects_unsafe_identifiers_and_arbitrary_paths() -> None:
    provider, seen = _provider(_ok_handler())
    with pytest.raises(ProviderError):
        provider.get_repository(1, "o/../x", "r")
    with pytest.raises(ProviderError):
        provider.get_commit(1, "o", "r", "main; rm -rf /", 5)
    with pytest.raises(ProviderError):
        provider.get_file_content(1, "o", "r", "../../etc/passwd", "a" * 40, 100)
    assert not [r for r in seen if "etc/passwd" in str(r.url)]


def test_github_provider_requires_credentials() -> None:
    with pytest.raises(ValidationError):
        make_settings(GITHUB_PROVIDER="github")
    with pytest.raises(ValidationError, match="at least 16"):
        make_settings(GITHUB_WEBHOOK_SECRET="too-short")
    # Compose passes unset optional variables as empty strings; they must mean "unset".
    blank = make_settings(GITHUB_APP_ID="", GITHUB_WEBHOOK_SECRET="", GITHUB_APP_PRIVATE_KEY="")
    assert blank.GITHUB_APP_ID is None and blank.GITHUB_WEBHOOK_SECRET is None
    settings = make_settings(
        GITHUB_PROVIDER="github",
        GITHUB_APP_ID=1,
        GITHUB_WEBHOOK_SECRET=SECRET,
        GITHUB_APP_PRIVATE_KEY_FILE="/nonexistent/key.pem",
    )
    provider = GitHubProvider(settings, client=httpx.Client(base_url="https://x.test"))
    with pytest.raises(ProviderAuthenticationError):
        provider.get_installation(1)


def test_provider_interface_and_adapters_are_read_only() -> None:
    methods = {
        name
        for name, member in inspect.getmembers(RepositoryProvider, inspect.isfunction)
        if not name.startswith("_")
    }
    assert methods and all(name.startswith(("get_", "list_", "last_")) for name in methods)
    package = Path(github_provider.__file__).parent
    pattern = re.compile(r"""\.(put|patch|delete)\(|["'](PUT|PATCH|DELETE)["']""")
    # Everything that can reach GitHub (the local DB-mapping DELETE route is not one of these).
    for name in (
        "github_provider",
        "fixture_provider",
        "provider",
        "sync",
        "webhooks",
        "tools",
        "jobs",
        "queue",
        "retrieval",
        "correlation",
    ):
        source = package / f"{name}.py"
        assert not pattern.search(source.read_text(encoding="utf-8")), name
    assert "POST" in Path(github_provider.__file__).read_text(encoding="utf-8")
    text = Path(github_provider.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r'"POST"', text)) == 1  # only the installation-token exchange


def test_fixture_provider_matches_domain_shapes_and_supports_failure_injection() -> None:
    provider = FixtureProvider()
    installation = provider.get_installation(1001)
    assert installation.permissions["contents"] == "read"
    repositories = provider.list_installation_repositories(1001)
    assert {item.full_name for item in repositories} == {
        "acme/checkout-api",
        "acme/platform-monorepo",
    }
    head = provider.get_branch(1001, "acme", "checkout-api", "main").head_sha
    tree = provider.get_tree(1001, "acme", "checkout-api", head, 1000)
    kinds = {entry.path: entry.kind for entry in tree.entries}
    assert kinds["docs/link"] == "symlink" and kinds["vendor/sub"] == "submodule"
    content = provider.get_file_content(1001, "acme", "checkout-api", "deploy/id_rsa", head, 10_000)
    assert content.startswith(b"-----BEGIN RSA PRIVATE KEY-----")
    commits = provider.list_commits(1001, "acme", "checkout-api", "main", None, 3)
    assert len(commits) == 3 and commits[0].committed_at >= commits[1].committed_at
    assert commits[0].committed_at.tzinfo is not None
    assert provider.get_commit(1001, "acme", "checkout-api", commits[0].sha, 5).files
    with pytest.raises(ProviderAuthenticationError):
        provider.list_installation_repositories(3003)  # suspended installation
    provider.fail_with = ProviderRateLimitError("limit", 30)
    with pytest.raises(ProviderRateLimitError):
        provider.get_installation(1001)


def test_fixture_rebase_shifts_every_timestamp_consistently() -> None:
    from datetime import timedelta

    base = FixtureProvider()
    shifted = FixtureProvider(shift=timedelta(days=30))
    plain = base.list_commits(1001, "acme", "checkout-api", "main", None, 1)[0]
    moved = shifted.list_commits(1001, "acme", "checkout-api", "main", None, 1)[0]
    assert moved.sha == plain.sha and moved.committed_at - plain.committed_at == timedelta(days=30)
    deployment = base.list_deployments(1001, "acme", "checkout-api", 1)[0]
    shifted_deployment = shifted.list_deployments(1001, "acme", "checkout-api", 1)[0]
    assert shifted_deployment.created_at - deployment.created_at == timedelta(days=30)
    assert shifted_deployment.statuses[-1].created_at - deployment.statuses[
        -1
    ].created_at == timedelta(days=30)
