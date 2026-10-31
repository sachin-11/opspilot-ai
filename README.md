# OpsPilot AI

OpsPilot AI is a multi-tenant incident-response platform. This repository contains **Module 7: OIDC authentication (AWS Cognito compatible), workspace memberships, centralized RBAC, scoped API keys, single-use WebSocket tickets, rate limiting, identity-backed approvals, and an append-only integrity-verified audit log**, on top of **Module 6: evidence-backed remediation proposals, constrained patch generation, deterministic policy validation, isolated sandbox verification, explicit human approval, and GitHub fix pull requests**, built on the Module 0–5 foundation (incidents, investigations, runbook RAG, read-only GitHub integration, observability pipeline).

## Module 3, 4, and 5 capabilities

Module 5 ingests production signals through signed webhooks and a Kafka-compatible broker, normalizes and de-duplicates them, feeds the existing incident correlation, and gives investigations bounded, read-only log/metric/alarm tools. Automated remediation is not implemented.

Module 4 adds a **strictly read-only** GitHub App integration: repository sync, a bounded source-code index with citations, commit/pull-request/deployment tracking, deterministic incident-to-change correlation, and read-only LangGraph code-evidence tools. **Module 4 cannot modify GitHub repositories** (no branches, commits, pull requests, comments, patches, or workflow runs); fix generation and human-approved fix PRs belong to a later module.

Included: the incident and human-reviewed investigation flows plus private text/Markdown/text-PDF ingestion, atomic document versions, deterministic redacted chunks, PostgreSQL full-text and pgvector search, hybrid RRF, optional reranking, citations, evaluation tooling, safe investigation RAG tools, real-time progress, and knowledge-management UI.

### Intentionally deferred to later modules

OCR, DOCX, web scraping, Slack, production authentication/authorization (Module 7), billing, Kubernetes, and any automated production remediation. **OpsPilot never merges, deploys, or changes production.** The only write it performs is creating a fix branch, one commit, and a pull request after explicit human approval (Module 6).

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

For the k6 read-path load test, how to run both tools after Module 7 (they sign in through the local dev provider), and the latest measured results and findings, see [docs/load-testing.md](docs/load-testing.md).
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
Webhook public IDs are unauthenticated by design (HMAC protects ingestion); the metrics endpoint now requires a signed-in user; webhook secrets are encrypted with a single environment key; the broker is single-node with replication factor 1 and no TLS/SASL (local development only); rate-limit and lag checks are best-effort and approximate; ordering is per workspace+service only; CloudWatch is verified with stubs, not real AWS; the local benchmark measures one laptop-class machine, not production capacity. Automated remediation, Slack, and production AWS deployment remain deferred.

## Remediation (Module 6)

Module 6 turns an approved investigation into a reviewable GitHub pull request, with deterministic controls between every model suggestion and every side effect. **OpsPilot does not auto-merge, auto-approve, deploy, push to a default branch, or mutate infrastructure.**

### Lifecycle

`drafting` → `policy_check` → `sandbox_queued` → `sandbox_running` → `awaiting_approval` → `approved` → `publishing` → `pull_request_created`, with the terminal/side states `policy_blocked`, `verification_failed`, `rejected`, `stale`, `failed`, and `cancelled`. Legal transitions are an explicit table (`remediation/state.py`); any other transition returns `409 Conflict`. A `revision_requested` review returns to `drafting`; an invalidated approval returns to `policy_check` for re-verification.

A remediation may start only from a **completed, human-approved investigation** with a root cause and supporting evidence, whose service maps to an **active, non-archived** repository that has a ready snapshot (the concrete base commit) that is still the default branch head. One active remediation per investigation; failed/stale/blocked ones are regenerated with `POST .../retry`. The model may **abstain** (`no_safe_patch`, `more_evidence_required`, `outside_allowed_scope`, `human_work_required`); abstention is a normal outcome (`failed` with an `abstained_*` code), preferred over an unsafe patch.

API (all under `/api/v1/workspaces/{workspace_id}`): `POST /investigations/{id}/remediations`, `GET /remediations`, `GET /remediations/{id}`, `POST /remediations/{id}/cancel|retry|review|publish`, `GET /remediations/{id}/sandbox-runs|verification|pull-request`. Generation and publication run asynchronously on RQ.

### Structured output and validation

The generator returns a typed object (problem statement, evidence IDs, repository, base commit, per-file explanations, unified diff, risk assessment, **registered command IDs**, rollback guidance, limitations). Output is rejected when it cites nonexistent evidence, targets another repository or base commit, has a malformed/binary diff, lists files that differ from the diff, or requests anything other than a registered command ID. At most **two** bounded repair attempts follow.

### Policy rules (deterministic, versioned `2026-10.1`)

No model decides policy; `evaluate_patch` is a pure function recorded in `remediation_policy_evaluations`.

- **Blocked paths:** `.github/workflows/` and other CI config, `.env*`, keys/certificates, Terraform/Kubernetes/Helm/infra and deployment paths, Dockerfiles/compose, database migrations and `.sql`, dependency lockfiles, generated/vendored directories, auth and billing code (unless explicitly enabled), `.git`, anything outside the mapped monorepo prefix, and unlisted file types.
- **Blocked content:** symlink creation, path traversal, submodule changes, binary patches, executable-bit/mode changes, obfuscated/dynamic code, credential material, network exfiltration and raw sockets, reverse shells, persistence mechanisms, CI bypasses, test deletion or weakening, broad exception swallowing, disabled security checks, non-fix target branches.
- **Limits:** files, added lines, deleted lines, patch bytes (configurable `REMEDIATION_MAX_*`), sandbox attempts, revisions.
- **Warnings** (allowed, risk raised): public API changes, configuration or dependency changes, large surface, missing targeted test, reduced coverage, instruction-like text in the patch.

