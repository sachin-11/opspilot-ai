"""Central application settings, loaded from environment variables.

Every required value must be provided explicitly; there are no fallback
credentials. Secrets are held as ``SecretStr`` so they never appear in
``repr`` output or logs.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL

APP_NAME = "OpsPilot AI"
SERVICE_NAME = "opspilot-api"
APP_VERSION = "0.1.0"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _validate_origin(origin: str) -> str:
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"CORS origin must be an http(s) origin, got {origin!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"CORS origin must not contain a path, query or fragment: {origin!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    APP_ENV: Environment = Environment.DEVELOPMENT
    API_HOST: str = "0.0.0.0"  # noqa: S104 - bound inside the container
    API_PORT: int = Field(default=8000, ge=1, le=65535)
    LOG_LEVEL: LogLevel = LogLevel.INFO
    CORS_ORIGINS: Annotated[list[str], NoDecode]

    POSTGRES_DB: str = Field(min_length=1)
    POSTGRES_USER: str = Field(min_length=1)
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_HOST: str = Field(min_length=1)
    POSTGRES_PORT: int = Field(ge=1, le=65535)

    REDIS_URL: SecretStr

    AUTH_JWT_SECRET: SecretStr
    AUTH_ACCESS_TOKEN_MINUTES: int = Field(default=15, ge=1, le=120)
    AUTH_REFRESH_TOKEN_DAYS: int = Field(default=7, ge=1, le=90)
    AUTH_SIGNUP_ENABLED: bool = False
    AUTH_LOGIN_MAX_ATTEMPTS: int = Field(default=10, ge=1, le=1000)
    AUTH_LOGIN_WINDOW_SECONDS: int = Field(default=900, ge=10, le=86400)
    INGEST_RATE_LIMIT_PER_MINUTE: int = Field(default=600, ge=1, le=100000)

    DEPENDENCY_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=30)

    AI_PRIMARY_PROVIDER: str = Field(default="fake", min_length=1, max_length=50)
    AI_PRIMARY_MODEL: str = Field(default="deterministic-v1", min_length=1, max_length=120)
    AI_PRIMARY_API_KEY: SecretStr | None = None
    AI_FALLBACK_PROVIDER: str | None = Field(default=None, max_length=50)
    AI_FALLBACK_MODEL: str | None = Field(default=None, max_length=120)
    AI_FALLBACK_API_KEY: SecretStr | None = None
    AI_PROVIDER_TIMEOUT_SECONDS: float = Field(default=30, gt=0, le=120)
    AI_PROVIDER_RETRIES: int = Field(default=2, ge=0, le=5)
    AI_MAX_RUNTIME_SECONDS: int = Field(default=300, ge=30, le=1800)
    AI_MAX_LLM_CALLS: int = Field(default=8, ge=1, le=50)
    AI_MAX_TOOL_CALLS: int = Field(default=8, ge=1, le=50)
    AI_MAX_RETRIES_PER_NODE: int = Field(default=1, ge=0, le=3)
    AI_MAX_EVIDENCE_ITEMS: int = Field(default=50, ge=5, le=200)
    AI_MAX_TOOL_OUTPUT_BYTES: int = Field(default=65536, ge=1024, le=1048576)
    AI_MAX_INPUT_TOKENS: int = Field(default=50000, ge=1000)
    AI_MAX_OUTPUT_TOKENS: int = Field(default=10000, ge=500)
    AI_MAX_COST_USD: float = Field(default=2.0, gt=0, le=100)
    AI_MAX_REVISIONS: int = Field(default=2, ge=0, le=5)
    AI_INPUT_PRICE_PER_MILLION: float | None = Field(default=None, ge=0)
    AI_OUTPUT_PRICE_PER_MILLION: float | None = Field(default=None, ge=0)
    AI_AUTO_TRIGGER_ENABLED: bool = False
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: SecretStr | None = None

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _check_origins(cls, origins: list[str]) -> list[str]:
        return [origin if origin == "*" else _validate_origin(origin) for origin in origins]

    @field_validator("POSTGRES_PASSWORD")
    @classmethod
    def _password_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("POSTGRES_PASSWORD must not be empty")
        return value

    @field_validator("AUTH_JWT_SECRET")
    @classmethod
    def _check_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("AUTH_JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("REDIS_URL")
    @classmethod
    def _check_redis_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlparse(value.get_secret_value())
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("REDIS_URL must be a redis:// or rediss:// URL")
        return value

    @model_validator(mode="after")
    def _restrict_wildcard_cors(self) -> Self:
        if "*" in self.CORS_ORIGINS and self.APP_ENV is not Environment.DEVELOPMENT:
            raise ValueError("Wildcard CORS origin is only allowed when APP_ENV=development")
        return self

    @model_validator(mode="after")
    def _validate_ai_provider_configuration(self) -> Self:
        if self.AI_PRIMARY_PROVIDER != "fake" and self.AI_PRIMARY_API_KEY is None:
            raise ValueError("AI_PRIMARY_API_KEY is required for non-fake providers")
        fallback_values = (self.AI_FALLBACK_PROVIDER, self.AI_FALLBACK_MODEL)
        if any(fallback_values) and not all(fallback_values):
            raise ValueError(
                "AI_FALLBACK_PROVIDER and AI_FALLBACK_MODEL must be configured together"
            )
        if self.AI_FALLBACK_PROVIDER not in {None, "fake"} and self.AI_FALLBACK_API_KEY is None:
            raise ValueError("AI_FALLBACK_API_KEY is required for a non-fake fallback")
        return self

    @property
    def database_url(self) -> URL:
        """SQLAlchemy URL (psycopg driver). Render with ``hide_password`` before logging."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD.get_secret_value(),
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            database=self.POSTGRES_DB,
        )

    @property
    def checkpoint_url(self) -> str:
        return self.database_url.set(drivername="postgresql").render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    # Required fields come from the environment; mypy cannot see that.
    return Settings()
