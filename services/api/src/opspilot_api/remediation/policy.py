"""Deterministic, versioned patch policy. No model output participates in these decisions.

``evaluate_patch`` is a pure function of the patch text, the policy context, and the version. A
model may be asked to *explain* violations afterwards, but can never remove or downgrade one.
"""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from opspilot_api.config import Settings
from opspilot_api.remediation.diffparse import DiffError, FileDiff, parse_unified_diff
from opspilot_api.remediation.models import PolicyDecision, RiskLevel

POLICY_VERSION = "2026-10.1"

ALLOWED_EXTENSIONS = frozenset(
    {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".yaml", ".yml", ".toml"}
    | {".ini", ".cfg", ".txt", ".md"}
)
_CONFIG_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"})
_DEPENDENCY_MANIFESTS = frozenset(
    {"package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "go.mod"}
)
_LOCKFILES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "cargo.lock",
        "go.sum",
        "gemfile.lock",
        "composer.lock",
        "pipfile.lock",
    }
)
_CI_NAMES = frozenset(
    {".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml"}
)
_CI_DIRS = frozenset({".github", ".circleci", ".buildkite", ".husky"})
_INFRA_DIRS = frozenset(
    {"terraform", "infra", "infrastructure", "k8s", "kubernetes", "helm", "charts", "pulumi"}
    | {"cloudformation", "ansible", "deploy", "deployment", "deployments", "manifests", "cdk"}
)
_MIGRATION_DIRS = frozenset({"migrations", "migrate", "alembic", "migration"})
_GENERATED_DIRS = frozenset(
    {"vendor", "node_modules", "dist", "build", "generated", "__generated__", "third_party"}
    | {".next", "target", "out", "coverage", "site-packages", ".venv", "venv"}
)
_GENERATED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py", ".min.js", ".min.css", ".generated.ts", ".map")
_KEY_SUFFIXES = (".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".jks", ".keystore", ".pub")
_KEY_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_AUTH_TOKENS = frozenset(
    {"auth", "authn", "authz", "authentication", "authorization", "oauth", "rbac", "acl", "iam"}
    | {"permission", "permissions", "jwt", "sso", "saml"}
)
_BILLING_TOKENS = frozenset(
    {"billing", "payment", "payments", "invoice", "invoices", "stripe", "paypal", "subscription"}
)
_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "development", "production", "prod"})
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
)


@dataclass(frozen=True)
class PolicyLimits:
    max_files: int
    max_added: int
    max_deleted: int
    max_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "PolicyLimits":
        return cls(
            settings.REMEDIATION_MAX_FILES,
            settings.REMEDIATION_MAX_ADDED_LINES,
            settings.REMEDIATION_MAX_DELETED_LINES,
            settings.REMEDIATION_MAX_PATCH_BYTES,
        )


@dataclass(frozen=True)
class PolicyContext:
    limits: PolicyLimits
    path_prefix: str | None = None
    allow_auth_paths: bool = False
    allow_billing_paths: bool = False
    target_branch: str | None = None
    default_branch: str | None = None
    branch_prefix: str = "opspilot/fix-"
    # Paths the model was permitted to see; anything else is out of scope.
    allowed_paths: frozenset[str] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        path_prefix: str | None = None,
        default_branch: str | None = None,
        target_branch: str | None = None,
    ) -> "PolicyContext":
        return cls(
            limits=PolicyLimits.from_settings(settings),
            path_prefix=path_prefix,
            allow_auth_paths=settings.REMEDIATION_ALLOW_AUTH_PATHS,
            allow_billing_paths=settings.REMEDIATION_ALLOW_BILLING_PATHS,
            target_branch=target_branch,
            default_branch=default_branch,
            branch_prefix=settings.REMEDIATION_BRANCH_PREFIX,
        )


@dataclass
class PolicyResult:
    version: str
    decision: PolicyDecision
    risk_level: RiskLevel
    violations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision is PolicyDecision.BLOCKED