### Sandbox security

Repository code (including tests) runs **only** in an ephemeral Docker container created by `build_docker_args` from a registered command, never from model text: non-root uid 10001, read-only root filesystem with a size-limited tmpfs, one dedicated writable workspace mount, `--network none`, `--cap-drop ALL`, `no-new-privileges`, CPU/memory (no swap)/PID limits, hard timeout, bounded captured output (marked truncated), post-run disk-limit check, no Docker socket, no host-home or workspace-root mount, no `--privileged`, and a fixed sanitized environment (`PATH`, `HOME`, `LANG`, `CI`, ... only; no application, AWS, GitHub, database, Redis, Kafka, or LLM secrets). The workspace and container are removed after success, failure, timeout, or cancellation (leftover workspaces are purged by retention). Build the image with `scripts/build-sandbox.ps1`.

The Compose `worker` has **no Docker access and does not serve the `remediation` queue**. Remediation jobs run on a separate, sandbox-capable worker: `scripts/remediation-worker.ps1` (host process, `WORKER_QUEUES=remediation`). If Docker or the image is unavailable, the run fails closed with `sandbox_unavailable`.

### Repository checkout

The checkout reads the exact approved commit through the Module 4 read-only provider (tree + blobs). It never runs `git clone`, so no credential is placed in a Git remote and no hook, filter, or submodule can execute. Symlinks and submodules are never materialized, unsafe paths and oversized trees are refused, and a tree hash is recorded and re-verified.

### Verification command registry

Registered IDs: `python.pytest`, `python.pytest_targeted`, `python.ruff_check`, `python.ruff_format`, `python.ruff_security`, `python.mypy`, `node.test`, `node.lint`, `node.typecheck`, `node.build`. Each is an argv vector (no shell) with required project markers, timeout, fixed environment, and maximum output; `node.lint`/targeted pytest accept only validated, bounded path arguments. The model selects IDs; free-form commands never resolve.

### Baseline comparison

Checks run first on the unpatched base commit, then the patch is applied and the **same checks** rerun. Pre-existing failures are never attributed to the patch; new failures block approval; a patch must not be reported verified when no test check ran (`not_run`); skipped checks or remaining pre-existing failures make the result `partial` (shown as such); `passed` means every selected check executed and passed. Deleting or weakening tests is blocked by policy.

### Approval binding

Approval binds to the workspace, repository, base commit, revision ID, patch hash, policy version, verification-result hash (recomputed from persisted check rows), and an expiry time, stored with a context hash. At publication every value is re-derived; any change (a new revision, edited rows, policy-version bump, expiry, advanced base) invalidates the approval, records an audit event, returns the remediation to `policy_check` (or `stale`), and publishes nothing. Approvals are append-only, idempotent on replay, single use (one publication row per approval), and fail closed on timeout or provider outage. Revisions, policy evaluations, approvals, and audit events are protected by database triggers that reject `UPDATE`/`DELETE`.

### GitHub write permissions

The write provider is a separate, minimal interface: verify permissions, create a branch, create **one commit**, open a PR, read PR status. There is no merge, force push, default-branch push, review approval, branch-protection, workflow dispatch, release, deployment, or settings capability, and branch names must start with `opspilot/fix-`. It is **disabled by default**; enable it by setting `REMEDIATION_GITHUB_WRITE_ENABLED=true` **and** listing `"<workspace_uuid>:<owner>/<repo>"` in `REMEDIATION_WRITE_ENABLED_REPOSITORIES`. The GitHub App needs `contents: write` and `pull_requests: write` (the token requests only those plus `metadata: read`). The base commit and default-branch head are rechecked before the branch is created; retries reuse the same branch, commit, and PR. If the target branch advanced, the remediation becomes `stale`; OpsPilot never rebases automatically.

### Local fixture mode

With `GITHUB_PROVIDER=fixture` and `REMEDIATION_WRITE_PROVIDER=fixture` (defaults), `AI_PRIMARY_PROVIDER=fake`, no network or paid API is used: a deterministic fake generator proposes a fix for the planted connection-pool regression (or abstains), and an in-memory writer records the branch/commit/PR. CI uses only these providers and synthetic repositories.

```powershell
scripts/build-sandbox.ps1                      # once: build the sandbox image
scripts/remediation-worker.ps1                 # sandbox-capable worker (separate terminal)
```

Evaluation (deterministic, fails on any unsafe publication, approval bypass, invalid citation, duplicate PR, or missing required block):

```powershell
cd services/api
uv run python -m opspilot_api.remediation.evaluation --dataset tests/fixtures/remediation_evaluation.json --sandbox docker
```

### Known security limitations (Module 6)

- Actors are `null` until Module 7 authentication; anyone who can reach the API can review. Approval is explicit but not yet identity-backed.
- Docker isolation is container-level, not a VM; a kernel or runtime escape is out of scope. The disk limit is enforced after each command (bind-mounted workspaces cannot be hard-capped by Docker) and the sandbox host should be dedicated.
- The sandbox worker needs Docker Engine access; do not give that access to the general worker or mount the Docker socket into containers.
- Content rules are pattern-based and conservative; they cannot prove arbitrary code is safe. They are a gate in front of sandbox execution and human review, not a replacement for either.
- Patches are applied by a strict built-in unified-diff applier (no fuzz); only text files are supported. A commit's membership in the repository is checked through the provider, which for GitHub also resolves fork-network commits.
- Only the fake generator ships; a real model adapter must keep the same structured-output contract and cannot weaken policy (the module treats all model output as untrusted).

## Authentication and authorization (Module 7)

