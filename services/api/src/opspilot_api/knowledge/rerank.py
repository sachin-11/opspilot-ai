"""Optional bounded reranking with retryable fallback semantics."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from opspilot_api.config import Settings


class RerankError(Exception):
    pass


class RetryableRerankError(RerankError):
    pass


class RerankConfigurationError(RerankError):
    pass


@dataclass(frozen=True)
class RerankItem:
    chunk_id: str
    text: str


class Reranker(ABC):
    @abstractmethod
    def rerank(self, query: str, items: list[RerankItem]) -> dict[str, float]: ...


class FakeReranker(Reranker):
    def __init__(self, model: str = "token-overlap-v1") -> None:
        self.model = model

    def rerank(self, query: str, items: list[RerankItem]) -> dict[str, float]:
        if self.model == "retryable-failure":
            raise RetryableRerankError("temporary reranker failure")
        if self.model == "authentication-failure":
            raise RerankConfigurationError("reranker configuration rejected")
        query_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        return {
            item.chunk_id: len(query_tokens & set(re.findall(r"[a-z0-9]+", item.text.lower())))
            / max(1, len(query_tokens))
            for item in items
        }


class UnsupportedReranker(Reranker):
    def __init__(self, provider: str) -> None:
        self.provider = provider

    def rerank(self, query: str, items: list[RerankItem]) -> dict[str, float]:
        raise RerankConfigurationError(f"reranker provider {self.provider!r} is not installed")


def create_reranker(settings: Settings) -> Reranker:
    if settings.RERANK_PROVIDER == "fake":
        return FakeReranker(settings.RERANK_MODEL)
    return UnsupportedReranker(settings.RERANK_PROVIDER)
