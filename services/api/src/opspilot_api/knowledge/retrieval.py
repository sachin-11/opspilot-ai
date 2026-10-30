"""Workspace-first lexical, vector, hybrid RRF, and optional reranked retrieval."""

import asyncio
import hashlib
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Select, create_engine, func, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.incidents.models import MonitoredService
from opspilot_api.investigations.safety import redact_text
from opspilot_api.knowledge.embeddings import EmbeddingRouter
from opspilot_api.knowledge.models import (
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalLog,
)
from opspilot_api.knowledge.query_enhancement import QueryEnhancer
from opspilot_api.knowledge.rerank import (
    Reranker,
    RerankItem,
    RetryableRerankError,
    create_reranker,
)
from opspilot_api.knowledge.schemas import (
    EXPERIMENTAL_VARIANTS,
    RetrievalVariant,
    SearchDiagnostics,
    SearchRequest,
    SearchResponse,
    SearchResult,
)


@dataclass
class Candidate:
    chunk: KnowledgeChunk
    document: KnowledgeDocument
    service_name: str | None
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


def citation_label(candidate: Candidate) -> str:
    parts = [f"{candidate.document.document_type.title()}: {candidate.document.title}"]
    if candidate.chunk.page_number:
        parts.append(f"page {candidate.chunk.page_number}")
    if candidate.chunk.heading_path:
        parts.append(" > ".join(candidate.chunk.heading_path))
    return f"[{', '.join(parts)}]"


