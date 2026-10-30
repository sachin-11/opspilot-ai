"""Batch embedding abstraction with deterministic local vectors and bounded retries."""

import hashlib
import math
import re
import secrets
import time
from abc import ABC, abstractmethod

from opspilot_api.config import Settings


class EmbeddingError(Exception):
    code = "embedding_error"


class RetryableEmbeddingError(EmbeddingError):
    code = "embedding_temporarily_unavailable"


class EmbeddingConfigurationError(EmbeddingError):
    code = "embedding_configuration_error"


class EmbeddingProvider(ABC):
    provider_name: str
    model_name: str
    dimensions: int

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingProvider(EmbeddingProvider):
    provider_name = "fake"

    def __init__(self, model: str = "hash-bow-v1", dimensions: int = 64) -> None:
        self.model_name = model
        self.dimensions = dimensions
        self.calls = 0

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.model_name == "retryable-failure":
            raise RetryableEmbeddingError("temporary embedding failure")
        if self.model_name == "authentication-failure":
            raise EmbeddingConfigurationError("embedding provider configuration rejected")
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign * (1.0 + min(len(token), 12) / 12)
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0:
            return vector
        return [value / magnitude for value in vector]


class UnsupportedEmbeddingProvider(EmbeddingProvider):
    def __init__(self, provider: str, model: str, dimensions: int) -> None:
        self.provider_name = provider
        self.model_name = model
        self.dimensions = dimensions

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingConfigurationError(
            f"embedding provider {self.provider_name!r} is not installed"
        )


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.EMBEDDING_PROVIDER == "fake":
        return FakeEmbeddingProvider(settings.EMBEDDING_MODEL, settings.EMBEDDING_DIMENSIONS)
    return UnsupportedEmbeddingProvider(
        settings.EMBEDDING_PROVIDER, settings.EMBEDDING_MODEL, settings.EMBEDDING_DIMENSIONS
    )


class EmbeddingRouter:
    def __init__(self, settings: Settings, provider: EmbeddingProvider | None = None) -> None:
        self.settings = settings
        self.provider = provider or create_embedding_provider(settings)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.settings.EMBEDDING_BATCH_SIZE):
            batch = texts[start : start + self.settings.EMBEDDING_BATCH_SIZE]
            result: list[list[float]] | None = None
            last_error: RetryableEmbeddingError | None = None
            for attempt in range(self.settings.EMBEDDING_RETRIES + 1):
                try:
                    result = self.provider.embed_batch(batch)
                    break
                except RetryableEmbeddingError as exc:
                    last_error = exc
                    if attempt < self.settings.EMBEDDING_RETRIES:
                        time.sleep(min(1.0, 0.05 * (2**attempt)) + secrets.randbelow(20) / 1000)
            if result is None:
                raise last_error or EmbeddingError("embedding batch failed")
            if len(result) != len(batch) or any(
                len(vector) != self.settings.EMBEDDING_DIMENSIONS for vector in result
            ):
                raise EmbeddingError("embedding provider returned invalid dimensions")
            vectors.extend(result)
        return vectors