Every endpoint is authenticated and authorized server-side. Public exceptions are only: `/health/live`, `/health/ready`, `/api/v1/meta`, the verified GitHub and generic ingestion webhooks (their workspace comes from server-side configuration, never the request), and the development-only local identity provider. A route that is not explicitly classified in `auth/route_policy.py` is denied (HTTP 403), and a test asserts that every registered route is classified.

### Sign-in flow

The browser uses OIDC **Authorization Code with PKCE (S256), state, and nonce**. The Next.js server (route handlers under `apps/web/src/app/api/auth`) starts the flow, validates `state`, exchanges the code, verifies the ID token (issuer, audience, signature, nonce), and stores the access token in a **sealed (AES-256-GCM JWE) HttpOnly cookie**. No token is ever in `localStorage`, HTML, URLs, or logs. The browser talks to the API only through a same-origin proxy (`/api/proxy`) that attaches the bearer token server-side. Cookie-authenticated mutations require a per-session **CSRF token** header plus a same-origin `Origin`. Return URLs are restricted to same-origin application paths. `proxy.ts` protects every page server-side; there are explicit login, logout, session-expired, access-denied, and auth-error pages. `next.config.ts` sets a practical Content Security Policy and security headers; cookies are `Secure` whenever `APP_BASE_URL` is https, `SameSite=Lax`.

### AWS Cognito / OIDC configuration

Backend (`AUTH_PROVIDER=oidc`; startup fails if anything is missing):

```
AUTH_PROVIDER=oidc
AUTH_OIDC_ISSUER=https://cognito-idp.<region>.amazonaws.com/<user-pool-id>
AUTH_OIDC_AUDIENCE=<app client id>      # matched against client_id (Cognito access tokens) or aud
AUTH_OIDC_TOKEN_USE=access              # checked against the token_use claim
AUTH_AUDIT_KEY=<32+ random characters>  # audit-log HMAC key (required outside development)
```

The JWKS URL is derived from the configured issuer only (`<issuer>/.well-known/jwks.json`); issuer and JWKS never come from a token or request. Tokens are verified with an explicit asymmetric algorithm allowlist (`AUTH_OIDC_ALGORITHMS`, default `RS256`; `none` and HMAC are refused), required `exp`/`sub`/`iss`, optional `nbf`, and clock-skew leeway. The JWKS is cached (`AUTH_JWKS_CACHE_SECONDS`) and refreshed at most once per unknown `kid` (rate-bounded, so rotation works and an attacker cannot hammer the IdP). Expired, invalid, and configuration errors are distinguished without leaking token data. The user is keyed by the immutable `sub`; email changes never create a new identity and a disabled user loses access on the next request.

Web (server-side environment only):

```
AUTH_MODE=oidc
APP_BASE_URL=https://app.example.com            # must be https outside localhost
SESSION_SECRET=<32+ random characters>
OIDC_CLIENT_ID=<app client id>
OIDC_AUTHORIZATION_URL=https://<domain>.auth.<region>.amazoncognito.com/oauth2/authorize
OIDC_TOKEN_URL=https://<domain>.auth.<region>.amazoncognito.com/oauth2/token
OIDC_ISSUER=<same issuer as the API>  OIDC_JWKS_URL=<issuer>/.well-known/jwks.json
```

Register `https://app.example.com/api/auth/callback` as the callback URL. Invitation acceptance compares the invitation email with a **verified** email claim, so configure the pool so that the verified `email` appears in the token your API validates (or set `AUTH_OIDC_TOKEN_USE=id`).

### Local OIDC with Keycloak (optional)

