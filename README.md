# OpsPilot AI

OpsPilot AI is a multi-tenant incident-response platform. This repository contains **Module 5: the observability event pipeline (signed webhooks, Kafka-compatible streaming, CloudWatch read tools)**, built on the Module 0–4 foundation, including the read-only GitHub integration.

## Module 3, 4, and 5 capabilities

Module 5 ingests production signals through signed webhooks and a Kafka-compatible broker, normalizes and de-duplicates them, feeds the existing incident correlation, and gives investigations bounded, read-only log/metric/alarm tools. Automated remediation is not implemented.

Module 4 adds a **strictly read-only** GitHub App integration: repository sync, a bounded source-code index with citations, commit/pull-request/deployment tracking, deterministic incident-to-change correlation, and read-only LangGraph code-evidence tools. **Module 4 cannot modify GitHub repositories** (no branches, commits, pull requests, comments, patches, or workflow runs); fix generation and human-approved fix PRs belong to a later module.

Included: the incident and human-reviewed investigation flows plus private text/Markdown/text-PDF ingestion, atomic document versions, deterministic redacted chunks, PostgreSQL full-text and pgvector search, hybrid RRF, optional reranking, citations, evaluation tooling, safe investigation RAG tools, real-time progress, and knowledge-management UI.

### Intentionally deferred to later modules

Fix generation and pull-request creation, OCR, DOCX, web scraping, Slack, production authentication/authorization, billing, Kafka, Kubernetes, and automated remediation. **All remediation remains advisory; OpsPilot never executes production changes.**

## Architecture

```
Browser -> web (Next.js) ; Browser -> api (FastAPI) -> postgres, redis/RQ -> worker -> LangGraph -> postgres checkpoints
```

Details and a Mermaid diagram: [docs/architecture.md](docs/architecture.md).

```
apps/web/          Next.js (App Router), Tailwind, Vitest
services/api/      FastAPI app, RQ worker, Alembic migrations, pytest
infra/docker/      Dockerfiles
scripts/           PowerShell helper scripts
compose.yaml       web, api, worker, postgres (pgvector), redis
```

## Prerequisites

