import pytest
from pydantic import ValidationError

from opspilot_api.config import Environment, Settings

from .conftest import TEST_PASSWORD, make_settings


def test_missing_required_settings_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("POSTGRES_DB", "POSTGRES_PASSWORD", "REDIS_URL", "CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    missing = {err["loc"][0] for err in excinfo.value.errors()}
    assert {"POSTGRES_DB", "POSTGRES_PASSWORD", "REDIS_URL", "CORS_ORIGINS"} <= missing


def test_cors_origins_are_split_and_normalised() -> None:
    settings = make_settings(CORS_ORIGINS="http://localhost:3300, https://app.example.com/")
    assert settings.CORS_ORIGINS == ["http://localhost:3300", "https://app.example.com"]


@pytest.mark.parametrize("bad", ["localhost:3000", "ftp://x.example", "http://x.example/path"])
def test_invalid_cors_origin_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(CORS_ORIGINS=bad)


def test_wildcard_cors_only_in_development() -> None:
    assert make_settings(APP_ENV=Environment.DEVELOPMENT, CORS_ORIGINS="*").CORS_ORIGINS == ["*"]
    with pytest.raises(ValidationError):
        make_settings(APP_ENV=Environment.PRODUCTION, CORS_ORIGINS="*")


def test_invalid_redis_url_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(REDIS_URL="http://localhost:6379")


def test_secrets_not_exposed_in_repr() -> None:
    settings = make_settings()
    assert TEST_PASSWORD not in repr(settings)
    assert "redis-test-secret" not in repr(settings)
    assert TEST_PASSWORD not in settings.database_url.render_as_string(hide_password=True)


def test_fake_remediation_sandbox_is_refused_in_production() -> None:
    with pytest.raises(ValueError, match="fake remediation sandbox"):
        make_settings(
            APP_ENV=Environment.PRODUCTION,
            REMEDIATION_SANDBOX_PROVIDER="fake",
            CORS_ORIGINS="https://app.example.com",
        )


def test_remediation_write_defaults_are_disabled() -> None:
    settings = make_settings()
    assert settings.REMEDIATION_GITHUB_WRITE_ENABLED is False
    assert settings.REMEDIATION_WRITE_ENABLED_REPOSITORIES == []
    with pytest.raises(ValueError, match="less than or equal to 2"):
        make_settings(REMEDIATION_MAX_REPAIR_ATTEMPTS=3)
