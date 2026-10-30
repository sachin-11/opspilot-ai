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

    DEPENDENCY_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=30)

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


@lru_cache
def get_settings() -> Settings:
    # Required fields come from the environment; mypy cannot see that.
    return Settings()
