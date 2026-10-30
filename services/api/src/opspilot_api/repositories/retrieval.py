"""Workspace-scoped code retrieval: lexical, vector, hybrid RRF, and optional reranking.

Every candidate query joins the repository's ``active_snapshot_id`` (a fully ready snapshot) and
repeats the workspace predicate, so other workspaces, removed repositories, and unfinished or
stale snapshots can never enter a candidate set.
"""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, and_, create_engine, exists, func, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.investigations.safety import redact_text
from opspilot_api.knowledge.embeddings import EmbeddingRouter
from opspilot_api.knowledge.models import RetrievalLog
from opspilot_api.knowledge.rerank import (
    Reranker,
    RerankItem,
    RetryableRerankError,
    create_reranker,
)
from opspilot_api.repositories.models import (
    CodeChunk,
    Repository,
    RepositorySnapshot,
    RepositoryStatus,
    ServiceRepositoryMapping,
    SnapshotStatus,
)
from opspilot_api.repositories.schemas import (
    CodeSearchDiagnostics,
    CodeSearchRequest,
    CodeSearchResponse,
    CodeSearchResult,
    CodeVariant,
)

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_WORDS = re.compile(r"[A-Za-z0-9]+")
EXCERPT_CHARS = 1500


@dataclass
class _Candidate:
    chunk: CodeChunk
    repository_full_name: str
    commit_sha: str
    branch: str
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


def citation_label(full_name: str, commit_sha: str, chunk: CodeChunk) -> str:
    lines = (
        str(chunk.start_line)
        if chunk.start_line == chunk.end_line
        else f"{chunk.start_line}-{chunk.end_line}"
    )
    symbol = f", {chunk.symbol_name}" if chunk.symbol_name else ""
    return f"[github.com/{full_name}@{commit_sha[:7]}: {chunk.path}:{lines}{symbol}]"


def _query_terms(query: str) -> list[str]:
    seen: dict[str, None] = {}
    for word in _WORDS.findall(_CAMEL.sub(r"\1 \2", query)):
        if len(word) >= 2:
            seen[word.lower()] = None
    return list(seen)[:32]