def _issue(
    code: str, message: str, path: str | None = None, line: int | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if path is not None:
        item["path"] = path
    if line is not None:
        item["line"] = line
    return item


def _tokens(path: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", path.lower()) if token}


def check_path(path: str, ctx: PolicyContext) -> list[dict[str, Any]]:
    """Path-level policy: traversal, blocked locations, allowed types, monorepo prefix."""
    found: list[dict[str, Any]] = []
    if (
        not path
        or path.startswith(("/", "~"))
        or "\\" in path
        or re.match(r"^[A-Za-z]:", path)
        or "\x00" in path
    ):
        return [_issue("path_traversal", "absolute or malformed path", path)]
    parts = PurePosixPath(path).parts
    if any(part in {"..", ".", ""} for part in parts) or "//" in path:
        return [_issue("path_traversal", "path traversal segment", path)]
    lower_parts = [part.lower() for part in parts]
    name = lower_parts[-1]
    suffix = PurePosixPath(name).suffix
    if ".git" in lower_parts:
        found.append(_issue("blocked_path", "git internals are not modifiable", path))
    if name == ".gitmodules":
        found.append(_issue("submodule_modification", "submodule configuration", path))
    if (
        lower_parts[0] in _CI_DIRS
        or name in _CI_NAMES
        or (lower_parts[0] == ".github" and "workflows" in lower_parts)
    ):
        found.append(_issue("blocked_path_ci", "CI/CD configuration is out of scope", path))
    if name.startswith(".env") or name in {".npmrc", ".pypirc", ".netrc", "credentials"}:
        found.append(_issue("blocked_path_secrets", "environment/credential files", path))
    if name.endswith(_KEY_SUFFIXES) or name.startswith(_KEY_NAMES):
        found.append(_issue("blocked_path_secrets", "key or certificate material", path))
    if name.startswith(("dockerfile", "docker-compose", "compose.")) or name.endswith(
        (".tf", ".tfvars", ".tfstate")
    ):
        found.append(_issue("blocked_path_infrastructure", "infrastructure definition", path))
    if _INFRA_DIRS & set(lower_parts[:-1]):
        found.append(_issue("blocked_path_infrastructure", "infrastructure/deployment path", path))
    if _MIGRATION_DIRS & set(lower_parts[:-1]) or suffix == ".sql":
        found.append(_issue("blocked_path_migration", "database migration or SQL", path))
    if name in _LOCKFILES or suffix == ".lock":
        found.append(_issue("blocked_path_lockfile", "dependency lockfile", path))
    if _GENERATED_DIRS & set(lower_parts[:-1]) or name.endswith(_GENERATED_SUFFIXES):
        found.append(_issue("blocked_path_generated", "generated or vendored code", path))
    if not ctx.allow_auth_paths and _AUTH_TOKENS & _tokens(path):
        found.append(_issue("blocked_path_auth", "authentication/authorization code", path))
    if not ctx.allow_billing_paths and _BILLING_TOKENS & _tokens(path):
        found.append(_issue("blocked_path_billing", "billing/payment code", path))
    if suffix not in ALLOWED_EXTENSIONS and not found:
        found.append(_issue("disallowed_file_type", f"file type {suffix or name!r}", path))
    if ctx.path_prefix:
        prefix = ctx.path_prefix.strip("/")
        if path != prefix and not path.startswith(prefix + "/"):
            found.append(_issue("outside_path_prefix", f"outside {prefix}/", path))
    if ctx.allowed_paths is not None and path not in ctx.allowed_paths:
        found.append(_issue("outside_allowed_scope", "file was not in the allowed scope", path))
    return found


_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "credential_material",
        "private key material",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    ("credential_material", "AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "credential_material",
        "provider token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_\w{20,}|xox[baprs]-[\w-]{10,})"),
    ),
    ("credential_material", "API secret key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    (
        "credential_material",
        "hard-coded credential",
        re.compile(
            r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
            r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
        ),
    ),
    ("credential_material", "credentials in URL", re.compile(r"://[^/\s:@]+:[^/\s@]{3,}@")),
    (
        "network_exfiltration",
        "outbound network call to a literal URL",
        re.compile(
            r"(?i)(?:requests|httpx|urllib3?|aiohttp|http|https|axios|got|session)\.\w+\(|"
            r"\bfetch\(|\burlopen\(|\bcurl\b|\bwget\b|XMLHttpRequest"
        ),
    ),
    (
        "network_access",
        "raw socket or low-level network access",
        re.compile(
            r"\bsocket\.(?:socket|create_connection)\b|\bnet\.(?:connect|createConnection|Socket)\b"
            r"|\bdns\.(?:resolver|lookup)|\bsmtplib\b|\bftplib\b|\bparamiko\b|\bimport\s+socket\b"
            r"|\bhttp\.client\b|\bWebSocket\(|\bdgram\b"
        ),
    ),
    (
        "reverse_shell",
        "reverse-shell pattern",
        re.compile(
            r"/dev/tcp/|\bnc\s+-[a-z]*e\b|\bncat\b|\bpty\.spawn\b|\bmkfifo\b|\bbash\s+-i\b"
            r"|/bin/(?:ba)?sh\s+-i\b|\bdup2\(|child_process.{0,60}['\"](?:ba)?sh['\"]"
        ),
    ),
    (
        "persistence_mechanism",
        "persistence mechanism",
        re.compile(
            r"\bcrontab\b|/etc/cron|systemctl\s+enable|\.bashrc|\.bash_profile|authorized_keys"
            r"|launchctl|schtasks|CurrentVersion\\\\?Run|rc\.local|\.ssh/"
        ),
    ),
    (
        "obfuscated_content",
        "dynamic code execution or encoded payload",
        re.compile(
            r"(?<![.\w])(?:eval|exec|compile)\(|\b__import__\(|\batob\(|new Function\(|b64decode\("
            r"|(?:\\x[0-9a-fA-F]{2}){8,}|[A-Za-z0-9+/]{120,}={0,2}"
        ),
    ),
    (
        "ci_bypass",
        "CI bypass",
        re.compile(
            r"(?i)\[(?:skip ci|ci skip)\]|--no-verify|continue-on-error|\|\|\s*true\b|\bexit\s+0\b"
        ),
    ),
    (
        "security_check_disabled",
        "security check disabled",
        re.compile(
            r"verify\s*=\s*False|_create_unverified_context|rejectUnauthorized\s*:\s*false"
            r"|NODE_TLS_REJECT_UNAUTHORIZED|#\s*nosec|check_hostname\s*=\s*False|csrf_exempt"
            r"|--insecure|ssl\s*=\s*False|SECURE\w*\s*=\s*False"
        ),
    ),
    (
        "test_weakening",
        "test skipped or disabled",
        re.compile(
            r"pytest\.mark\.(?:skip|xfail)|pytest\.skip\(|@unittest\.skip|\b(?:it|test|describe)"
            r"\.skip\b|\bxit\(|\bxdescribe\(|\bxtest\("
        ),
    ),
)
_COMMENT_START = re.compile(r"^\s*(?:#|//|\*|/\*|--)")
_URL = re.compile(r"https?://", re.IGNORECASE)
_PROMPT_INJECTION = re.compile(
    r"(?i)ignore (?:all )?(?:previous|prior) instructions|system\s*:\s*ignore|you are now"
)
_PY_EXCEPT = re.compile(
    r"^\s*except(?:\s+(?:Exception|BaseException))?(?:\s+as\s+\w+)?\s*:\s*(.*)$"
)
_JS_EMPTY_CATCH = re.compile(
    r"catch\s*(?:\([^)]*\))?\s*\{\s*\}|\.catch\(\s*\(?\w*\)?\s*=>\s*\{\s*\}\s*\)"
)
_PUBLIC_API = re.compile(
    r"^\s*(?:@(?:router|app)\.(?:get|post|put|patch|delete)|export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|const|interface|type)\b)"
)


