# OpsPilot AI

OpsPilot AI is planned as a multi-tenant, autonomous incident-response platform. This repository currently contains **Module 0: the foundation** only.

## Module 0 scope

Included: monorepo layout, FastAPI API (`/health/live`, `/health/ready`, `/api/v1/meta`), RQ worker with a `ping` smoke job, PostgreSQL + pgvector (extension enabled via Alembic), Redis, a Next.js status dashboard, Docker Compose, quality tooling, GitHub Actions CI, and documentation.

### Intentionally deferred to later modules

AI agents, LangGraph workflows, GitHub integration, authentication/authorization, incident management, tenants/users and any business tables, billing, Kafka, AWS/cloud deployment, navigation/incident UI.

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
