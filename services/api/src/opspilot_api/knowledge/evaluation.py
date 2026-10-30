"""Deterministic PostgreSQL/pgvector retrieval evaluation command."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from opspilot_api.config import Settings, get_settings
from opspilot_api.incidents.models import Environment, MonitoredService, Workspace
from opspilot_api.investigations import models as investigation_models  # noqa: F401
from opspilot_api.knowledge.embeddings import EmbeddingRouter
from opspilot_api.knowledge.models import (
    DocumentStatus,
    DocumentType,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    VersionStatus,
)
from opspilot_api.knowledge.retrieval import RetrievalService
from opspilot_api.knowledge.schemas import (
    BASELINE_VARIANTS,
    EXPERIMENTAL_VARIANTS,
    RetrievalVariant,
    SearchRequest,
)


class FixtureDocument(BaseModel):
    label: str
    title: str
    service: str | None
    content: str


class FixtureQuery(BaseModel):
    query: str
    relevant: list[str]


class EvaluationFixture(BaseModel):
    documents: list[FixtureDocument]
    queries: list[FixtureQuery] = Field(min_length=20)


@dataclass(frozen=True)
class SeededFixture:
    workspace_id: UUID
    labels_by_document: dict[UUID, str]
    allowed_documents: set[UUID]


BASELINE_THRESHOLDS: dict[str, dict[str, float]] = {
    "lexical": {"recall_at_5": 0.35, "mrr": 0.35},
    "vector": {"recall_at_5": 0.60, "mrr": 0.45},
    "hybrid_rrf": {"recall_at_5": 0.70, "mrr": 0.55},
    "hybrid_rrf_rerank": {"recall_at_5": 0.70, "mrr": 0.55},
}


def _seed(session: Session, fixture: EvaluationFixture, settings: Settings) -> SeededFixture:
    suffix = uuid4().hex[:12]
    workspace = Workspace(name=f"Retrieval evaluation {suffix}", slug=f"retrieval-eval-{suffix}")
    decoy_workspace = Workspace(name=f"Retrieval decoy {suffix}", slug=f"retrieval-decoy-{suffix}")
    session.add_all([workspace, decoy_workspace])
    session.flush()
    services: dict[str, MonitoredService] = {}
    for slug in sorted({item.service for item in fixture.documents if item.service}):
        service = MonitoredService(
            workspace_id=workspace.id,
            name=slug.title(),
            slug=slug,
            description="Deterministic retrieval evaluation service",
            environment=Environment.PRODUCTION.value,
        )
        session.add(service)
        services[slug] = service
    session.flush()
    embedder = EmbeddingRouter(settings)
    labels: dict[UUID, str] = {}
    allowed: set[UUID] = set()
    for item in fixture.documents:
        document = KnowledgeDocument(
            workspace_id=workspace.id,
            service_id=services[item.service].id if item.service else None,
            title=item.title,
            slug=f"{item.label}-{suffix}",
            description="Synthetic deterministic evaluation fixture",
            document_type=DocumentType.RUNBOOK.value,
            status=DocumentStatus.READY.value,
            tags=["evaluation", item.service or "shared"],
        )
        session.add(document)
        session.flush()
        version = KnowledgeDocumentVersion(
            workspace_id=workspace.id,
            document_id=document.id,
            version_number=1,
            mime_type="text/plain",
            content_hash=hashlib.sha256(item.content.encode()).hexdigest(),
            extracted_text_hash=hashlib.sha256(item.content.encode()).hexdigest(),
            byte_size=len(item.content.encode()),
            embedding_provider=settings.EMBEDDING_PROVIDER,
            embedding_model=settings.EMBEDDING_MODEL,
            embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
            chunk_count=1,
            status=VersionStatus.READY.value,
        )
        session.add(version)
        session.flush()
        chunk = KnowledgeChunk(
            workspace_id=workspace.id,
            document_id=document.id,
            document_version_id=version.id,
            service_id=document.service_id,
            chunk_index=0,
            content=f"{item.title}\n\n{item.content}",
            content_hash=hashlib.sha256(item.content.encode()).hexdigest(),
            token_count=len(item.content.split()),
            heading_path=[item.title],
            embedding=embedder.embed([f"{item.title} {item.content}"])[0],
            embedding_provider=settings.EMBEDDING_PROVIDER,
            embedding_model=settings.EMBEDDING_MODEL,
            embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
            chunk_metadata={"evaluation_label": item.label},
        )
        session.add(chunk)
        document.current_version_id = version.id
        labels[document.id] = item.label
        allowed.add(document.id)
    # A highly similar chunk in another workspace makes leakage detectable.
    first = fixture.documents[0]
    decoy = KnowledgeDocument(
        workspace_id=decoy_workspace.id,
        title="Cross-workspace decoy",
        slug=f"cross-workspace-decoy-{suffix}",
        document_type=DocumentType.RUNBOOK.value,
        status=DocumentStatus.READY.value,
        tags=["evaluation"],
    )
    session.add(decoy)
    session.flush()
    decoy_version = KnowledgeDocumentVersion(
        workspace_id=decoy_workspace.id,
        document_id=decoy.id,
        version_number=1,
        mime_type="text/plain",
        content_hash=hashlib.sha256(first.content.encode()).hexdigest(),
        byte_size=len(first.content.encode()),
        embedding_provider=settings.EMBEDDING_PROVIDER,
        embedding_model=settings.EMBEDDING_MODEL,
        embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
        chunk_count=1,
        status=VersionStatus.READY.value,
    )
    session.add(decoy_version)
    session.flush()
    session.add(
        KnowledgeChunk(
            workspace_id=decoy_workspace.id,
            document_id=decoy.id,
            document_version_id=decoy_version.id,
            chunk_index=0,
            content=first.content,
            content_hash=hashlib.sha256(first.content.encode()).hexdigest(),
            token_count=len(first.content.split()),
            heading_path=["Decoy"],
            embedding=embedder.embed([first.content])[0],
            embedding_provider=settings.EMBEDDING_PROVIDER,
            embedding_model=settings.EMBEDDING_MODEL,
            embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
            chunk_metadata={"evaluation_label": "decoy"},
        )
    )
    decoy.current_version_id = decoy_version.id
    session.commit()
    return SeededFixture(workspace.id, labels, allowed)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _metrics(
    ranks: list[int | None], relevant_counts: list[int], latencies: list[float]
) -> dict[str, float]:
    answer_indexes = [index for index, count in enumerate(relevant_counts) if count]
    denominator = max(1, len(answer_indexes))
    answer_ranks: list[int] = [
        rank for index in answer_indexes if (rank := ranks[index]) is not None
    ]
    recalls = {
        f"recall_at_{k}": sum(1 for rank in answer_ranks if rank <= k) / denominator
        for k in (1, 3, 5)
    }
    mrr = sum(1 / rank for rank in answer_ranks) / denominator
    ndcg = sum(1 / math.log2(rank + 1) for rank in answer_ranks if rank <= 5) / denominator
    return {
        **recalls,
        "mrr": mrr,
        "ndcg_at_5": ndcg,
        "p50_latency_ms": statistics.median(latencies),
        "p95_latency_ms": _percentile(latencies, 0.95),
    }


def _run_variant(
    session: Session,
    settings: Settings,
    fixture: EvaluationFixture,
    seeded: SeededFixture,
    variant: RetrievalVariant,
) -> dict[str, float]:
    ranks: list[int | None] = []
    relevant_counts: list[int] = []
    latencies: list[float] = []
    no_answer_total = 0
    no_answer_correct = 0
    leakage = 0
    missing_citations = 0
    retriever = RetrievalService(settings)
    for query in fixture.queries:
        started = time.perf_counter()
        response = retriever.search(
            session,
            seeded.workspace_id,
            SearchRequest(
                query=query.query,
                variant=variant,
                candidate_count=20,
                top_k=5,
            ),
        )
        latencies.append((time.perf_counter() - started) * 1000)
        labels = [seeded.labels_by_document.get(item.document_id) for item in response.results]
        relevant_counts.append(len(query.relevant))
        if query.relevant:
            ranks.append(
                next(
                    (index for index, label in enumerate(labels, 1) if label in query.relevant),
                    None,
                )
            )
        else:
            ranks.append(None)
            no_answer_total += 1
            no_answer_correct += int(not response.results)
        leakage += sum(
            1 for item in response.results if item.document_id not in seeded.allowed_documents
        )
        missing_citations += sum(1 for item in response.results if not item.citation_label)
    metrics = _metrics(ranks, relevant_counts, latencies)
    metrics.update(
        no_answer_accuracy=no_answer_correct / max(1, no_answer_total),
        cross_workspace_leakage_count=leakage,
        missing_citation_count=missing_citations,
    )
    return metrics


def evaluate(settings: Settings, fixture: EvaluationFixture) -> dict[str, Any]:
    if settings.EMBEDDING_PROVIDER != "fake" or settings.RERANK_PROVIDER != "fake":
        raise RuntimeError(
            "evaluation requires deterministic fake embedding and reranking providers"
        )
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    report: dict[str, Any] = {
        "dataset_queries": len(fixture.queries),
        "variants": {},
        "experimental": {},
    }
    try:
        with Session(engine) as session:
            seeded = _seed(session, fixture, settings)
            for variant in BASELINE_VARIANTS:
                report["variants"][variant.value] = _run_variant(
                    session, settings, fixture, seeded, variant
                )
            # Experimental variants use the fake model provider and are reported separately.
            experimental_settings = settings.model_copy(
                update={"KNOWLEDGE_EXPERIMENTAL_QUERY_ENHANCEMENT": True}
            )
            for variant in EXPERIMENTAL_VARIANTS:
                report["experimental"][variant.value] = _run_variant(
                    session, experimental_settings, fixture, seeded, variant
                )
    finally:
        engine.dispose()
    return report


def _table(variants: dict[str, Any]) -> list[str]:
    lines = [
        "| Variant | R@1 | R@3 | R@5 | MRR | NDCG@5 | No-answer | Leakage | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in variants.items():
        lines.append(
            f"| {name} | {item['recall_at_1']:.3f} | {item['recall_at_3']:.3f} | "
            f"{item['recall_at_5']:.3f} | {item['mrr']:.3f} | {item['ndcg_at_5']:.3f} | "
            f"{item['no_answer_accuracy']:.3f} | {item['cross_workspace_leakage_count']} | "
            f"{item['p50_latency_ms']:.2f} | {item['p95_latency_ms']:.2f} |"
        )
    return lines


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Retrieval evaluation",
        "",
        f"Dataset queries: {report['dataset_queries']}",
        "",
        *_table(report["variants"]),
        "",
        "## Experimental query enhancement (fake model provider, reported separately)",
        "",
        "Disabled by default; not part of the baseline thresholds.",
        "",
        *_table(report["experimental"]),
    ]
    return "\n".join(lines) + "\n"


def _enforce(report: dict[str, Any]) -> None:
    failures: list[str] = []
    for name, metrics in {**report["variants"], **report["experimental"]}.items():
        if metrics["cross_workspace_leakage_count"]:
            failures.append(f"{name}: cross-workspace leakage")
        if metrics["missing_citation_count"]:
            failures.append(f"{name}: missing citations")
        for metric, minimum in BASELINE_THRESHOLDS.get(name, {}).items():
            if metrics[metric] < minimum:
                failures.append(f"{name}: {metric} {metrics[metric]:.3f} < {minimum:.3f}")
    if failures:
        raise RuntimeError("retrieval evaluation failed: " + "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    fixture = EvaluationFixture.model_validate_json(args.dataset.read_text(encoding="utf-8"))
    report = evaluate(get_settings(), fixture)
    json_text = json.dumps(report, indent=2) + "\n"
    markdown_text = _markdown(report)
    if args.json_output:
        args.json_output.write_text(json_text, encoding="utf-8")
    else:
        print(json_text)
    if args.markdown_output:
        args.markdown_output.write_text(markdown_text, encoding="utf-8")
    else:
        print(markdown_text)
    _enforce(report)


if __name__ == "__main__":
    main()
