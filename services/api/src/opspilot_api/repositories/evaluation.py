"""Deterministic code-search and change-correlation evaluation (fixture provider, fake models).

Run: ``python -m opspilot_api.repositories.evaluation --dataset <dataset.json>`` (see README).

Seeds two workspaces with similar code into PostgreSQL/pgvector, runs every retrieval variant and
the deterministic correlation engine, and fails (non-zero exit) on cross-workspace leakage,
invalid citations, searchable excluded/secret content, or metrics below the documented fixture
thresholds. No network, GitHub credentials, paid embeddings, reranking, or LLM calls are used.
"""

import argparse
import json
import math
import secrets
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from opspilot_api.config import Settings, get_settings
from opspilot_api.incidents.models import (
    Incident,
    IngestedEvent,
    MonitoredService,
    Workspace,
)
from opspilot_api.investigations import models as investigation_models  # noqa: F401
from opspilot_api.repositories.correlation import rank_suspect_changes
from opspilot_api.repositories.fixture_provider import DEFAULT_FIXTURE, FixtureProvider
from opspilot_api.repositories.models import (
    CodeChunk,
    Repository,
    RepositoryCommit,
    RepositoryFile,
    RepositorySnapshot,
    ServiceRepositoryMapping,
)
from opspilot_api.repositories.policy import redact_code
from opspilot_api.repositories.retrieval import CodeRetrievalService
from opspilot_api.repositories.schemas import CodeSearchRequest, CodeVariant
from opspilot_api.repositories.sync import (
    sync_installation_repositories,
    sync_repository,
    upsert_installation,
)

# Documented fixture-baseline floors (measured values are reported alongside; see README).
SEARCH_THRESHOLDS: dict[str, dict[str, float]] = {
    "lexical": {"recall_at_5": 0.90, "mrr": 0.80, "no_answer_accuracy": 1.0},
    "vector": {"recall_at_5": 0.65, "mrr": 0.60},
    "hybrid_rrf": {"recall_at_5": 0.85, "mrr": 0.75, "no_answer_accuracy": 1.0},
    "hybrid_rrf_rerank": {"recall_at_5": 0.85, "mrr": 0.80, "no_answer_accuracy": 1.0},
}
CORRELATION_THRESHOLDS = {"recall_at_1": 0.85, "recall_at_3": 0.95, "no_change_accuracy": 1.0}
RANKED_KINDS_EXCLUDED = {"no_answer", "secret_probe"}


class SearchQuery(BaseModel):
    kind: str
    query: str
    relevant: list[str]
    service: str | None = None


class CorrelationCase(BaseModel):
    name: str
    service: str
    title: str
    description: str = ""
    events: list[list[str | None]] = Field(default_factory=list)
    opened_at: datetime
    expected_top: list[str]
    forbidden_top: list[str] = Field(default_factory=list)
    expect_none: bool = False
    workspace: str = "main"


class EvaluationDataset(BaseModel):
    secret_markers: list[str]
    search_queries: list[SearchQuery] = Field(min_length=15)
    correlation_cases: list[CorrelationCase] = Field(min_length=12)


@dataclass
class Seeded:
    main: Workspace
    decoy: Workspace
    services: dict[str, MonitoredService]
    main_repository_ids: set[UUID]
    decoy_repository_ids: set[UUID]
    provider: FixtureProvider
    heads: dict[UUID, str]
    files: dict[UUID, dict[str, str]]


def _sha(label: str) -> str:
    return (label * 40)[:40]