class RetrievalService:
    def __init__(
        self,
        settings: Settings,
        embeddings: EmbeddingRouter | None = None,
        reranker: Reranker | None = None,
        enhancer: QueryEnhancer | None = None,
    ) -> None:
        self.settings = settings
        self.embeddings = embeddings or EmbeddingRouter(settings)
        self.reranker = reranker or create_reranker(settings)
        self.enhancer = enhancer or QueryEnhancer(settings)

    def _base(
        self, workspace_id: UUID, request: SearchRequest
    ) -> Select[KnowledgeChunk, KnowledgeDocument, str]:
        query = (
            select(KnowledgeChunk, KnowledgeDocument, MonitoredService.name)
            .join(
                KnowledgeDocument,
                (KnowledgeDocument.id == KnowledgeChunk.document_id)
                & (KnowledgeDocument.workspace_id == KnowledgeChunk.workspace_id),
            )
            .outerjoin(
                MonitoredService,
                (MonitoredService.id == KnowledgeChunk.service_id)
                & (MonitoredService.workspace_id == workspace_id),
            )
            .where(
                KnowledgeChunk.workspace_id == workspace_id,
                KnowledgeDocument.workspace_id == workspace_id,
                KnowledgeDocument.status == DocumentStatus.READY.value,
                KnowledgeDocument.archived_at.is_(None),
                KnowledgeDocument.current_version_id == KnowledgeChunk.document_version_id,
            )
        )
        if request.service_id:
            query = query.where(KnowledgeChunk.service_id == request.service_id)
        if request.document_types:
            query = query.where(
                KnowledgeDocument.document_type.in_([item.value for item in request.document_types])
            )
        if request.tags:
            query = query.where(KnowledgeDocument.tags.contains(request.tags))
        return query

    def _lexical(
        self, session: Session, workspace_id: UUID, request: SearchRequest, text: str
    ) -> list[Candidate]:
        parsed = func.websearch_to_tsquery("english", text)
        score = func.ts_rank_cd(KnowledgeChunk.search_vector, parsed).label("lexical_score")
        statement = (
            self._base(workspace_id, request)
            .add_columns(score)
            .where(KnowledgeChunk.search_vector.op("@@")(parsed))
            .order_by(score.desc(), KnowledgeChunk.id)
            .limit(request.candidate_count)
        )
        return [
            Candidate(
                chunk=row[0],
                document=row[1],
                service_name=row[2],
                lexical_rank=index,
                lexical_score=float(row[3]),
            )
            for index, row in enumerate(session.execute(statement), start=1)
        ]

    def _vector(
        self, session: Session, workspace_id: UUID, request: SearchRequest, vector: list[float]
    ) -> list[Candidate]:
        distance = KnowledgeChunk.embedding.cosine_distance(vector).label("distance")
        statement = (
            self._base(workspace_id, request)
            .add_columns(distance)
            .where(
                KnowledgeChunk.embedding_provider == self.settings.EMBEDDING_PROVIDER,
                KnowledgeChunk.embedding_model == self.settings.EMBEDDING_MODEL,
                KnowledgeChunk.embedding_dimensions == self.settings.EMBEDDING_DIMENSIONS,
            )
            .order_by(distance, KnowledgeChunk.id)
            .limit(request.candidate_count)
        )
        return [
            Candidate(
                chunk=row[0],
                document=row[1],
                service_name=row[2],
                vector_rank=index,
                vector_score=max(-1.0, 1.0 - float(row[3])),
            )
            for index, row in enumerate(session.execute(statement), start=1)
        ]

    @staticmethod
    def _rrf(
        lexical: list[Candidate], vector: list[Candidate], constant: int = 60
    ) -> list[Candidate]:
        combined: dict[UUID, Candidate] = {}
        for source in (lexical, vector):
            for item in source:
                existing = combined.get(item.chunk.id)
                if existing is None:
                    existing = Candidate(item.chunk, item.document, item.service_name)
                    combined[item.chunk.id] = existing
                if item.lexical_rank is not None:
                    existing.lexical_rank = item.lexical_rank
                    existing.lexical_score = item.lexical_score
                if item.vector_rank is not None:
                    existing.vector_rank = item.vector_rank
                    existing.vector_score = item.vector_score
        for item in combined.values():
            score = 0.0
            if item.lexical_rank:
                score += 1 / (constant + item.lexical_rank)
            if item.vector_rank:
                score += 1 / (constant + item.vector_rank)
            item.rrf_score = score
        return sorted(
            combined.values(), key=lambda item: (-(item.rrf_score or 0), str(item.chunk.id))
        )

    def search(
        self,
        session: Session,
        workspace_id: UUID,
        request: SearchRequest,
        investigation_id: UUID | None = None,
    ) -> SearchResponse:
        if len(request.query) > self.settings.KNOWLEDGE_MAX_QUERY_LENGTH:
            raise ValueError("query exceeds configured length limit")
        if request.candidate_count > self.settings.KNOWLEDGE_MAX_CANDIDATES:
            raise ValueError("candidate count exceeds configured limit")
        if request.top_k > self.settings.KNOWLEDGE_MAX_TOP_K:
            raise ValueError("top_k exceeds configured limit")
        experimental = request.variant in EXPERIMENTAL_VARIANTS
        started = time.perf_counter()
        # Experimental variants only change the query text used for candidate generation;
        # tenant filters, current-version gating, and citations are identical to baseline.
        lexical_text = request.query
        embed_text = request.query
        if request.variant is RetrievalVariant.HYBRID_RRF_REWRITE:
            lexical_text = embed_text = self.enhancer.rewrite(request.query)
        elif request.variant is RetrievalVariant.HYBRID_RRF_HYDE:
            embed_text = self.enhancer.hyde(request.query)
        embedding_latency: float | None = None
        rerank_latency: float | None = None
        rerank_fallback = False
        vector: list[float] | None = None
        if request.variant is not RetrievalVariant.LEXICAL:
            embedding_started = time.perf_counter()
            vector = self.embeddings.embed([embed_text])[0]
            embedding_latency = (time.perf_counter() - embedding_started) * 1000
        lexical = (
            self._lexical(session, workspace_id, request, lexical_text)
            if request.variant is not RetrievalVariant.VECTOR
            else []
        )
        vector_results = (
            self._vector(session, workspace_id, request, vector) if vector is not None else []
        )
        if request.variant is RetrievalVariant.LEXICAL:
            candidates = lexical
        elif request.variant is RetrievalVariant.VECTOR:
            candidates = vector_results
        else:
            candidates = self._rrf(lexical, vector_results)
        if request.variant is RetrievalVariant.HYBRID_RRF_RERANK:
            rerank_started = time.perf_counter()
            bounded = candidates[: self.settings.RERANK_CANDIDATE_LIMIT]
            try:
                scores = self.reranker.rerank(
                    request.query,
                    [RerankItem(str(item.chunk.id), item.chunk.content) for item in bounded],
                )
                for item in bounded:
                    item.rerank_score = scores[str(item.chunk.id)]
                candidates = sorted(
                    bounded,
                    key=lambda item: (-(item.rerank_score or 0), -(item.rrf_score or 0)),
                )
            except RetryableRerankError:
                rerank_fallback = True
            rerank_latency = (time.perf_counter() - rerank_started) * 1000
        results = [self._result(item) for item in candidates[: request.top_k]]
        results = [item for item in results if self._relevant(item, request.variant)]
        latency = (time.perf_counter() - started) * 1000
        safe_query = redact_text(request.query)[: self.settings.KNOWLEDGE_MAX_QUERY_LENGTH]
        log = RetrievalLog(
            id=uuid4(),
            workspace_id=workspace_id,
            investigation_id=investigation_id,
            query=safe_query,
            query_hash=hashlib.sha256(safe_query.encode()).hexdigest(),
            retrieval_variant=request.variant.value,
            filters={
                "service_id": str(request.service_id) if request.service_id else None,
                "document_types": [item.value for item in request.document_types],
                "tags": request.tags,
                "experimental": experimental,
            },
            top_k=request.top_k,
            candidate_count=request.candidate_count,
            result_chunk_ids=[str(item.chunk_id) for item in results],
            latency_ms=latency,
            embedding_latency_ms=embedding_latency,
            rerank_latency_ms=rerank_latency,
            rerank_fallback_used=rerank_fallback,
        )
        session.add(log)
        session.commit()
        return SearchResponse(
            results=results,
            diagnostics=SearchDiagnostics(
                variant=request.variant,
                experimental=experimental,
                candidate_count=len(candidates),
                latency_ms=round(latency, 3),
                embedding_latency_ms=round(embedding_latency, 3)
                if embedding_latency is not None
                else None,
                rerank_latency_ms=round(rerank_latency, 3) if rerank_latency is not None else None,
                rerank_fallback_used=rerank_fallback,
                low_relevance=not results,
            ),
        )

    def _relevant(self, result: SearchResult, variant: RetrievalVariant) -> bool:
        if variant is RetrievalVariant.LEXICAL:
            score = result.lexical_score or 0
        elif variant is RetrievalVariant.VECTOR:
            return (result.vector_score or -1) >= self.settings.KNOWLEDGE_MIN_VECTOR_RELEVANCE
        elif variant is RetrievalVariant.HYBRID_RRF_RERANK:
            if result.rerank_score is not None:
                return (
                    result.rerank_score >= self.settings.KNOWLEDGE_MIN_RERANK_RELEVANCE
                    or result.lexical_rank is not None
                )
            return (
                result.lexical_rank is not None
                or (result.vector_score or -1) >= self.settings.KNOWLEDGE_MIN_VECTOR_RELEVANCE
            )
        else:
            return (
                result.lexical_rank is not None
                or (result.vector_score or -1) >= self.settings.KNOWLEDGE_MIN_VECTOR_RELEVANCE
            )
        return score >= self.settings.KNOWLEDGE_MIN_RELEVANCE

    @staticmethod
    def _result(item: Candidate) -> SearchResult:
        return SearchResult(
            chunk_id=item.chunk.id,
            document_id=item.document.id,
            document_version_id=item.chunk.document_version_id,
            document_title=item.document.title,
            document_type=item.document.document_type,
            service_id=item.chunk.service_id,
            service_name=item.service_name,
            heading_path=item.chunk.heading_path,
            page_number=item.chunk.page_number,
            excerpt=item.chunk.content[:1000],
            lexical_rank=item.lexical_rank,
            lexical_score=item.lexical_score,
            vector_rank=item.vector_rank,
            vector_score=item.vector_score,
            rrf_score=item.rrf_score,
            rerank_score=item.rerank_score,
            citation_label=citation_label(item),
        )