- Docker Desktop (Compose v2)
- Node.js 22+ (includes Corepack; pnpm is run as `corepack pnpm`, no global install)
- [uv](https://docs.astral.sh/uv/) (it provisions Python 3.12)

## Setup (Windows PowerShell)

```powershell
cd opspilot-ai
scripts\bootstrap.ps1     # creates .env from .env.example, installs locked deps
scripts\dev.ps1           # builds + starts the stack, waits for health, applies migrations
```

Stop with `scripts\dev.ps1 -Down` (volumes are kept).

### Portable equivalent

```bash
cp .env.example .env
(cd services/api && uv sync --frozen)
(cd apps/web && corepack pnpm install --frozen-lockfile)
docker compose up -d --build --wait
docker compose exec api alembic upgrade head
docker compose exec api python -m opspilot_api.investigations.checkpoint_setup
```

## Local service URLs

| Service | URL |
| --- | --- |
| Web dashboard | http://localhost:3300 |
| API docs | http://localhost:8300/docs |
| Liveness | http://localhost:8300/health/live |
| Readiness | http://localhost:8300/health/ready |
| Metadata | http://localhost:8300/api/v1/meta |
| PostgreSQL (host) | 127.0.0.1:5450 |
| Redis (host) | 127.0.0.1:6390 |

Ports are bound to loopback and configurable through `*_HOST_PORT` in `.env`. If you change `API_HOST_PORT`/`WEB_HOST_PORT`, update `NEXT_PUBLIC_API_BASE_URL` and `CORS_ORIGINS` to match, then rebuild (`docker compose up -d --build`).

## Docker Compose commands

```bash
docker compose up -d --build --wait   # start and wait for health
docker compose ps                     # status / health
docker compose logs -f api worker     # follow logs
docker compose stop redis             # simulate an outage (readiness -> 503)
docker compose start redis
docker compose down                   # stop (keeps volumes); add -v to delete data
```

## Migrations

```powershell
scripts\migrate.ps1
# or: docker compose exec api alembic upgrade head
```

Migration `0001` runs `CREATE EXTENSION IF NOT EXISTS vector`. Verify:
`docker compose exec postgres psql -U opspilot -d opspilot -c "\dx vector"`.

Migration `0002` creates the Module 1 domain. Migration `0003` creates investigations, steps, evidence, and append-only reviews, including a partial unique index allowing only one active investigation per incident. LangGraph's checkpoint tables are initialized explicitly with the checkpoint setup command above. Validate reversibility on a disposable database with:

```bash
docker compose exec api alembic downgrade 0001
docker compose exec api alembic upgrade head
```

## Demo seed and local flow

The seed is disabled when `APP_ENV=production` and is safe to run repeatedly:

```bash
docker compose exec api python -m opspilot_api.incidents.seed
```

Open http://localhost:3300, create a manual incident, move it through a valid status transition, change severity, and add a timeline note. The UI uses REST as its source of truth and refreshes affected TanStack Query caches from workspace-scoped WebSocket events.

## Investigation lifecycle

Open an incident and choose **Start investigation**. The API persists a queued run and returns 202; RQ runs the graph under the investigation's stable checkpoint thread. It loads context, triages, plans bounded tool calls, gathers sanitized evidence, generates and critiques hypotheses, then drafts a cited report and pauses at `awaiting_review`.

- **Approve** resumes the checkpoint and completes the run.
- **Request revision** requires feedback and resumes only at the revision node; evidence collection is not repeated. Two revisions are allowed by default.
- **Reject** requires feedback, preserves the report/history, and closes the run.
- Cancellation is idempotent. A failed run is never overwritten; retry creates a linked investigation.

WebSocket events invalidate investigation caches. While the socket is degraded, active investigations poll REST; terminal runs stop polling.

## Model configuration and limits

Local and CI mode uses `AI_PRIMARY_PROVIDER=fake` with `AI_PRIMARY_MODEL=deterministic-v1`; it is deterministic and never calls an external model. A primary and optional fallback are selected entirely through `AI_*` environment settings. The current repository ships the fake adapter and provider interface; configure only an adapter implemented in `investigations/providers.py`. Fallback is attempted only for timeout/rate-limit/temporary failures, never for credentials, invalid structured output, unsafe tool calls, isolation failures, or spend limits.

The `.env.example` documents request timeouts/retries and hard limits for runtime, model calls, tool calls, evidence count, tool-output bytes, input/output tokens, configured price rates, total estimated cost, and revisions. Estimated cost is unavailable when usage or rates are unavailable. Crossing a limit stops further model work, persists a safe failure, and publishes `investigation.limit_reached`.

Optional LangSmith tracing is disabled by default. Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` to enable the LangGraph/LangChain integration; do not attach unredacted evidence or secrets to trace metadata. Missing tracing configuration never blocks an investigation.

## Event ingestion

Every submission requires an `Idempotency-Key`. Matching is deterministic: SHA-256 over normalized workspace ID, service ID, event type, and error signature. Raw summaries are deliberately excluded because timestamps and request IDs make them unstable.

```bash
curl -X POST http://localhost:8300/api/v1/workspaces/WORKSPACE_ID/events \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-alert-123" \
  -d '{"service_id":"SERVICE_ID","external_id":"alert-123","source":"demo-monitor","event_type":"http_error_rate","severity":"sev2","summary":"Checkout API 5xx rate exceeded threshold","error_signature":"checkout.http.5xx","attributes":{"error_rate":0.18,"threshold":0.05,"region":"ap-south-1"},"occurred_at":"2026-10-30T10:00:00Z"}'
```

The first request returns `result: "created"`; a related event with a new key returns `"correlated"`; replaying the same key returns the original event and outcome without writing duplicates.

## Real-time endpoint

Connect to `ws://localhost:8300/api/v1/ws/workspaces/{workspace_id}/incidents`. Messages use a typed envelope containing `event_id`, `type`, `workspace_id`, `incident_id`, `occurred_at`, and `data`. The client sends `ping`, reconnects with capped exponential backoff, and keeps REST usable while Redis/WebSocket connectivity is degraded.

## Operational knowledge

The knowledge UI at `/knowledge` accepts direct plain text/Markdown or `.txt`, `.md`, and text-based `.pdf` files up to the configured bound. Executables, archives, encrypted PDFs, image-only PDFs, unsupported binaries, oversized files, and empty extraction results are rejected. The API stores the unchanged private original, while only redacted extracted chunks are embedded and indexed.

Uploads return HTTP 202 and RQ performs extraction, redaction, deterministic structure-aware chunking, batched embedding, transactional chunk insertion, and the atomic current-version switch. A failed or cancelled replacement never displaces the last ready version. Reindex creates a new version from the stored original; archive is soft and immediately excludes every version from retrieval.

Local development uses `KNOWLEDGE_STORAGE_BACKEND=local` and `KNOWLEDGE_STORAGE_ROOT=./.data/knowledge` on the host, or the private `knowledge_data` Compose volume at `/app/data/knowledge`. Storage keys are generated, never public, and an S3-compatible adapter boundary is available for deployment integration. Local originals are **not application-encrypted**; rely on host/volume encryption and access controls.

Embeddings default to deterministic `EMBEDDING_PROVIDER=fake`, `EMBEDDING_MODEL=hash-bow-v1`, and the schema-fixed 64 dimensions. Changing dimensions requires a migration and reindex. Production adapters use the same bounded batching/retry interface and must be implemented before selecting them. Reranking is optional and falls back to RRF only on retryable failures.

Search variants are `lexical` (PostgreSQL FTS), `vector` (pgvector cosine), `hybrid_rrf` (rank-only fusion), and `hybrid_rrf_rerank` (bounded fake/adapter rerank). Every result identifies the current ready chunk and includes a stable citation. Candidate queries contain the workspace predicate before ranking; archived, failed, and stale versions cannot enter a candidate set.

```bash
curl -X POST http://localhost:8300/api/v1/workspaces/WORKSPACE_ID/knowledge/search \
  -H "Content-Type: application/json" \
  -d '{"query":"checkout database connection exhaustion","document_types":["runbook"],"variant":"hybrid_rrf","candidate_count":20,"top_k":5}'
```

Run the deterministic 20-query PostgreSQL/pgvector evaluation after exporting `.env` values:

```bash
cd services/api
uv run python -m opspilot_api.knowledge.evaluation --dataset tests/fixtures/retrieval_evaluation.json --json-output retrieval-evaluation.json --markdown-output retrieval-evaluation.md
```

**Experimental query enhancement.** `hybrid_rrf_rewrite` and `hybrid_rrf_hyde` use the Module 2 model-provider router to rewrite the query or generate a hypothetical passage (HyDE). They are rejected (HTTP 422) unless `KNOWLEDGE_EXPERIMENTAL_QUERY_ENHANCEMENT=true`, make at most one bounded call (`KNOWLEDGE_ENHANCEMENT_TIMEOUT_SECONDS`, `_MAX_OUTPUT_TOKENS`, `_MAX_COST_USD`), fail loudly rather than silently falling back, and are marked `experimental` in diagnostics and `RetrievalLog.filters`. They are API/evaluation-only (not in the UI selector), never used by the investigation tool, and reported in a separate evaluation section with the fake provider.

The command compares all four baseline variants and fails on fixture-baseline regression, missing citations, or any cross-workspace leakage. It reports Recall@1/3/5, MRR, NDCG@5, no-answer behavior, P50/P95 latency, and leakage. The fake provider is a deterministic test/demo baseline, not a claim about production semantic quality.

## GitHub integration (Module 4, read-only)

### Modes
- `GITHUB_PROVIDER=fixture` (default, used by CI and the local demo): committed synthetic repositories in `services/api/src/opspilot_api/repositories/fixtures/`. No GitHub credentials, no network. With `GITHUB_FIXTURE_REBASE_TO_NOW=true` fixture timestamps are shifted so the planted regression is about an hour before now, which makes incidents created in the demo correlate with it.
- `GITHUB_PROVIDER=github`: a GitHub App using short-lived installation tokens.

Application and LangGraph code depend only on the `RepositoryProvider` interface; every method is a read operation.

### GitHub App setup
1. Create a GitHub App. **Repository permissions: Contents (read), Metadata (read), Pull requests (read), Deployments (read). Request no write permission** and no user OAuth.
2. Subscribe to events: Installation, Installation repositories, Push, Pull request, Deployment, Deployment status.
3. Webhook URL `https://<your-host>/api/v1/integrations/github/webhook`, content type `application/json`, and a long random webhook secret.
4. Generate a private key. Provide it through `GITHUB_APP_PRIVATE_KEY_FILE` (preferred, mounted outside the repository) or `GITHUB_APP_PRIVATE_KEY`; set `GITHUB_APP_ID` and `GITHUB_WEBHOOK_SECRET` (min. 16 characters). Optionally set `GITHUB_ALLOWED_INSTALLATION_IDS`. Secrets are server-side only; installation tokens are cached in process memory for their provider-declared lifetime, never stored in the database, never returned by the API, and never logged.
5. In the UI open `/integrations/github`, enter the installation ID, and synchronize.

### Webhooks
`POST /api/v1/integrations/github/webhook` reads the raw bytes (bounded by `GITHUB_WEBHOOK_MAX_BYTES`), verifies the `X-Hub-Signature-256` HMAC with a constant-time comparison **before** parsing JSON, requires `X-GitHub-Delivery` and `X-GitHub-Event`, de-duplicates by delivery ID, stores only selected normalized fields, and returns 202 after enqueueing an RQ job. Invalid signatures never enqueue; unknown events are acknowledged and recorded as `ignored`. Repository synchronization never runs inside the HTTP request.

### Indexing policy
Allowed: Python, TypeScript/JavaScript, JSON, YAML, TOML, SQL, shell, Dockerfiles, Terraform, Markdown, Go/Java/Rust, INI. Excluded: binaries, images/media, archives, vendor/dependency/build directories, generated and minified files (including files with generated markers), source maps, lockfiles, `.git`, `.env*`, private keys/certificates, large data files, files above `REPO_MAX_FILE_BYTES`, symlinks, and submodule contents. Limits (`REPO_MAX_FILES`, `REPO_MAX_TOTAL_BYTES`, `REPO_MAX_CHUNKS`, `REPO_INDEX_TIMEOUT_SECONDS`, parse-failure ratio) fail the snapshot loudly. Secrets in allowed files are redacted line by line **before** storage or embedding, so line numbers stay exact. Only a fully persisted snapshot is activated (`repositories.active_snapshot_id` is switched in the same transaction); a failed or cancelled run keeps the previous ready snapshot searchable. A push builds a new bounded snapshot at the new head, which is safer than patching a live one (unchanged chunk embeddings are reused through a content-keyed cache) at the cost of re-reading the repository.

### Service mapping
`POST /api/v1/workspaces/{ws}/services/{service}/repositories` maps a repository to a service with an environment, a branch pattern, and an optional monorepo `path_prefix`. A service can have several repositories; duplicates return 409; deleting a mapping never deletes repository data. The first integration tracks the default branch, so a mapping whose branch pattern does not match it contributes nothing.

### Code search
```bash
curl -X POST http://localhost:8300/api/v1/workspaces/WORKSPACE_ID/repositories/search \
  -H "Content-Type: application/json" \
  -d '{"query":"database connection pool exhausted","variant":"hybrid_rrf","top_k":5}'
```
Variants: `lexical`, `vector`, `hybrid_rrf`, `hybrid_rrf_rerank`. Filters: repository, service (applies the mapped path prefix), environment, branch, path prefix, language, symbol type. Every candidate query joins the repository's active snapshot and repeats the workspace predicate. Citations look like `[github.com/acme/checkout-api@c3c3c3c: src/checkout/db.py:9-24, ConnectionPool]` and are built only from stored repository, snapshot commit, path, and line data.

### Incident/change correlation
`GET /api/v1/workspaces/{ws}/incidents/{id}/suspect-changes` ranks recent commits and deployments of the service's mapped repositories with a documented, explainable score (no LLM): successful deploy shortly before the incident (0.30), incident-term overlap with changed paths/messages/lines (0.30), commit recency (0.15), monorepo prefix match (0.10), a revert after the incident (0.10), the commit being a revert (0.05), environment match (0.05). Reverts before the incident, failed deployments, and other-environment-only deployments are contradicting signals; changes outside a mapped prefix are excluded. Results carry matched and contradicting signals, time distance, changed files, and a confidence category. **Correlation does not prove causation**; reports must say so.

### Investigation integration
Seven read-only tools (`list_recent_changes`, `rank_suspect_changes`, `get_commit_summary`, `get_commit_diff`, `search_repository_code`, `get_code_excerpt`, `get_pull_request_context`) derive scope from the trusted investigation context, validate arguments, read only synchronized data, and produce `repository_commit`, `deployment`, `pull_request`, `code_chunk`, and `commit_diff` evidence. Repository text is untrusted evidence. Missing mappings or unavailable data produce an explicit observation, never fabricated evidence, and investigations without a mapping behave as before. `AI_MAX_TOOL_CALLS` now defaults to 14 and a plan may contain up to 12 tool requests.

### Repository evaluation
```bash
cd services/api
uv run python -m opspilot_api.repositories.evaluation --dataset tests/fixtures/repository_evaluation.json --json-output repository-evaluation.json --markdown-output repository-evaluation.md
```
23 code-search queries (18 ranked, plus no-answer and secret probes) and 13 correlation cases over fixture repositories with two similar workspaces. It fails on cross-workspace leakage, invalid citations (line, commit, or path mismatch), excluded or secret content becoming searchable, or metrics below the floors in `repositories/evaluation.py`. Measured on the fixture: lexical R@5 1.000 / MRR 0.944; hybrid RRF R@5 0.944 / MRR 0.903 (no-answer 1.0); vector-only R@5 0.722 with no-answer 0.0, because the fake 64-dimension hashed embedding gives unrelated queries similar cosine scores; correlation R@1 1.000 and R@3 1.000 on cases written by the fixture author. These are fixture baselines, not production quality claims.

### Module 4 limitations
Workspace IDs are still not authentication, so any workspace can claim any installation of the App unless `GITHUB_ALLOWED_INSTALLATION_IDS` is set. Only the default branch is tracked, only text source is indexed, GitHub API pagination is bounded (5 pages of repositories, 100 commits per sync, 30 deployments), deployments are matched by commit SHA, and correlation is heuristic. The GitHub adapter is covered by mocked-transport tests only; it has not been exercised against api.github.com from this repository.

## Observability event pipeline (Module 5)

Signed webhooks and read-only CloudWatch/fixture queries feed incident correlation through a Kafka-compatible broker. The system is **at-least-once with idempotent consumers; it does not claim exactly-once delivery** (see `docs/architecture.md` for why).

### Setup and local broker
Docker Compose adds `broker` (Redpanda, single node, no ZooKeeper, named volume `broker_data`), a one-shot `broker-init` that creates the topics, and two consumer services:

```bash
# Fernet key that encrypts webhook secrets at rest (the .env.example value is a throwaway):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
docker compose up -d --build        # broker, broker-init, api, worker, raw-consumer, normalized-consumer, web
docker compose exec broker rpk cluster health
docker compose exec broker rpk topic list
docker compose exec broker rpk group describe opspilot-normalized-consumer   # lag per partition
docker compose logs -f raw-consumer normalized-consumer
```
Containers reach the broker at `broker:9092`; the only published port is `127.0.0.1:${KAFKA_HOST_PORT:-19092}` for host-run tests and the benchmark. Metrics are exposed at `GET /api/v1/metrics` (API) and on ports 9108/9109 (consumers, container-internal).

Troubleshooting: if the API returns 503 `broker_unavailable`, check `docker compose ps broker` and `rpk cluster health`. Host tools must use `127.0.0.1:19092` (not `localhost`, which can resolve to IPv6). A stuck message shows up as growing consumer lag (`rpk group describe`) or an open dead letter in `/observability/dead-letters`. `docker compose down -v` also deletes the broker volume.

### Topics
| Topic | Contents |
| --- | --- |
| `opspilot.observability.raw.v1` | redacted, schema-versioned envelopes straight from ingress |
| `opspilot.observability.normalized.v1` | canonical normalized signals |
| `opspilot.observability.dead-letter.v1` | sanitized failure envelopes (with `original_topic`, partition, offset, failure code) |

The record key is `workspace_id:service_id`: ordering is guaranteed per workspace+service within a partition, not across services, and not across stages when retries occur. Headers carry routing metadata only (never credentials). Messages with an unknown `schema_version` are dead-lettered.

### Generic webhook sources
Create a source in `/observability/sources/new` (or `POST /api/v1/workspaces/{ws}/observability/sources`); the signing secret is shown **once** (store it; rotate to get a new one, the old one stops working immediately). Sign `"{timestamp}.{raw body}"` with HMAC-SHA256 and send:

```bash
SECRET=whsec_...; PUBLIC_ID=...; TS=$(date +%s)
BODY='{"type":"alert","event_id":"evt-1","title":"Checkout 5xx high","severity":"high","occurred_at":"2026-03-10T12:00:00Z"}'
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')
curl -X POST "http://localhost:8300/api/v1/ingest/webhooks/$PUBLIC_ID" \
  -H "Content-Type: application/json" -H "X-OpsPilot-Timestamp: $TS" -H "X-OpsPilot-Signature: $SIG" \
  --data-raw "$BODY"
```
Responses: `202` accepted (durably published), `200` duplicate (original `signal_id`), `401` invalid/missing/expired signature, `413` too large, `400` malformed JSON, `403` disabled source, `404` unknown source, `429` rate limited, `503` cannot durably accept. Payload types: `alert`/`availability`/`security`/`custom`, `log` (error level or above), `metric` (value + threshold), `deployment`, plus CloudWatch alarm and log shapes. Normalization is deterministic (no LLM): required fields are validated explicitly (never guessed), strings are bounded and redacted, unknown fields survive only inside bounded sanitized attributes, and long strings are truncated to 2000 characters. Secrets are encrypted at rest with `OBS_SECRET_ENCRYPTION_KEY`; production key management is deferred.

### CloudWatch and fixture sources
`aws_cloudwatch` sources are configured with `role_arn`, `external_id_ref` (the *name* of an environment variable holding the external ID), `allowed_log_groups`, `allowed_namespaces`, `alarm_names`, `max_window_minutes`, `max_results`, `timeout_seconds`, and `default_metric`. Credential-looking keys are rejected; no AWS key is stored or returned; temporary role credentials are used. Only `FilterLogEvents`, `GetMetricStatistics`, `DescribeAlarms`, and `DescribeAlarmHistory` are ever called, within the configured allow-lists and bounds. The `demo_fixture` provider returns deterministic logs, metrics, and alarms and can simulate `throttle`, `timeout`, `auth_failure`, and `error` (`configuration.fixture_mode`); CI and the demo use it exclusively. The CloudWatch adapter is verified with botocore stubs only; it has not been run against real AWS from this repository.

### Deduplication, retries, dead letters, backpressure
* **Deduplication:** provider event ID, then `Idempotency-Key`, then a normalized fingerprint inside `OBS_DEDUP_WINDOW_SECONDS`. Duplicates return `200`, never create a second incident or timeline entry, and are visible as `duplicate` in the signal explorer.
* **Retries:** transient failures (broker, database, throttling) retry with exponential backoff and jitter up to `OBS_RETRY_MAX_ATTEMPTS` and `OBS_RETRY_MAX_AGE_SECONDS`; invalid signature/schema/fields, oversized payloads, tenant mismatches, and configuration failures are not retried.
* **Dead letters:** exhausted or terminal messages are stored (redacted, bounded) and published to the dead-letter topic with a sanitized reason; `POST .../dead-letters/{id}/replay` republishes once, is audited, and is idempotent (`already_replayed`).
* **Backpressure:** per-source (`OBS_SOURCE_RATE_PER_SECOND`) and per-workspace (`OBS_WORKSPACE_RATE_PER_SECOND`) limits return `429` with `Retry-After`; an unreachable broker, unavailable rate-limit state, consumer lag above `OBS_LAG_THRESHOLD`, or a dead-letter burst returns `503`. `202` is only ever returned after durable acceptance, nothing is buffered in memory, and pressure clears automatically.
* **Evidence budget:** an investigation now truncates an over-chatty tool's evidence (and records `evidence_truncated`) instead of failing at `AI_MAX_EVIDENCE_ITEMS`, reserving room for the tools that still have to run. `AI_MAX_TOOL_CALLS` defaults to 20 and a plan may contain 16 tool requests.

### Pipeline verification, load test, and benchmark
```bash
cd services/api
uv run python -m opspilot_api.observability.smoke      # deterministic end-to-end smoke test (memory bus)
uv run pytest -m integration tests/test_observability_integration.py tests/test_observability_kafka.py
# Benchmark against the running Compose stack (development only; refuses production):
uv run python -m opspilot_api.observability.loadgen --events 2000 --rate 200 \
  --duplicate-percent 10 --invalid-percent 2 --services 3 --fingerprints 5 --seed 7
uv run python -m opspilot_api.observability.loadgen --events 3000 --rate 0   # burst mode
```
Results (JSON) are written to the git-ignored `benchmark-results/` directory. They report accepted and processed events per second, request and end-to-end P50/P95/P99 latency, duplicate suppression, error and dead-letter rates, consumer lag, and incidents created/correlated. Resilience scenarios are covered by tests: broker unavailable and recovery, backpressure (`test_backpressure_returns_honest_status_codes_and_recovers`), consumer crash before commit and restart, duplicate delivery, poison messages, unsupported schema version, dead-letter replay, provider throttling and authentication failure, and webhook replay (`test_observability_integration.py`, `test_observability_kafka.py`).

#### Measured benchmark (this repository, one Windows laptop running Docker Compose, 2026-10-30)
Single-node Redpanda, one raw consumer, one normalized consumer, 16–48 concurrent clients, seeded plan (10% duplicates, 2% invalid).

| Run | Sent | Accepted/s | Processed/s | Request P50 / P95 / P99 | End-to-end P50 / P95 / P99 | Duplicates suppressed | Dead-letter rate | Errors (non-2xx) | Max / final lag |
| --- | ---: | ---: | ---: | --- | --- | --- | ---: | ---: | --- |
| `--rate 200`, 3 services | 2000 | 34.9 | 25.7 | 350 / 616 / 2667 ms | 12.0 / 16.0 / 16.0 s | 213 of 213 | 2.4% | 0 | 580 / 0 |
| `--rate 0` burst, 48 clients, 1 service | 1500 | 41.8 | 23.8 | 912 / 2251 / 3347 ms | 20.3 / 22.0 / 22.1 s | 163 of 163 | 2.2% | 0 | 831 / 0 |

What this shows, and what it does not: the requested 200 events/s was **not** reached; the synchronous ingress path (signature check, receipt commit, acknowledged broker publish, second commit) sustained roughly 35–42 accepted events/s here, and the single normalized consumer processed roughly 24–26/s, so a backlog built up (peak lag 580–831 messages) and end-to-end latency of 12–22 s is queueing delay that drained to zero lag. No request failed, every injected duplicate was suppressed, every incident was created or correlated once (15 and 5 incidents), and the dead-letter rate equals the injected invalid rate. Rate limits (429) were never hit because throughput stayed below the 100/s per-source limit. These are single-machine numbers for a development stack, not a capacity claim; horizontally scaling consumers (more partitions and group members) and batching database work are the obvious next steps and were not attempted.


### Module 5 limitations
Workspace IDs are still not authentication; the metrics endpoint and webhook public IDs are unauthenticated by design (HMAC protects ingestion); webhook secrets are encrypted with a single environment key; the broker is single-node with replication factor 1 and no TLS/SASL (local development only); rate-limit and lag checks are best-effort and approximate; ordering is per workspace+service only; CloudWatch is verified with stubs, not real AWS; the local benchmark measures one laptop-class machine, not production capacity. Automated remediation, Slack, and production AWS deployment remain deferred.

## Current security boundary

Workspace IDs in URLs scope every tenant-owned query, but **they are not authentication**. REST and WebSocket endpoints are intentionally unauthenticated. Do not expose this build as a production-secure multi-tenant service. Evidence and knowledge chunks are bounded and common secret forms are redacted, but this is defense in depth rather than complete DLP. Uploaded originals remain unredacted in private storage and local storage is not encrypted by the application. Incident/log/document content remains untrusted. Hidden chain-of-thought and full prompts are neither stored nor returned; only structured outputs, evidence references, concise summaries, and safe errors are persisted.

## Tests and quality checks

```powershell
scripts\check-all.ps1                 # backend + frontend
scripts\check-all.ps1 -Integration    # also live-PostgreSQL/Redis tests (stack running, migrated)
scripts\check-backend.ps1
scripts\check-frontend.ps1
```

Portable:

```bash
cd services/api
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
uv run pytest -m integration          # needs the stack + .env values exported

cd apps/web
export NEXT_PUBLIC_API_BASE_URL=http://localhost:8300
corepack pnpm lint && corepack pnpm typecheck && corepack pnpm test && corepack pnpm build
```

Unit tests use test-only settings and need no running services or real credentials.

## Troubleshooting

- **Port already in use** (`bind: address already in use`): another process owns the port. Change `WEB_HOST_PORT`, `API_HOST_PORT`, `POSTGRES_HOST_PORT` or `REDIS_HOST_PORT` in `.env` (and keep `NEXT_PUBLIC_API_BASE_URL`, `CORS_ORIGINS` and the host-side `POSTGRES_PORT`/`REDIS_URL` consistent). Find the owner: `netstat -ano | findstr :8300`.
- **Unhealthy container:** `docker compose ps`, then `docker compose logs <service>`. `api` stays `starting` until PostgreSQL and Redis are healthy; the worker is healthy once registered in Redis.
- **Dashboard says "Backend unreachable":** the API is down, or the browser origin is not in `CORS_ORIGINS`, or `NEXT_PUBLIC_API_BASE_URL` was changed without rebuilding `web`.
- **"set X in .env" / `Field required` on startup:** a required variable is missing. Re-copy from `.env.example` (`scripts\bootstrap.ps1`).
- **Integration tests time out on Windows:** use `127.0.0.1`, not `localhost`, for host-side `POSTGRES_HOST`/`REDIS_URL` (Compose publishes on IPv4 loopback).
- **Changed `POSTGRES_PASSWORD` after first start:** the volume keeps the old one. Reset with `docker compose down -v` (deletes local data).
