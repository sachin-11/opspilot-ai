# OpsPilot AI

OpsPilot AI is a multi-tenant incident-response platform. This repository contains **Module 1: the incident-management core**, built on the Module 0 foundation.

## Module 1 capabilities

Included: workspaces, monitored services, manual incidents, status/severity transitions, optimistic concurrency, append-only timelines, notes, deterministic event ingestion and deduplication, cursor pagination and filters, Redis Pub/Sub WebSockets, an operational Next.js dashboard, and an idempotent development seed. Module 0 health endpoints, RQ worker, PostgreSQL + pgvector, Docker Compose, tests, and CI remain intact.

### Intentionally deferred to later modules

AI agents, LangGraph/LLM/RAG workflows, fuzzy correlation, GitHub/AWS integrations, production authentication/authorization, billing, Kafka, Kubernetes, and automated remediation.

## Architecture

```
Browser -> web (Next.js) ; Browser -> api (FastAPI) -> postgres (pgvector), redis <- worker (RQ)
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

Migration `0002` creates the Module 1 workspace, service, incident, timeline, and ingested-event tables with tenant-scoped constraints and indexes. Validate reversibility on a disposable database with:

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

Workspace IDs in URLs scope every tenant-owned query, but **they are not authentication**. REST and WebSocket endpoints are intentionally unauthenticated in Module 1. Do not expose this build as a production-secure multi-tenant service; authenticated workspace claims and authorization belong to a later module. Event attributes reject credential-like keys and payloads above 64 KiB.

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