def get_chunk(
    session: Session, workspace_id: UUID, chunk_id: UUID
) -> tuple[KnowledgeChunk, KnowledgeDocument, str]:
    row = session.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(
            KnowledgeDocument,
            (KnowledgeDocument.id == KnowledgeChunk.document_id)
            & (KnowledgeDocument.workspace_id == workspace_id),
        )
        .where(
            KnowledgeChunk.id == chunk_id,
            KnowledgeChunk.workspace_id == workspace_id,
            KnowledgeDocument.workspace_id == workspace_id,
            KnowledgeDocument.status == DocumentStatus.READY.value,
            KnowledgeDocument.archived_at.is_(None),
            KnowledgeDocument.current_version_id == KnowledgeChunk.document_version_id,
        )
    ).one_or_none()
    if row is None:
        raise LookupError("knowledge chunk not found in workspace")
    candidate = Candidate(row[0], row[1], None)
    return row[0], row[1], citation_label(candidate)


def _search_database(
    settings: Settings,
    workspace_id: UUID,
    request: SearchRequest,
    investigation_id: UUID | None,
) -> SearchResponse:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            return RetrievalService(settings).search(
                session, workspace_id, request, investigation_id
            )
    finally:
        engine.dispose()


async def search_knowledge(
    settings: Settings,
    workspace_id: UUID,
    request: SearchRequest,
    investigation_id: UUID | None = None,
) -> SearchResponse:
    return await asyncio.to_thread(
        _search_database, settings, workspace_id, request, investigation_id
    )
