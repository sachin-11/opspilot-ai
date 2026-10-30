# OpsPilot AI

OpsPilot AI is a multi-tenant incident-response platform. This repository contains **Module 2: the human-reviewed AI investigation engine**, built on the Module 0/1 foundation.

## Module 2 capabilities

Included: the complete Module 1 incident core plus RQ-executed LangGraph investigations, PostgreSQL checkpoints, typed read-only tools, redacted and deduplicated evidence, structured hypotheses/reports, retryable-only provider fallback, usage/cost circuit breakers, human approval/revision/rejection, stale-worker recovery, real-time progress, and an incident-detail investigation UI.

### Intentionally deferred to later modules

RAG/runbook embeddings, GitHub/code access, AWS/CloudWatch, Slack, production authentication/authorization, billing, Kafka, Kubernetes, and automated remediation. **All remediation shown by Module 2 is advisory only; OpsPilot never executes production changes.**

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

## Current security boundary

Workspace IDs in URLs scope every tenant-owned query, but **they are not authentication**. REST and WebSocket endpoints are intentionally unauthenticated. Do not expose this build as a production-secure multi-tenant service. Evidence is bounded and common secret forms are redacted, but this is defense in depth rather than a complete DLP system. Incident/log content remains untrusted. Hidden chain-of-thought and full prompts are neither stored nor returned; only structured outputs, evidence references, concise summaries, and safe errors are persisted.

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
