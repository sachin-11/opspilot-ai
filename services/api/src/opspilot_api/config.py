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
    AI_MAX_TOOL_CALLS: int = Field(default=14, ge=1, le=50)
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

    GITHUB_PROVIDER: str = Field(default="fixture", pattern="^(fixture|github)$")
    # Demo only: shift fixture timestamps so the planted regression is ~1h before "now".
    GITHUB_FIXTURE_REBASE_TO_NOW: bool = False
    GITHUB_APP_ID: int | None = Field(default=None, ge=1)
    GITHUB_APP_PRIVATE_KEY: SecretStr | None = None
    GITHUB_APP_PRIVATE_KEY_FILE: str | None = None
    GITHUB_WEBHOOK_SECRET: SecretStr | None = None
    GITHUB_API_BASE_URL: str = "https://api.github.com"
    GITHUB_ALLOWED_INSTALLATION_IDS: Annotated[list[int], NoDecode] = Field(default_factory=list)
    GITHUB_REQUEST_TIMEOUT_SECONDS: float = Field(default=15, gt=0, le=60)
    GITHUB_WEBHOOK_MAX_BYTES: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    REPO_MAX_FILES: int = Field(default=2000, ge=1, le=50_000)
    REPO_MAX_FILE_BYTES: int = Field(default=262_144, ge=1024, le=2_097_152)
    REPO_MAX_TOTAL_BYTES: int = Field(default=20_971_520, ge=1024, le=524_288_000)
    REPO_MAX_CHUNKS: int = Field(default=5000, ge=1, le=100_000)
    REPO_INDEX_TIMEOUT_SECONDS: float = Field(default=300, gt=0, le=3600)
    REPO_CHUNK_MAX_LINES: int = Field(default=80, ge=10, le=500)
    REPO_CHUNK_OVERLAP_LINES: int = Field(default=5, ge=0, le=50)
    REPO_MAX_PARSE_FAILURE_RATIO: float = Field(default=0.5, ge=0, le=1)
    REPO_MAX_COMMITS_PER_SYNC: int = Field(default=100, ge=1, le=500)
    REPO_MAX_PATCH_EXCERPT_CHARS: int = Field(default=4000, ge=200, le=20_000)
    REPO_RATE_LIMIT_MAX_RETRIES: int = Field(default=3, ge=0, le=10)
    REPO_CORRELATION_LOOKBACK_HOURS: int = Field(default=72, ge=1, le=720)
    REPO_MIN_VECTOR_RELEVANCE: float = Field(default=0.3, ge=-1, le=1)
    # Hybrid results with no lexical match must clear this stricter vector bar.
    REPO_MIN_HYBRID_VECTOR_RELEVANCE: float = Field(default=0.6, ge=-1, le=1)

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_FILE",
        "GITHUB_WEBHOOK_SECRET",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Compose passes unset optional variables as empty strings; an empty webhook secret
        # must never count as "configured".
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("GITHUB_WEBHOOK_SECRET")
    @classmethod
    def _webhook_secret_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 16:
            raise ValueError("GITHUB_WEBHOOK_SECRET must be at least 16 characters")
        return value

    @field_validator("GITHUB_ALLOWED_INSTALLATION_IDS", mode="before")
    @classmethod
    def _split_installation_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(item) for item in value.split(",") if item.strip()]
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

    @model_validator(mode="after")
    def _validate_repository_configuration(self) -> Self:
        if self.REPO_CHUNK_OVERLAP_LINES >= self.REPO_CHUNK_MAX_LINES:
            raise ValueError("repository chunk overlap must be smaller than the maximum")
        if self.GITHUB_PROVIDER == "github":
            has_key = self.GITHUB_APP_PRIVATE_KEY is not None or self.GITHUB_APP_PRIVATE_KEY_FILE
            if self.GITHUB_APP_ID is None or not has_key:
                raise ValueError("GITHUB_APP_ID and a private key are required for GitHub")
            if self.GITHUB_WEBHOOK_SECRET is None:
                raise ValueError("GITHUB_WEBHOOK_SECRET is required for GitHub")
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