def _added_lines(diff: FileDiff) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for hunk in diff.hunks:
        line_no = hunk.new_start
        for tag, text in hunk.lines:
            if tag == "+":
                result.append((line_no, text))
            if tag != "-":
                line_no += 1
    return result


def _content_rules(diff: FileDiff) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for line_no, text in _added_lines(diff):
        is_comment = bool(_COMMENT_START.match(text))
        for code, message, pattern in _RULES:
            if not pattern.search(text):
                continue
            if code == "network_exfiltration" and (is_comment or not _URL.search(text)):
                # Relative calls (e.g. fetch(path)) are config-level; literal egress is not.
                continue
            if code in {"ci_bypass", "persistence_mechanism", "test_weakening"} and is_comment:
                continue
            found.append(_issue(code, message, diff.path, line_no))
    for hunk in diff.hunks:
        new_side = [(tag, text) for tag, text in hunk.lines if tag != "-"]
        line_no = hunk.new_start
        for position, (tag, text) in enumerate(new_side):
            current = line_no + position
            nxt = new_side[position + 1][1].strip() if position + 1 < len(new_side) else ""
            added_here = tag == "+" or (
                position + 1 < len(new_side) and new_side[position + 1][0] == "+"
            )
            match = _PY_EXCEPT.match(text)
            if match and added_here:
                body = match.group(1).strip() or nxt
                bare = not re.search(r"except\s+\w", text)
                if bare or body in {"pass", "...", "continue"}:
                    found.append(
                        _issue("broad_exception_swallow", "exception swallowed", diff.path, current)
                    )
            elif tag == "+" and _JS_EMPTY_CATCH.search(text):
                found.append(
                    _issue("broad_exception_swallow", "empty catch handler", diff.path, current)
                )
    return found


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path.lower()))


