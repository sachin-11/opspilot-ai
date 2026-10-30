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

    KNOWLEDGE_STORAGE_BACKEND: str = Field(default="local", pattern="^(local|s3)$")
    KNOWLEDGE_STORAGE_ROOT: str = Field(default="/app/data/knowledge", min_length=1)
    KNOWLEDGE_MAX_FILE_BYTES: int = Field(default=5_242_880, ge=1024, le=52_428_800)
    KNOWLEDGE_MAX_PDF_PAGES: int = Field(default=100, ge=1, le=1000)
    KNOWLEDGE_MAX_EXTRACTED_CHARS: int = Field(default=2_000_000, ge=1000)
    KNOWLEDGE_EXTRACTION_TIMEOUT_SECONDS: float = Field(default=20, gt=0, le=120)
    KNOWLEDGE_CHUNK_TARGET_TOKENS: int = Field(default=300, ge=50, le=2000)
    KNOWLEDGE_CHUNK_OVERLAP_TOKENS: int = Field(default=40, ge=0, le=500)
    KNOWLEDGE_CHUNK_MIN_TOKENS: int = Field(default=20, ge=1, le=500)
    KNOWLEDGE_CHUNK_MAX_TOKENS: int = Field(default=500, ge=50, le=3000)
    EMBEDDING_PROVIDER: str = Field(default="fake", min_length=1, max_length=50)
    EMBEDDING_MODEL: str = Field(default="hash-bow-v1", min_length=1, max_length=120)
    EMBEDDING_API_KEY: SecretStr | None = None
    EMBEDDING_DIMENSIONS: int = Field(default=64, ge=1, le=4096)
    EMBEDDING_BATCH_SIZE: int = Field(default=32, ge=1, le=256)
    EMBEDDING_TIMEOUT_SECONDS: float = Field(default=20, gt=0, le=120)
    EMBEDDING_RETRIES: int = Field(default=2, ge=0, le=5)
    EMBEDDING_MAX_TOKENS_PER_JOB: int = Field(default=200_000, ge=1000)
    KNOWLEDGE_MAX_QUERY_LENGTH: int = Field(default=1000, ge=20, le=5000)
    KNOWLEDGE_MAX_CANDIDATES: int = Field(default=50, ge=5, le=200)
    KNOWLEDGE_MAX_TOP_K: int = Field(default=20, ge=1, le=50)
    KNOWLEDGE_MIN_RELEVANCE: float = Field(default=0.01, ge=0, le=1)
    KNOWLEDGE_MIN_VECTOR_RELEVANCE: float = Field(default=0.30, ge=-1, le=1)
    KNOWLEDGE_MIN_RERANK_RELEVANCE: float = Field(default=0.10, ge=0, le=1)
    RERANK_PROVIDER: str = Field(default="fake", min_length=1, max_length=50)
    RERANK_MODEL: str = Field(default="token-overlap-v1", min_length=1, max_length=120)
    RERANK_API_KEY: SecretStr | None = None
    RERANK_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=60)
    RERANK_CANDIDATE_LIMIT: int = Field(default=20, ge=1, le=100)
    KNOWLEDGE_EXPERIMENTAL_QUERY_ENHANCEMENT: bool = False
    KNOWLEDGE_ENHANCEMENT_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=60)
    KNOWLEDGE_ENHANCEMENT_MAX_OUTPUT_TOKENS: int = Field(default=300, ge=20, le=2000)
    KNOWLEDGE_ENHANCEMENT_MAX_COST_USD: float = Field(default=0.01, ge=0, le=1)

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

    @model_validator(mode="after")
    def _validate_knowledge_configuration(self) -> Self:
        if self.EMBEDDING_DIMENSIONS != 64:
            raise ValueError(
                "EMBEDDING_DIMENSIONS must be 64 for schema version 0004; change it via migration"
            )
        if self.KNOWLEDGE_CHUNK_OVERLAP_TOKENS >= self.KNOWLEDGE_CHUNK_TARGET_TOKENS:
            raise ValueError("chunk overlap must be smaller than target size")
        if self.KNOWLEDGE_CHUNK_MIN_TOKENS > self.KNOWLEDGE_CHUNK_TARGET_TOKENS:
            raise ValueError("chunk minimum must not exceed target size")
        if self.KNOWLEDGE_CHUNK_TARGET_TOKENS > self.KNOWLEDGE_CHUNK_MAX_TOKENS:
            raise ValueError("chunk target must not exceed maximum size")
        if self.EMBEDDING_PROVIDER != "fake" and self.EMBEDDING_API_KEY is None:
            raise ValueError("EMBEDDING_API_KEY is required for non-fake embedding providers")
        if self.RERANK_PROVIDER != "fake" and self.RERANK_API_KEY is None:
            raise ValueError("RERANK_API_KEY is required for non-fake rerank providers")
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