def _seed(session: Session, dataset: EvaluationDataset, settings: Settings) -> Seeded:
    offset = secrets.randbelow(10**9) * 1000
    data = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    for installation in data["installations"]:
        installation["id"] += offset
        for repo in installation["repositories"]:
            repo["id"] += offset
    provider = FixtureProvider(data=data)
    suffix = secrets.token_hex(5)
    main = Workspace(name=f"Code eval {suffix}", slug=f"code-eval-{suffix}")
    decoy = Workspace(name=f"Code decoy {suffix}", slug=f"code-decoy-{suffix}")
    session.add_all([main, decoy])
    session.commit()

    repos: dict[UUID, dict[str, Repository]] = {}
    for workspace, base in ((main, 1001), (decoy, 2002)):
        info = provider.get_installation(base + offset)
        installation = upsert_installation(session, workspace.id, info)
        session.commit()
        sync_installation_repositories(session, settings, provider, workspace.id, installation.id)
        repos[workspace.id] = {
            item.full_name: item
            for item in session.scalars(
                select(Repository).where(Repository.workspace_id == workspace.id)
            )
        }
        for repository in repos[workspace.id].values():
            sync_repository(session, settings, provider, workspace.id, repository.id)

    services: dict[str, MonitoredService] = {}

    def service(workspace: Workspace, key: str, name: str) -> MonitoredService:
        row = MonitoredService(
            workspace_id=workspace.id,
            name=name,
            slug=f"{name.lower()}-{suffix}",
            environment="production",
        )
        session.add(row)
        session.flush()
        services[key] = row
        return row

    checkout = service(main, "checkout", "Checkout")
    payments = service(main, "payments", "Payments")
    inventory = service(main, "inventory", "Inventory")
    service(main, "reporting", "Reporting")
    decoy_checkout = service(decoy, "decoy-checkout", "Checkout")
    mappings = [
        (main, checkout, repos[main.id]["acme/checkout-api"], None),
        (main, payments, repos[main.id]["acme/platform-monorepo"], "services/payments/"),
        (main, inventory, repos[main.id]["acme/platform-monorepo"], "services/inventory/"),
        (decoy, decoy_checkout, repos[decoy.id]["decoy-corp/checkout-api"], None),
    ]
    for workspace, svc, repository, prefix in mappings:
        session.add(
            ServiceRepositoryMapping(
                workspace_id=workspace.id,
                service_id=svc.id,
                repository_id=repository.id,
                environment="production",
                branch_pattern="main",
                path_prefix=prefix,
                path_prefix_key=prefix or "",
            )
        )
    session.commit()

    heads: dict[UUID, str] = {}
    files: dict[UUID, dict[str, str]] = {}
    for repository in [*repos[main.id].values(), *repos[decoy.id].values()]:
        refreshed = session.get(Repository, repository.id)
        if refreshed is None or refreshed.active_snapshot_id is None:
            raise RuntimeError(f"fixture repository {repository.full_name} was not indexed")
        snapshot = session.get(RepositorySnapshot, refreshed.active_snapshot_id)
        if snapshot is None:
            raise RuntimeError(f"fixture repository {repository.full_name} has no snapshot")
        heads[repository.id] = snapshot.commit_sha
        installation_provider_id = next(
            item["id"]
            for item in data["installations"]
            if any(r["id"] == repository.provider_repository_id for r in item["repositories"])
        )
        files[repository.id] = {
            row.path: redact_code(
                provider.get_file_content(
                    installation_provider_id,
                    repository.owner,
                    repository.name,
                    row.path,
                    snapshot.commit_sha,
                    10_000_000,
                ).decode("utf-8")
            )
            for row in session.scalars(
                select(RepositoryFile).where(RepositoryFile.snapshot_id == snapshot.id)
            )
        }
    return Seeded(
        main,
        decoy,
        services,
        {item.id for item in repos[main.id].values()},
        {item.id for item in repos[decoy.id].values()},
        provider,
        heads,
        files,
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else 0.0


def _matches(label: str, path: str, symbol: str | None) -> bool:
    expected_path, _, expected_symbol = label.partition("::")
    return path == expected_path and (not expected_symbol or expected_symbol == symbol)


def _evaluate_search(
    session: Session, settings: Settings, dataset: EvaluationDataset, seeded: Seeded
) -> dict[str, dict[str, float]]:
    report: dict[str, dict[str, float]] = {}
    retriever = CodeRetrievalService(settings)
    for variant in CodeVariant:
        ranks: list[int | None] = []
        latencies: list[float] = []
        no_answer_total = no_answer_correct = 0
        leakage = invalid_citations = excluded_or_secret = 0
        for item in dataset.search_queries:
            service = seeded.services[item.service] if item.service else None
            request = CodeSearchRequest(
                query=item.query,
                variant=variant,
                candidate_count=20,
                top_k=5,
                service_id=service.id if service else None,
            )
            tick = time.perf_counter()
            response = retriever.search(session, seeded.main.id, request)
            latencies.append((time.perf_counter() - tick) * 1000)
            for result in response.results:
                if result.repository_id not in seeded.main_repository_ids:
                    leakage += 1
                    continue
                source = seeded.files[result.repository_id].get(result.path)
                lines = source.split("\n") if source is not None else None
                short = result.commit_sha[:7]
                valid = (
                    source is not None
                    and lines is not None
                    and result.commit_sha == seeded.heads[result.repository_id]
                    and result.excerpt == "\n".join(lines[result.start_line - 1 : result.end_line])
                    and f"@{short}: {result.path}:{result.start_line}" in result.citation_label
                    and result.citation_label.startswith("[github.com/")
                )
                invalid_citations += 0 if valid else 1
                if source is None or any(m in result.excerpt for m in dataset.secret_markers):
                    excluded_or_secret += 1
            if item.kind == "secret_probe":
                continue
            if item.kind == "no_answer":
                no_answer_total += 1
                no_answer_correct += int(not response.results)
                continue
            rank = next(
                (
                    position
                    for position, result in enumerate(response.results, start=1)
                    if any(
                        _matches(label, result.path, result.symbol_name) for label in item.relevant
                    )
                ),
                None,
            )
            ranks.append(rank)
        found = [rank for rank in ranks if rank is not None]
        total = max(1, len(ranks))
        report[variant.value] = {
            "recall_at_1": sum(1 for r in found if r <= 1) / total,
            "recall_at_3": sum(1 for r in found if r <= 3) / total,
            "recall_at_5": sum(1 for r in found if r <= 5) / total,
            "mrr": sum(1 / r for r in found) / total,
            "ndcg_at_5": sum(1 / math.log2(r + 1) for r in found if r <= 5) / total,
            "no_answer_accuracy": no_answer_correct / max(1, no_answer_total),
            "cross_workspace_leakage_count": leakage,
            "invalid_citation_count": invalid_citations,
            "excluded_or_secret_count": excluded_or_secret,
            "p50_latency_ms": statistics.median(latencies),
            "p95_latency_ms": _percentile(latencies, 0.95),
        }
    return report


def _evaluate_correlation(
    session: Session, settings: Settings, dataset: EvaluationDataset, seeded: Seeded
) -> dict[str, float]:
    top1 = top3 = expected_total = 0
    forbidden_top = false_positives = invalid = leakage = no_change_total = no_change_correct = 0
    latencies: list[float] = []
    for case in dataset.correlation_cases:
        workspace = seeded.decoy if case.workspace == "decoy" else seeded.main
        service = seeded.services[case.service]
        incident = Incident(
            workspace_id=workspace.id,
            service_id=service.id,
            title=case.title,
            description=case.description or None,
            severity="sev2",
            status="open",
            source="manual",
            opened_at=case.opened_at,
        )
        session.add(incident)
        session.flush()
        for index, (event_type, signature) in enumerate(case.events):
            session.add(
                IngestedEvent(
                    workspace_id=workspace.id,
                    service_id=service.id,
                    incident_id=incident.id,
                    idempotency_key=f"{incident.id}-{index}",
                    source="eval",
                    event_type=str(event_type),
                    severity="sev2",
                    summary=str(event_type),
                    error_signature=signature,
                    attributes={},
                    fingerprint="f" * 64,
                    occurred_at=case.opened_at,
                )
            )
        session.commit()
        tick = time.perf_counter()
        result = rank_suspect_changes(session, settings, workspace.id, incident.id, limit=5)
        latencies.append((time.perf_counter() - tick) * 1000)
        allowed = (
            seeded.main_repository_ids if workspace is seeded.main else seeded.decoy_repository_ids
        )
        shas = [item.commit_sha for item in result.candidates]
        for candidate in result.candidates:
            leakage += int(candidate.repository_id not in allowed)
            known = session.scalar(
                select(RepositoryCommit.id).where(
                    RepositoryCommit.workspace_id == workspace.id,
                    RepositoryCommit.repository_id == candidate.repository_id,
                    RepositoryCommit.provider_commit_sha == candidate.commit_sha,
                )
            )
            invalid += int(
                known is None or f"@{candidate.commit_sha[:7]}:" not in candidate.citation_label
            )
        if case.expect_none:
            no_change_total += 1
            ok = not result.candidates
            no_change_correct += int(ok)
            false_positives += int(not ok)
            continue
        expected = {_sha(label) for label in case.expected_top}
        expected_total += 1
        top1 += int(bool(shas[:1]) and shas[0] in expected)
        top3 += int(any(sha in expected for sha in shas[:3]))
        if shas and shas[0] in {_sha(label) for label in case.forbidden_top}:
            forbidden_top += 1
    return {
        "recall_at_1": top1 / max(1, expected_total),
        "recall_at_3": top3 / max(1, expected_total),
        "no_change_accuracy": no_change_correct / max(1, no_change_total),
        "forbidden_top_count": forbidden_top,
        "no_change_false_positive_count": false_positives,
        "cross_workspace_leakage_count": leakage,
        "invalid_citation_count": invalid,
        "p50_latency_ms": statistics.median(latencies),
        "p95_latency_ms": _percentile(latencies, 0.95),
    }


def evaluate(settings: Settings, dataset: EvaluationDataset) -> dict[str, Any]:
    if (
        settings.EMBEDDING_PROVIDER != "fake"
        or settings.RERANK_PROVIDER != "fake"
        or settings.GITHUB_PROVIDER != "fixture"
    ):
        raise RuntimeError("evaluation requires the fixture repository provider and fake models")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            seeded = _seed(session, dataset, settings)
            ranked = [q for q in dataset.search_queries if q.kind not in RANKED_KINDS_EXCLUDED]
            chunk_count = len(
                list(
                    session.scalars(
                        select(CodeChunk.id).where(CodeChunk.workspace_id == seeded.main.id)
                    )
                )
            )
            return {
                "dataset": {
                    "search_queries": len(dataset.search_queries),
                    "ranked_queries": len(ranked),
                    "correlation_cases": len(dataset.correlation_cases),
                    "main_workspace_chunks": chunk_count,
                },
                "search": _evaluate_search(session, settings, dataset, seeded),
                "correlation": _evaluate_correlation(session, settings, dataset, seeded),
            }
    finally:
        engine.dispose()


def _table(rows: dict[str, dict[str, float]]) -> list[str]:
    lines = [
        "| Variant | R@1 | R@3 | R@5 | MRR | NDCG@5 | No-answer | Leakage | Invalid cites "
        "| Excluded/secret | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in rows.items():
        lines.append(
            f"| {name} | {item['recall_at_1']:.3f} | {item['recall_at_3']:.3f} | "
            f"{item['recall_at_5']:.3f} | {item['mrr']:.3f} | {item['ndcg_at_5']:.3f} | "
            f"{item['no_answer_accuracy']:.3f} | {item['cross_workspace_leakage_count']} | "
            f"{item['invalid_citation_count']} | {item['excluded_or_secret_count']} | "
            f"{item['p50_latency_ms']:.2f} | {item['p95_latency_ms']:.2f} |"
        )
    return lines


def markdown(report: dict[str, Any]) -> str:
    dataset, corr = report["dataset"], report["correlation"]
    return "\n".join(
        [
            "# Repository evaluation",
            "",
            f"Search queries: {dataset['search_queries']} ({dataset['ranked_queries']} ranked) | "
            f"correlation cases: {dataset['correlation_cases']}",
            "",
            "## Code search",
            "",
            *_table(report["search"]),
            "",
            "## Change correlation (deterministic, no LLM)",
            "",
            "| R@1 | R@3 | No-change | Forbidden top-1 | No-change FP | Leakage | Invalid cites "
            "| P50 ms | P95 ms |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            f"| {corr['recall_at_1']:.3f} | {corr['recall_at_3']:.3f} | "
            f"{corr['no_change_accuracy']:.3f} | {corr['forbidden_top_count']} | "
            f"{corr['no_change_false_positive_count']} | {corr['cross_workspace_leakage_count']} | "
            f"{corr['invalid_citation_count']} | {corr['p50_latency_ms']:.2f} | "
            f"{corr['p95_latency_ms']:.2f} |",
            "",
        ]
    )


def enforce(report: dict[str, Any]) -> None:
    failures: list[str] = []
    for name, metrics in report["search"].items():
        for key, label in (
            ("cross_workspace_leakage_count", "cross-workspace leakage"),
            ("invalid_citation_count", "invalid citations"),
            ("excluded_or_secret_count", "excluded/secret content searchable"),
        ):
            if metrics[key]:
                failures.append(f"search/{name}: {label} ({metrics[key]})")
        for metric, minimum in SEARCH_THRESHOLDS[name].items():
            if metrics[metric] < minimum:
                failures.append(f"search/{name}: {metric} {metrics[metric]:.3f} < {minimum:.3f}")
    corr = report["correlation"]
    for key, label in (
        ("cross_workspace_leakage_count", "cross-workspace leakage"),
        ("invalid_citation_count", "invalid citations"),
        ("forbidden_top_count", "forbidden change ranked first"),
        ("no_change_false_positive_count", "candidate returned where none is expected"),
    ):
        if corr[key]:
            failures.append(f"correlation: {label} ({corr[key]})")
    for metric, minimum in CORRELATION_THRESHOLDS.items():
        if corr[metric] < minimum:
            failures.append(f"correlation: {metric} {corr[metric]:.3f} < {minimum:.3f}")
    if failures:
        raise RuntimeError("repository evaluation failed: " + "; ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    dataset = EvaluationDataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))
    report = evaluate(get_settings(), dataset)
    text = json.dumps(report, indent=2) + "\n"
    summary = markdown(report)
    if args.json_output:
        args.json_output.write_text(text, encoding="utf-8")
    else:
        print(text)
    if args.markdown_output:
        args.markdown_output.write_text(summary, encoding="utf-8")
    else:
        print(summary)
    enforce(report)


if __name__ == "__main__":
    main()