def _test_rules(diff: FileDiff) -> list[dict[str, Any]]:
    if not _is_test_path(diff.path):
        return []
    if diff.kind == "deleted":
        return [_issue("test_deletion", "test file deleted", diff.path)]
    removed_asserts = added_asserts = 0
    marker = re.compile(
        r"\bassert\b|\bexpect\(|\.should\b|assert_|self\.assert|\bdef test_|\b(?:it|test)\("
    )
    for hunk in diff.hunks:
        for tag, text in hunk.lines:
            if marker.search(text):
                removed_asserts += tag == "-"
                added_asserts += tag == "+"
    if removed_asserts > added_asserts:
        return [_issue("test_weakening", "net removal of assertions or test cases", diff.path)]
    return []


def _warnings(diffs: list[FileDiff], added: int, deleted: int) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    touched_tests = [item for item in diffs if _is_test_path(item.path)]
    for item in diffs:
        name = PurePosixPath(item.path).name.lower()
        if PurePosixPath(name).suffix in _CONFIG_EXTENSIONS and name not in _DEPENDENCY_MANIFESTS:
            warnings.append(
                _issue("configuration_change", "configuration behavior change", item.path)
            )
        if name in _DEPENDENCY_MANIFESTS:
            warnings.append(_issue("new_dependencies", "dependency manifest modified", item.path))
        changed = [text for _, text in _added_lines(item)] + [
            text for hunk in item.hunks for tag, text in hunk.lines if tag == "-"
        ]
        if any(_PUBLIC_API.match(text) for text in changed):
            warnings.append(_issue("public_api_change", "public API surface changed", item.path))
        if any(_PROMPT_INJECTION.search(text) for text in changed):
            warnings.append(
                _issue("prompt_injection_marker", "instruction-like text in patch", item.path)
            )
    if not touched_tests:
        warnings.append(_issue("missing_targeted_test", "patch adds or updates no test"))
    if len(diffs) > 3 or added + deleted > 60:
        warnings.append(_issue("large_behavioral_surface", "patch touches a broad surface"))
    if any(item.deleted > item.added for item in touched_tests):
        warnings.append(_issue("reduced_test_coverage", "test lines removed"))
    return warnings


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {(i["code"], i.get("path", ""), i.get("line", 0), i["message"]): i for i in items}
    return [unique[key] for key in sorted(unique)]