To exercise the real sign-in flow (email and password on the identity provider's own page, no "Continue as ..." buttons) without any cloud account, start the optional Keycloak container. It uses compose profile `idp`, so a normal `docker compose up` never starts it.

```
docker compose --profile idp up -d keycloak
```

Keycloak is stateless here: it imports `infra/keycloak/opspilot-realm.json` on every start (admin-console changes are not persisted). The users have **fixed IDs** in that file on purpose: the application identifies a person by the provider's immutable `sub`, so without them every restart would mint a new identity for the same email, and that person would lose access to their workspaces: realm `opspilot`, public client `opspilot-web` (Authorization Code + PKCE S256, callback `http://localhost:3300/api/auth/callback`), an audience mapper so access tokens carry `aud=opspilot-web`, and two throwaway users (`owner@example.com` / `owner-local-password`, `responder@example.com` / `responder-local-password`). The admin console is at `http://localhost:8180` (user `admin`, password `KEYCLOAK_ADMIN_PASSWORD`). Then set, in `.env`, and recreate `api` and `web`:

```
AUTH_PROVIDER=oidc
AUTH_OIDC_ISSUER=http://localhost:8180/realms/opspilot
AUTH_OIDC_AUDIENCE=opspilot-web
AUTH_OIDC_JWKS_URL=http://keycloak:8080/realms/opspilot/protocol/openid-connect/certs

AUTH_MODE=oidc
OIDC_CLIENT_ID=opspilot-web
OIDC_AUTHORIZATION_URL=http://localhost:8180/realms/opspilot/protocol/openid-connect/auth
OIDC_TOKEN_URL=http://keycloak:8080/realms/opspilot/protocol/openid-connect/token
OIDC_ISSUER=http://localhost:8180/realms/opspilot
OIDC_JWKS_URL=http://keycloak:8080/realms/opspilot/protocol/openid-connect/certs
OIDC_END_SESSION_URL=http://localhost:8180/realms/opspilot/protocol/openid-connect/logout
```

The browser-facing URLs use `localhost`; the server-to-server URLs (token, JWKS) use the Compose service name `keycloak`. A new user has no workspace until they create one (they become its Owner) or accept an invitation sent to their verified email. `AUTH_OIDC_JWKS_URL` is only needed for providers that do not serve `<issuer>/.well-known/jwks.json` (Keycloak, Auth0, Okta); Cognito does not need it. Outside development both the issuer and the JWKS URL must be https. Keycloak here is a development convenience: production should use a managed provider (Cognito, Auth0, Okta, Entra ID) or a hardened, persistent Keycloak deployment.

### Signing out of the identity provider too

Clearing the app's own session cookie is not enough: the identity provider keeps its own login session and would sign the user straight back in without asking for credentials. Set `OIDC_END_SESSION_URL` (the browser-facing logout endpoint; Keycloak: `<issuer>/protocol/openid-connect/logout`) and **Sign out** also sends the browser there. The redirect carries only `client_id` and the landing page (`post_logout_redirect_uri`, or `logout_uri` for Cognito via `OIDC_END_SESSION_REDIRECT_PARAM`); no token is placed in the URL, so Keycloak shows its own "Do you want to log out?" confirmation before returning to the login page. The landing page must be a registered logout URL (the development realm allows `http://localhost:3300/*`). Without the setting, sign-out ends only the app session. The `.env.example` and the Keycloak steps above already include it; the Playwright suite asserts that the next sign-in asks for credentials.

### Local-auth mode

`AUTH_PROVIDER=local` (the default for development/test) uses deterministic signed tokens and a tiny dev-only OIDC-shaped IdP (`/api/v1/auth/local/*`: authorization-code + PKCE) with five users (`owner`, `admin`, `responder`, `analyst`, `viewer`). **Settings validation, provider construction, and token issuance all refuse it outside development/test**, so the application cannot start with local auth in production or staging. CI uses only this provider.

Migrate existing anonymous demo data (idempotent; development only):

```powershell
docker compose exec api python -m opspilot_api.auth.bootstrap_dev     # or run the seed, which calls it
```

It creates the local users, makes `owner` the owner of every existing workspace, and gives each role a membership in `demo-operations`. Open http://localhost:3300 and choose a user on the sign-in page.

### Roles and permissions

| Permission | owner | admin | responder | analyst | viewer |
|---|:-:|:-:|:-:|:-:|:-:|
| `workspace.read`, `members.read`, `services.read`, `incidents.read`, `investigations.read`, `knowledge.read`, `integrations.read`, `observability.read`, `repositories.read`, `remediations.read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `investigations.run` | ✓ | ✓ | ✓ | ✓ | |
| `incidents.manage`, `services.manage`, `investigations.review`, `remediations.create`, `remediations.review` | ✓ | ✓ | ✓ | | |
| `workspace.manage`, `members.manage`, `knowledge.manage`, `integrations.manage`, `observability.manage`, `repositories.manage`, `remediations.publish`, `api_keys.manage`, `audit.read` | ✓ | ✓ | | | |

Owners additionally manage owner memberships (grant, change, remove); admins cannot touch owners or grant the owner role. Roles are per workspace. A workspace always keeps at least one active owner (the final owner cannot be removed, demoted, or suspended). Routes never check role names: they map to a permission in one central table, and sensitive domain services (membership, API keys, investigation review, remediation create/review/publish) **recheck the actor's permission themselves**. Unauthenticated requests get 401; a member lacking the permission gets 403; a non-member (or another tenant's resource) gets 404, indistinguishable from nonexistence. Revoked or suspended memberships stop authorizing on the next request.

### Invitation lifecycle

An owner/admin creates an invitation (`email`, `role`). The token (`secrets.token_urlsafe(32)`) is **returned once**; only its SHA-256 hash is stored, and the audit log never records the email or token. Invitations expire (`AUTH_INVITATION_TTL_HOURS`, default 72), are single use, can be revoked, and bind to the authenticated user who accepts with a **verified, normalized matching email**. Every failure returns the same error, so callers learn nothing about which emails or tokens exist. An invitation can never demote an existing owner.

### API keys

`ospk_<prefix>_<secret>` (256-bit secret) is shown once at creation or rotation; only a SHA-256 hash and the safe prefix are stored and compared in constant time. A key belongs to one workspace (resolved from the key record; a different workspace in the URL yields 404), may expire (capped by `AUTH_API_KEY_MAX_TTL_DAYS`), can be rotated or revoked, and `last_used_at` is updated at most every `AUTH_API_KEY_TOUCH_SECONDS`. Keys travel only in the `X-API-Key` header (never the query string) and cannot be combined with a bearer token. Scopes: `events:write`, `incidents:read`, `knowledge:read`, `investigations:read`. Each route lists the single scope that admits a key; **everything else, including member and key management, audit, remediation creation, approval, and publication, is human-only**.

### WebSocket tickets

`POST /api/v1/auth/ws-ticket {workspace_id, channel}` (user session + membership) returns a random ticket stored in Redis as a **hash** with a short TTL (`AUTH_WS_TICKET_TTL_SECONDS`, default 30), bound to user, workspace, membership, and channel. The socket (`/api/v1/ws/workspaces/{id}/incidents?ticket=`) consumes it atomically (`GETDEL`), so it is single use; reused, expired, wrong-workspace, wrong-channel, and forged tickets close with 4401. The server re-checks membership before subscribing and every few seconds afterwards, closing with 4403 on revocation (including a disabled user), closes every socket after `AUTH_WS_MAX_LIFETIME_SECONDS` (default 3600) with 4401 so the client must reconnect with a fresh ticket, which requires a still-valid web session, forwards only its own workspace's events, and enforces per-user connection and message-rate limits. Every reconnect obtains a new ticket. Tickets are never logged, and bearer tokens and API keys are not accepted on the socket.

### Separation of duties

- Approval and publication are separate permissions (`remediations.review` vs `remediations.publish`); responders may review but not publish.
- The approver is the authenticated user, recorded server-side; client-supplied actor IDs are ignored. API keys and system actors can never review or publish.
- A **high-risk** remediation cannot be approved by the user who requested it or who last requested a revision.
- At publication (API and worker, "execution time") the system rechecks that the approver is still an active member holding `remediations.review` (and, for high risk, still not the requester/reviser) and that the publisher still holds `remediations.publish`. If the approver lost access, the approval is invalidated and the remediation returns to policy re-verification; if the publisher lost access, publication stops. Stale, expired, rejected, or changed-artifact approvals stay invalid (Module 6 binding).

### Rate limiting

Redis token buckets (atomic Lua), keyed by identity (user, API key, source public ID, or IP hash), with bounded key lifetimes. Rules cover authentication failures, API-key traffic, webhooks, investigation starts and other LLM operations, knowledge and repository search, remediation creation, review/publish attempts, WebSocket tickets and connections, and invitation creation/acceptance. Exceeding a limit returns `429` with `Retry-After`; internal keys are never exposed. If Redis is unreachable, **high-risk mutation rules fail closed** (429) while read-like rules fail open (availability over throttling). Counters `opspilot_rate_limit_allowed_total` / `_denied_total` are exported. `AUTH_RATE_LIMIT_MULTIPLIER` scales limits (the test suite relaxes it).

### Client IP behind a proxy

Rate limits and audit-log IP hashes use the caller's address. `X-Forwarded-For` is attacker-controlled, so it is **ignored by default** and uvicorn's own proxy-header handling is disabled (`--no-proxy-headers`). Set `TRUSTED_PROXY_CIDRS` (comma-separated CIDRs) to the load balancer and the web container's network to believe it: when the direct peer is inside that list, the chain is read right to left and the first address that is not a trusted proxy is the client, so entries a caller prepends cannot win. Malformed chains fall back to the direct peer, networks that match everything (`/0`) are rejected at startup, and the header is length-bounded. Without a load balancer (local development) every user appears to come from the web container, which is the safe fallback.

### Audit log and integrity verification

`GET /api/v1/workspaces/{id}/audit-events` (`audit.read`; filters `action`, `result`, `actor_user_id`, `since`, `until`; cursor pagination) lists append-only events: workspace creation, membership/invitation/API-key changes, integration configuration, dead-letter replay, investigation start/review, remediation creation/review/publication, permission denials, and rate-limit denials. Each event stores a hashed source IP, a bounded and scrubbed `metadata` (no tokens, keys, secrets, or emails), and an **HMAC-SHA256 integrity hash** keyed by `AUTH_AUDIT_KEY`. Database triggers reject `UPDATE` and `DELETE`; there is no update or delete endpoint. Membership and API-key audit rows are written in the same transaction as the change; sensitive routes (investigation/remediation/dead-letter/integration actions) write a pre-execution audit event and **refuse the action (503) if it cannot be recorded**.

```powershell
docker compose exec api python -m opspilot_api.auth.audit_verify [--workspace <uuid>]   # exit 1 on any mismatch
```

### Known limitations (Module 7)

- Read requests and ordinary mutations are not individually audited (only security-relevant and sensitive actions are), and pre-execution audit events record authorization, not the action's final outcome.
- The audit HMAC key is a single server secret: someone holding both the database and the key can forge events; use external log shipping or an HSM/KMS-backed key for stronger guarantees. Rotation of `AUTH_AUDIT_KEY` invalidates verification of older events.
- No MFA, SCIM, enterprise SSO administration, or per-user session revocation list: a signed-in session lives until its access token expires (short-lived) although membership and user status are rechecked on every request.
- `GET /api/v1/metrics` exposes process counters (no payloads) to any signed-in user; scrape it through a trusted network path.
- Redis-backed rate limits are approximate under Redis failover; the local IdP is for development only.
- Invitation email is not delivered by OpsPilot; the token is handed to the inviter once.

## Error tracking (optional, Sentry)

Set `SENTRY_DSN` (and optionally `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`, default 0) to report unhandled exceptions from the API and the worker; leave it empty to keep error tracking off. A malformed DSN fails at startup without echoing its value. Reports are scrubbed twice: the SDK is configured not to collect user identity, cookies, request bodies, query strings, stack-frame variables, database query data, queue payloads, or GenAI inputs/outputs, and only an allowlist of request headers (`user-agent`, `content-type`, `content-length`, `x-request-id`, `accept`) is kept; a `before_send` hook then removes the same fields again (including the query string, which carries WebSocket tickets) and strips the user and client address. Tests send a real request error through a capturing transport and assert that a body, cookie, bearer token, ticket and local variable never appear in the event. Delivery to Sentry itself is configured by you and is not exercised by the test suite.

## HTTP metrics

Every HTTP request is measured by an outermost middleware and exported with the other counters on the authenticated `/api/v1/metrics` endpoint: `opspilot_http_requests_total{method,route,status}`, `opspilot_http_request_duration_seconds{method,route}` (histogram) and `opspilot_http_requests_in_progress`. The `route` label is the matched route template (`/api/v1/workspaces/{workspace_id}/incidents`), never the raw path, and unmatched paths share the single value `unmatched`, so identifiers cannot create unbounded series; nothing from the request body, headers, or identity is recorded. Starting points for dashboards and alerts:

```
# 5xx ratio over 5 minutes
sum(rate(opspilot_http_requests_total{status=~"5.."}[5m])) / sum(rate(opspilot_http_requests_total[5m]))
# p95 latency per route
histogram_quantile(0.95, sum by (le, route) (rate(opspilot_http_request_duration_seconds_bucket[5m])))
```

## Demo data

An empty console is hard to evaluate, so `python -m opspilot_api.maintenance.demo_data` fills an **existing** workspace (the one you are signed in to) with realistic scenarios. It goes through the same services as the API, so the data is consistent, and it is idempotent (scenarios are matched by title and skipped when present). It refuses to run in production and always needs an explicit workspace.

```powershell
docker compose exec api python -m opspilot_api.maintenance.demo_data --list
docker compose exec api python -m opspilot_api.maintenance.demo_data --workspace <slug-or-uuid>
```

| Scenario | What it shows |
| --- | --- |
| Checkout 5xx spike after a release (SEV1, mitigated) | Six correlated alerts, a redelivered alert recognised as a duplicate, notes, and an AI investigation left **awaiting review** |
| Payments database pool exhausted (SEV2, resolved) | Full lifecycle; the investigation was **approved** |
| Notification queue lag (SEV2, investigating) | A 12-alert storm collapsed into one incident; the investigation was **rejected** with a reviewer comment |
| Search latency (SEV3, open) | A single alert, freshly opened |
| Staging login errors (SEV3, resolved) | A manually declared incident with a timeline |
| Nightly inventory sync (SEV4, reopened) | Resolved, then reopened |
| Pipeline signals | Alert, log, metric and a duplicate delivery through the signed webhook and Kafka, plus a poison payload that lands in the **dead-letter queue** |

Investigations are produced by the real worker with the configured provider (the deterministic `fake` one by default), so the worker must be running; signals need the API, Kafka and the consumers. Use `--no-investigations` or `--no-signals` to skip either part, and `--wait` to change how long it waits for each investigation. Timelines are spread over the scenarios' ages so the history looks believable. To remove the data, delete the workspace or seed into a fresh one.

## Dependency-failure drill

`python -m opspilot_api.maintenance.chaos` stops PostgreSQL and Redis in a throwaway Compose project and checks the documented degradation (readiness 503 while liveness stays 200, a database outage answered as a retryable 503, reads continuing and high-risk mutations refused when Redis is down) and automatic recovery. See [docs/chaos-testing.md](docs/chaos-testing.md).

## Data retention

`python -m opspilot_api.maintenance.retention` purges old observability pipeline data. It is **off by default**: set `RETENTION_SIGNAL_RECEIPT_DAYS` and/or `RETENTION_DEAD_LETTER_DAYS` (0 keeps forever; otherwise at least 7 days, so a provider redelivery is still recognised as a duplicate). It is a dry run unless you pass `--apply`:

```powershell
docker compose exec api python -m opspilot_api.maintenance.retention           # report only
docker compose exec api python -m opspilot_api.maintenance.retention --apply   # delete in batches
```

It removes only finished, reproducible data: signal receipts that were processed or deduplicated, and dead letters that were replayed or resolved (with their replay records). Receipts that failed or are dead-lettered, unresolved dead letters, incidents, investigations, and the audit log are never touched; the audit log is append-only (database triggers reject `DELETE`), so any purge of it is a deliberate database-administrator procedure, not this job. Deletion runs in batches of `RETENTION_BATCH_SIZE` so transactions stay short. Run it from a scheduler (cron or Task Scheduler) once you have chosen the periods, and take a backup first the first time you apply it.

## Backup and restore

`scripts/backup.ps1` writes a checksummed `pg_dump` archive and verifies it can be listed; `scripts/restore.ps1 -Verify` runs a restore drill in a temporary database and drops it, and `-ReplaceLive -ConfirmDatabaseName <db>` performs a guarded recovery. The restore script refuses a corrupt archive, refuses without the exact database name, and refuses while sessions are still connected. See [docs/runbook-backup-restore.md](docs/runbook-backup-restore.md) for what is and is not covered (uploaded knowledge files need their own archive; a restored database also needs `OBS_SECRET_ENCRYPTION_KEY` and `AUTH_AUDIT_KEY`), targets, scheduling, and the disaster-recovery steps.

## Database connection pool

The API holds a bounded PostgreSQL pool per process: `DB_POOL_SIZE` (default 10) plus `DB_MAX_OVERFLOW` (default 10) connections, a `DB_POOL_TIMEOUT_SECONDS` (default 10) wait for a free connection before the request fails instead of queueing, `DB_POOL_RECYCLE_SECONDS` (default 1800) so long-lived connections are replaced before a proxy drops them, and a server-side `DB_STATEMENT_TIMEOUT_SECONDS` (default 60) that cancels a runaway statement. Size the pool against PostgreSQL `max_connections` across **every** process that connects (each API replica, the worker, the pipeline consumers, and migrations): `replicas x (DB_POOL_SIZE + DB_MAX_OVERFLOW)` plus the other processes must stay below it. The synchronous engines used by the investigation workflow and CLI tools are separate and are not governed by these settings.

## Current security boundary

Module 6 adds a controlled, approval-gated GitHub write path (branch, commit, pull request only); everything else below still holds.

Every API route requires an authenticated identity and a workspace membership with the route's permission (see Module 7). Workspace IDs in URLs are an input to authorization, not authentication. Evidence and knowledge chunks are bounded and common secret forms are redacted, but this is defense in depth rather than complete DLP. Uploaded originals remain unredacted in private storage and local storage is not encrypted by the application. Incident/log/document content remains untrusted. Hidden chain-of-thought and full prompts are neither stored nor returned; only structured outputs, evidence references, concise summaries, and safe errors are persisted.

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

Unit tests use test-only settings and need no running services or real credentials. Integration tests additionally need the sandbox image (`scripts/build-sandbox.ps1`) and Docker, because Module 6 verifies patches in real hardened containers; they fail loudly instead of skipping when the image is missing.

## Troubleshooting

- **Port already in use** (`bind: address already in use`): another process owns the port. Change `WEB_HOST_PORT`, `API_HOST_PORT`, `POSTGRES_HOST_PORT` or `REDIS_HOST_PORT` in `.env` (and keep `NEXT_PUBLIC_API_BASE_URL`, `CORS_ORIGINS` and the host-side `POSTGRES_PORT`/`REDIS_URL` consistent). Find the owner: `netstat -ano | findstr :8300`.
- **Unhealthy container:** `docker compose ps`, then `docker compose logs <service>`. `api` stays `starting` until PostgreSQL and Redis are healthy; the worker is healthy once registered in Redis.
- **Dashboard says "Backend unreachable":** the API is down, or the browser origin is not in `CORS_ORIGINS`, or `NEXT_PUBLIC_API_BASE_URL` was changed without rebuilding `web`.
- **"set X in .env" / `Field required` on startup:** a required variable is missing. Re-copy from `.env.example` (`scripts\bootstrap.ps1`).
- **Integration tests time out on Windows:** use `127.0.0.1`, not `localhost`, for host-side `POSTGRES_HOST`/`REDIS_URL` (Compose publishes on IPv4 loopback).
- **Changed `POSTGRES_PASSWORD` after first start:** the volume keeps the old one. Reset with `docker compose down -v` (deletes local data).

## LLMOps (Module 8)

Versioned prompts, a golden evaluation dataset, deterministic graders, a calibrated-judge
abstraction, experiments, usage/cost accounting, atomic budgets, and provider circuit breakers.
Everything required runs locally and in CI with **no paid, hosted, or credentialed service**.
Code lives in `services/api/src/opspilot_api/llmops/`; the UI is under `apps/web/src/app/llmops`.

### Prompt lifecycle

`draft -> candidate -> published -> retired`. Prompts are **workspace-scoped**, so one tenant can
never change another tenant's prompts. The six built-in Module 2 prompts are seeded as published
`1.0.0` on first use.

- `POST .../prompts/{id}/versions` creates a new version (never an in-place edit). Credentials in a
  template and untrusted fields interpolated into instructions are rejected.
- `POST .../publish` needs a completed evaluation run **that evaluated this exact version** and a
  passing gate report. Failed gates block publication. Only an **owner** may override *soft* gates
  (with a written reason, audited); safety invariants can never be overridden.
- `POST .../rollback` moves the single default pointer to an older immutable version; history is
  never rewritten. A database trigger rejects content changes to non-draft versions and a partial
  unique index guarantees one default.

### Dataset lifecycle

`opspilot-golden` v1 has **104 cases in eight suites** (13 each): triage, tool selection,
grounding, report quality, retrieval/citations, change correlation, remediation safety, and
isolation/injection/abstention. Cases consolidate and extend the Module 2-6 fixtures. Published
dataset versions are immutable (database triggers); a change imports a **new version**. Splits are
`development` / `validation` / `test`: development runs only see development, tuning runs see
development + validation, and only `gate` runs see the held-out test split. Duplicate and
near-duplicate cases are detected (Jaccard over each case's distinguishing content). Exports are
redacted. Production-derived cases need an explicit redaction attestation and an authorizing user,
and are never promoted into the test split.

### Evaluation commands

```powershell
cd services/api
uv run python -m opspilot_api.llmops.cli ci --baseline tests/fixtures/llmops_baseline.json   # required CI tier, 3 repeats
uv run python -m opspilot_api.llmops.cli compare --control baseline --candidate candidate      # control vs candidate
uv run python -m opspilot_api.llmops.cli baseline --write tests/fixtures/llmops_baseline.json  # regenerate the golden baseline
uv run python -m opspilot_api.llmops.cli export --out dataset.json                              # redacted export
```

Reports (`evaluation-reports/*.json` and `*.md`) are produced by every run. Runs created through
the API (`POST /api/v1/workspaces/{id}/llmops/eval-runs`) execute asynchronously on the RQ worker.

### Deterministic and LLM graders

Deterministic graders run **first** and are pure functions: schema validity, required fields, field
accuracy, tool selection and arguments, evidence-id validity, citation precision/recall, forbidden
claims, required abstention, tenant leak, secret leak, prompt-injection compliance, status
transitions, remediation policy, error classification, Recall@K, MRR, NDCG, and change-correlation
Recall@K. Tenant, secret, injection, and remediation failures are **safety** failures: they fail the
case regardless of any judge. The judge (`judge.py`) is a provider-neutral abstraction with a
versioned rubric (`judge-rubric-v1`, poor/acceptable/excellent anchors), strict structured output
(extra fields such as `reasoning` are rejected, so hidden reasoning cannot be stored), seeded
randomized pairwise order, variant names kept out of prompts, repeated judgments with spread, and a
verbosity-bias check. CI uses only the deterministic `FakeJudge`.

The required CI tier runs a **rule-based reference agent** (`reference_agent.py`) that reads only
`input_data`, never expected values, so the golden set tests real policies. It has three variants:
`baseline` (shipped behavior, 103/104 pass: one honest known weakness on a paraphrase query),
`candidate` (stemming + BM25, 104/104), and `regressing` (follows injections, leaks tenants/secrets,
invents citations, auto-approves), which the gates must block. **With the fake tier, prompt text does
not change behavior**: it proves the pipeline and gates, and the real-model tier evaluates wording.

### Human calibration

Reviewers label results from the queue on the rubric (1-5 per criterion, pass/fail, notes). Labels
are upserted per reviewer, a reviewer cannot label two repeats of the same case, label changes are
audited with the previous value, and Cohen's kappa measures inter-rater and judge-human agreement
(`human.py`). A judge is **calibrated** only with at least 30 labelled cases, agreement of at least
0.8, and kappa of at least 0.6. An uncalibrated judge never changes a verdict, and a run that uses a
judge without calibration fails the `judge_agreement` gate.

### CI quality gates

Hard (not configurable, not overridable): zero tenant leaks, secret leaks, injection violations,
prohibited remediation actions, invalid citations, and flaky cases. Soft (versioned `GateConfig`):
schema validity, tool-selection accuracy, citation validity, required abstention, per-metric and
per-case regression versus the baseline, P95 latency, average cost, and judge agreement. Latency and
cost gates are **skipped, and reported as skipped**, when a run's metrics are synthetic. CI also
runs a negative control that must fail: the regressing variant.

### Experiments and statistics

Assignment is a stable hash of `(experiment, salt, unit)` persisted once; refreshes, retries, and
later allocation changes never reassign. The unit (workspace/user/investigation) is configurable.
Remediation publication, approval, authorization, and policy decisions are excluded, as is any
high/critical-risk call. An experiment needs a primary metric, at least one guardrail, a minimum
sample size (at least 30 per arm), and an allocation of at most 50%. Analysis uses a two-sided
two-proportion z-test with Wald confidence intervals (no p-value when an arm has fewer than 5
successes or failures), Bonferroni-adjusted guardrails, a fixed-horizon assumption with repeated
looks counted and flagged, an immediate safety stop, and **no automatic promotion**. Arm outcomes
are computed from shadow/production-sample evaluation runs, not from live traffic.

### Usage and cost accounting

`usage_events` is append-only (triggers reject UPDATE/DELETE) and idempotent on the provider request
id or internal usage id. Cost is `NULL` ("unavailable") whenever tokens or a rate are missing; it is
never guessed. Pricing rates are immutable rows; a price change is a new version, and each event
keeps the version it was costed with. Aggregation never rewrites events.

### Budget reservations

Reserve (atomic, all scopes or none), call, reconcile, release the unused part, expire stale
reservations. In PostgreSQL each scope is charged by one conditional
`UPDATE ... WHERE spent + reserved + :amount <= hard_limit`, so concurrent calls cannot overspend
(verified with 24 concurrent reservations: exactly 3 of 0.30 fit under 1.00). Soft thresholds emit a
warning event; a hard cap blocks new calls with a non-retryable error and **never switches to another
model**; completed results stay readable. Unknown actual cost keeps the reserved ceiling as spend and
is flagged unmetered. Raising a limit or disabling a budget needs a reason and is audited.

### Provider circuit breakers

`closed -> open -> half_open -> closed`. A breaker opens on consecutive failures, error-rate or
timeout-rate windows, mean latency, or provider rate limiting. Configuration/authentication errors
are surfaced and never counted as health failures. Half-open permits a bounded number of probes.
State is shared through Redis (compare-and-set) and time is injected, so tests are deterministic.
The provider-health endpoint reports `unknown` (never "healthy") if the shared store cannot be read.

### Optional LangSmith tracing

Off by default. With `LANGSMITH_TRACING=true` and a key, a best-effort exporter is added; if it is
unreachable the workflow is unaffected (failures are counted and logged by class only). Every trace
is sanitized **before** any adapter sees it: no keys, tokens, authorization headers, raw documents,
full source, or reasoning fields. CI needs no LangSmith credentials.

### Manual real-model evaluation (optional)

Not run by CI. Preconditions: `LLMOPS_REAL_EVAL_ENABLED=true`, `AI_PRIMARY_PROVIDER=anthropic`,
`AI_PRIMARY_MODEL`, and `AI_PRIMARY_API_KEY` in your own `.env` (never committed).

```powershell
uv run python -m opspilot_api.llmops.cli real-eval --max-cost-usd 0.50 --limit 20 `
  --rate-version anthropic-2026-10-06-le100k --input-per-million 0.10 --output-per-million 0.50
```

It reserves the worst-case cost before every call and stops at the cap, sends only redacted,
model-visible fields (never expected values, canaries, or secrets), refuses cases it cannot redact,
uses only development/validation splits, records the model and pricing-rate version, and writes a
report. It never publishes a prompt. The GitHub workflow `llmops-real-eval.yml` is
`workflow_dispatch`-only, needs the `llmops-real-eval` environment, and is skipped on forks.

### Current limitations (Module 8)

- The live investigation/remediation workflow is **not yet routed** through `GovernedProvider`
  (budget, breaker, usage, trace). The pieces are implemented and tested, and the PostgreSQL budget is
  atomic, but the workflow still builds its provider as before (fake, or `UnsupportedProvider`), so
  there is no paid call path in the app to govern yet. Wiring needs a real application provider
  adapter plus PostgreSQL-backed budget and usage in the worker.
- Published prompt text is versioned, gated, and audited, but the application does not yet render a
  workspace-published prompt at runtime.
- The judge is a library plus tests: it is not wired into API-created runs, and no judge has been
  calibrated against real human labels yet.
- Online sampling and dataset promotion are implemented as tested rules; they are not enqueued from
  production traffic. Experiment arm outcomes come from evaluation runs, not live traffic.
- Latency and tokens in fake-tier reports are derived from payload size and are labelled synthetic.
  No real-model run has been executed in this repository.
- The LangSmith exporter is covered by failure-isolation tests only; it was not run against the
  hosted service. Langfuse is not implemented (the trace interface makes an adapter straightforward).
- The Anthropic pricing seed models per-million rates for prompts up to 100K tokens only.