class CodeRetrievalService:
    def __init__(
        self,
        settings: Settings,
        embeddings: EmbeddingRouter | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = settings
        self.embeddings = embeddings or EmbeddingRouter(settings)
        self.reranker = reranker or create_reranker(settings)

    @staticmethod
    def _base(workspace_id: UUID, request: CodeSearchRequest) -> Select[CodeChunk, str, str, str]:
        query = (
            select(
                CodeChunk,
                Repository.full_name,
                RepositorySnapshot.commit_sha,
                RepositorySnapshot.branch,
            )
            .join(
                Repository,
                and_(
                    Repository.id == CodeChunk.repository_id,
                    Repository.workspace_id == workspace_id,
                    Repository.active_snapshot_id == CodeChunk.snapshot_id,
                ),
            )
            .join(
                RepositorySnapshot,
                and_(
                    RepositorySnapshot.id == CodeChunk.snapshot_id,
                    RepositorySnapshot.workspace_id == workspace_id,
                    RepositorySnapshot.status == SnapshotStatus.READY.value,
                ),
            )
            .where(
                CodeChunk.workspace_id == workspace_id,
                Repository.status.in_(
                    [
                        RepositoryStatus.ACTIVE.value,
                        RepositoryStatus.SYNCING.value,
                        RepositoryStatus.FAILED.value,
                    ]
                ),
            )
        )
        if request.repository_id:
            query = query.where(CodeChunk.repository_id == request.repository_id)
        if request.branch:
            query = query.where(RepositorySnapshot.branch == request.branch)
        if request.language:
            query = query.where(CodeChunk.language == request.language.lower())
        if request.symbol_type:
            query = query.where(CodeChunk.symbol_type == request.symbol_type.lower())
        if request.path_prefix:
            prefix = request.path_prefix.lstrip("/")
            query = query.where(func.left(CodeChunk.path, len(prefix)) == prefix)
        if request.service_id or request.environment:
            # Monorepo scoping: a mapped path prefix limits which files belong to the service.
            conditions = [
                ServiceRepositoryMapping.workspace_id == workspace_id,
                ServiceRepositoryMapping.repository_id == CodeChunk.repository_id,
                (
                    ServiceRepositoryMapping.path_prefix.is_(None)
                    | (
                        func.left(CodeChunk.path, func.length(ServiceRepositoryMapping.path_prefix))
                        == ServiceRepositoryMapping.path_prefix
                    )
                ),
            ]
            if request.service_id:
                conditions.append(ServiceRepositoryMapping.service_id == request.service_id)
            if request.environment:
                conditions.append(ServiceRepositoryMapping.environment == request.environment.value)
            query = query.where(exists().where(*conditions))
        return query

    def _lexical(
        self, session: Session, workspace_id: UUID, request: CodeSearchRequest
    ) -> list[_Candidate]:
        terms = _query_terms(request.query)
        if not terms:
            return []
        # OR-semantics over sanitized alphanumeric terms; the string is bound as a parameter.
        parsed = func.to_tsquery("english", " | ".join(terms))
        score = func.ts_rank_cd(CodeChunk.search_vector, parsed).label("lexical_score")
        statement = (
            self._base(workspace_id, request)
            .add_columns(score)
            .where(CodeChunk.search_vector.op("@@")(parsed))
            .order_by(score.desc(), CodeChunk.id)
            .limit(request.candidate_count)
        )
        return [
            _Candidate(
                row[0], row[1], row[2], row[3], lexical_rank=index, lexical_score=float(row[4])
            )
            for index, row in enumerate(session.execute(statement), start=1)
        ]

    def _vector(
        self,
        session: Session,
        workspace_id: UUID,
        request: CodeSearchRequest,
        vector: list[float],
    ) -> list[_Candidate]:
        distance = CodeChunk.embedding.cosine_distance(vector).label("distance")
        statement = (
            self._base(workspace_id, request)
            .add_columns(distance)
            .where(
                CodeChunk.embedding_provider == self.settings.EMBEDDING_PROVIDER,
                CodeChunk.embedding_model == self.settings.EMBEDDING_MODEL,
            )
            .order_by(distance, CodeChunk.id)
            .limit(request.candidate_count)
        )
        return [
            _Candidate(
                row[0],
                row[1],
                row[2],
                row[3],
                vector_rank=index,
                vector_score=max(-1.0, 1.0 - float(row[4])),
            )
            for index, row in enumerate(session.execute(statement), start=1)
        ]

    @staticmethod
    def _rrf(
        lexical: list[_Candidate], vector: list[_Candidate], constant: int = 60
    ) -> list[_Candidate]:
        combined: dict[UUID, _Candidate] = {}
        for source in (lexical, vector):
            for item in source:
                existing = combined.get(item.chunk.id)
                if existing is None:
                    existing = _Candidate(
                        item.chunk, item.repository_full_name, item.commit_sha, item.branch
                    )
                    combined[item.chunk.id] = existing
                if item.lexical_rank is not None:
                    existing.lexical_rank, existing.lexical_score = (
                        item.lexical_rank,
                        item.lexical_score,
                    )
                if item.vector_rank is not None:
                    existing.vector_rank, existing.vector_score = (
                        item.vector_rank,
                        item.vector_score,
                    )
        for item in combined.values():
            item.rrf_score = sum(
                1 / (constant + rank)
                for rank in (item.lexical_rank, item.vector_rank)
                if rank is not None
            )
        return sorted(
            combined.values(), key=lambda item: (-(item.rrf_score or 0), str(item.chunk.id))
        )

    def _relevant(self, item: _Candidate, variant: CodeVariant) -> bool:
        vector_ok = (item.vector_score or -1) >= self.settings.REPO_MIN_VECTOR_RELEVANCE
        if variant is CodeVariant.LEXICAL:
            return (item.lexical_score or 0) > 0
        if variant is CodeVariant.VECTOR:
            return vector_ok
        strong_vector = (item.vector_score or -1) >= self.settings.REPO_MIN_HYBRID_VECTOR_RELEVANCE
        return item.lexical_rank is not None or strong_vector

    def search(
        self,
        session: Session,
        workspace_id: UUID,
        request: CodeSearchRequest,
        investigation_id: UUID | None = None,
    ) -> CodeSearchResponse:
        if len(request.query) > self.settings.KNOWLEDGE_MAX_QUERY_LENGTH:
            raise ValueError("query exceeds configured length limit")
        started = time.perf_counter()
        embedding_latency: float | None = None
        rerank_latency: float | None = None
        fallback = False
        vector: list[float] | None = None
        if request.variant is not CodeVariant.LEXICAL:
            tick = time.perf_counter()
            vector = self.embeddings.embed([request.query])[0]
            embedding_latency = (time.perf_counter() - tick) * 1000
        lexical = (
            self._lexical(session, workspace_id, request)
            if request.variant is not CodeVariant.VECTOR
            else []
        )
        vector_results = self._vector(session, workspace_id, request, vector) if vector else []
        if request.variant is CodeVariant.LEXICAL:
            candidates = lexical
        elif request.variant is CodeVariant.VECTOR:
            candidates = vector_results
        else:
            candidates = self._rrf(lexical, vector_results)
        if request.variant is CodeVariant.HYBRID_RRF_RERANK:
            tick = time.perf_counter()
            bounded = candidates[: self.settings.RERANK_CANDIDATE_LIMIT]
            try:
                scores = self.reranker.rerank(
                    request.query,
                    [RerankItem(str(item.chunk.id), item.chunk.content) for item in bounded],
                )
                for item in bounded:
                    item.rerank_score = scores[str(item.chunk.id)]
                candidates = sorted(
                    bounded, key=lambda item: (-(item.rerank_score or 0), -(item.rrf_score or 0))
                )
            except RetryableRerankError:
                fallback = True
            rerank_latency = (time.perf_counter() - tick) * 1000
        results = [
            self._result(item)
            for item in candidates[: request.top_k]
            if self._relevant(item, request.variant)
        ]
        latency = (time.perf_counter() - started) * 1000
        safe_query = redact_text(request.query)[: self.settings.KNOWLEDGE_MAX_QUERY_LENGTH]
        session.add(
            RetrievalLog(
                workspace_id=workspace_id,
                investigation_id=investigation_id,
                query=safe_query,
                query_hash=hashlib.sha256(safe_query.encode()).hexdigest(),
                retrieval_variant=f"code_{request.variant.value}",
                filters={
                    "repository_id": str(request.repository_id) if request.repository_id else None,
                    "service_id": str(request.service_id) if request.service_id else None,
                    "language": request.language,
                    "path_prefix": request.path_prefix,
                    "domain": "code",
                },
                top_k=request.top_k,
                candidate_count=request.candidate_count,
                result_chunk_ids=[str(item.chunk_id) for item in results],
                latency_ms=latency,
                embedding_latency_ms=embedding_latency,
                rerank_latency_ms=rerank_latency,
                rerank_fallback_used=fallback,
            )
        )
        session.commit()
        return CodeSearchResponse(
            results=results,
            diagnostics=CodeSearchDiagnostics(
                variant=request.variant,
                candidate_count=len(candidates),
                latency_ms=round(latency, 3),
                embedding_latency_ms=round(embedding_latency, 3) if embedding_latency else None,
                rerank_latency_ms=round(rerank_latency, 3) if rerank_latency else None,
                rerank_fallback_used=fallback,
                low_relevance=not results,
            ),
        )

    @staticmethod
    def _result(item: _Candidate) -> CodeSearchResult:
        chunk = item.chunk
        return CodeSearchResult(
            chunk_id=chunk.id,
            repository_id=chunk.repository_id,
            repository_full_name=item.repository_full_name,
            snapshot_id=chunk.snapshot_id,
            commit_sha=item.commit_sha,
            branch=item.branch,
            path=chunk.path,
            language=chunk.language,
            symbol_name=chunk.symbol_name,
            symbol_type=chunk.symbol_type,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            excerpt=chunk.content[:EXCERPT_CHARS],
            lexical_rank=item.lexical_rank,
            lexical_score=item.lexical_score,
            vector_rank=item.vector_rank,
            vector_score=item.vector_score,
            rrf_score=item.rrf_score,
            rerank_score=item.rerank_score,
            citation_label=citation_label(item.repository_full_name, item.commit_sha, chunk),
        )


def get_code_chunk(
    session: Session, workspace_id: UUID, chunk_id: UUID, repository_ids: set[UUID] | None = None
) -> CodeSearchResult:
    """One chunk from a repository's active snapshot, workspace- (and optionally repo-) scoped."""
    request = CodeSearchRequest(query="chunk", candidate_count=5, top_k=1)
    statement = CodeRetrievalService._base(workspace_id, request).where(CodeChunk.id == chunk_id)
    if repository_ids is not None:
        statement = statement.where(CodeChunk.repository_id.in_(repository_ids))
    row = session.execute(statement).first()
    if row is None:
        raise LookupError("code chunk not found in workspace")
    return CodeRetrievalService._result(_Candidate(row[0], row[1], row[2], row[3]))


async def search_code(
    settings: Settings, workspace_id: UUID, request: CodeSearchRequest
) -> CodeSearchResponse:
    def run() -> CodeSearchResponse:
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        try:
            with Session(engine) as session:
                return CodeRetrievalService(settings).search(session, workspace_id, request)
        finally:
            engine.dispose()

    return await asyncio.to_thread(run)