def evaluate_branch(branch: str, ctx: PolicyContext) -> list[dict[str, Any]]:
    lowered = branch.lower()
    if (
        lowered in _PROTECTED_BRANCHES
        or lowered == (ctx.default_branch or "").lower()
        or not branch.startswith(ctx.branch_prefix)
        or ".." in branch
    ):
        return [_issue("protected_branch_target", "publication must target a fix branch")]
    return []


def evaluate_patch(patch_text: str, ctx: PolicyContext) -> PolicyResult:
    """Pure, deterministic evaluation; the same inputs always give the same result."""
    violations: list[dict[str, Any]] = []
    encoded = len(patch_text.encode("utf-8", errors="replace"))
    if encoded > ctx.limits.max_bytes:
        violations.append(
            _issue("limit_patch_bytes", f"patch is {encoded} bytes (limit {ctx.limits.max_bytes})")
        )
    try:
        diffs = parse_unified_diff(patch_text)
    except DiffError as exc:
        violations.append(_issue(exc.code, str(exc)))
        return PolicyResult(
            POLICY_VERSION, PolicyDecision.BLOCKED, RiskLevel.PROHIBITED, _sorted(violations), []
        )
    added = sum(item.added for item in diffs)
    deleted = sum(item.deleted for item in diffs)
    if len(diffs) > ctx.limits.max_files:
        violations.append(
            _issue("limit_files", f"{len(diffs)} files changed (limit {ctx.limits.max_files})")
        )
    if added > ctx.limits.max_added:
        violations.append(_issue("limit_added_lines", f"{added} lines added"))
    if deleted > ctx.limits.max_deleted:
        violations.append(_issue("limit_deleted_lines", f"{deleted} lines deleted"))
    for item in diffs:
        violations.extend(check_path(item.path, ctx))
        if item.file_mode == "120000":
            violations.append(_issue("symlink_creation", "symlink creation", item.path))
        elif item.file_mode == "160000":
            violations.append(_issue("submodule_modification", "submodule change", item.path))
        elif item.file_mode and item.file_mode != "100644":
            violations.append(_issue("executable_permission", "non-regular file mode", item.path))
        if item.old_mode or item.new_mode:
            violations.append(_issue("mode_change", "file mode change", item.path))
        violations.extend(_content_rules(item))
        violations.extend(_test_rules(item))
    if ctx.target_branch is not None:
        violations.extend(evaluate_branch(ctx.target_branch, ctx))
    warnings = [] if violations else _warnings(diffs, added, deleted)
    ordered_violations = _sorted(violations)
    ordered_warnings = _sorted(warnings)
    if ordered_violations:
        return PolicyResult(
            POLICY_VERSION,
            PolicyDecision.BLOCKED,
            RiskLevel.PROHIBITED,
            ordered_violations,
            ordered_warnings,
        )
    high_codes = {"public_api_change", "new_dependencies", "prompt_injection_marker"}
    if any(item["code"] in high_codes for item in ordered_warnings) or added + deleted > 80:
        risk = RiskLevel.HIGH
    elif ordered_warnings:
        risk = RiskLevel.MEDIUM
    else:
        risk = RiskLevel.LOW
    decision = PolicyDecision.ALLOWED_WITH_WARNINGS if ordered_warnings else PolicyDecision.ALLOWED
    return PolicyResult(POLICY_VERSION, decision, risk, [], ordered_warnings)
