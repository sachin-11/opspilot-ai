# OpsPilot AI - Architecture (Module 0)

## Container view

```mermaid
flowchart LR
    user([Browser])
    subgraph compose[Docker Compose]
        web["web<br/>Next.js dashboard"]
        api["api<br/>FastAPI"]
        worker["worker<br/>RQ worker"]
        pg[("postgres<br/>PostgreSQL + pgvector")]
        redis[("redis<br/>queue / cache")]
    end
    user -->|HTML, JS| web
    user -->|"fetch /health/ready (CORS)"| api
    api -->|SQLAlchemy async, psycopg| pg
    api -->|redis-py asyncio| redis
    worker -->|RQ| redis
```

## Responsibilities

| Container | Responsibility in Module 0 |
| --- | --- |
| **web** | Serves the status dashboard. The browser calls the API directly and renders loading / ready / degraded / unreachable states. |
| **api** | Versioned HTTP API (`/api/v1`), health endpoints, central settings, structured logging with correlation IDs, CORS, error format. Owns the PostgreSQL and Redis client lifecycle. |
| **worker** | Runs RQ jobs from the `default` queue. Only a `ping` smoke-test job exists. Same image and settings as the API. |
| **postgres** | System of record (no business tables yet). Alembic is the only schema writer. |
| **pgvector** | PostgreSQL extension enabled by migration `0001`, reserved for later embedding storage. No vector columns exist yet. |
| **redis** | RQ job queue backend (and future cache). Persisted with AOF to a named volume. |

## Readiness request flow

1. The browser loads `/` from **web**; the page validates `NEXT_PUBLIC_API_BASE_URL`, then renders the dashboard.
2. The dashboard calls `GET {API}/health/ready` on load, every 10 s, and on "Refresh".
3. **api** probes PostgreSQL (`SELECT 1`) and Redis (`PING`) concurrently, each under `DEPENDENCY_TIMEOUT_SECONDS`.
4. Both up: HTTP 200 `{"status":"ready", ...}`. Any down: HTTP 503 `{"status":"degraded", ...}` with per-dependency `up`/`down`. Errors are logged by class only; no credentials or connection strings are returned or logged.
5. The dashboard shows "All systems ready" (200), "Degraded" (503 body), or "Backend unreachable" (network failure, other status, or a malformed body - never reported as healthy).

`GET /health/live` does not touch any dependency, so a container health check cannot be failed by an outage it cannot fix.

## Key decisions

- **Modular monolith.** One backend codebase (`services/api`), run as two processes (API, worker) from one image. Module boundaries are Python packages, not network hops.
- **App factory.** `create_app(settings)` makes the app testable without environment mutation; readiness is injected via a FastAPI dependency so tests can fake dependency states.
- **Fail-fast configuration.** Pydantic Settings has no credential defaults; startup fails on missing or invalid values. Secrets are `SecretStr`. Wildcard CORS is rejected outside `development`.
- **Lazy connections.** The API starts even if PostgreSQL/Redis are down and reports it through readiness (503) rather than crashing in a restart loop.
- **Sync Alembic, async app.** Migrations use a synchronous psycopg connection (simple, robust); the app uses SQLAlchemy async.
- **Compose-level health ordering.** `api` waits for healthy `postgres` and `redis`; `worker` waits for `redis`; `web` waits for `api`. The worker's health check verifies it is registered in Redis.
- **Loopback-only host ports.** Published ports bind to `127.0.0.1` and are configurable (`*_HOST_PORT`) to avoid collisions.
- **Browser-side fetch.** The dashboard polls the API from the browser (needs CORS) so it reflects what a user's browser can reach, instead of proxying through the web server.

## Why a modular application, not microservices

There is one team-sized domain and no independent scaling, deployment cadence, or data-ownership boundary yet. Microservices would add network failure modes, distributed tracing/versioning, and operational cost with no benefit. The API and worker already deploy separately from one codebase; if a module later needs independent scaling or isolation, it can be split along its existing package boundary.

## Future extension points (not implemented)

- Domain modules under `services/api/src/opspilot_api/` (each with router, service, schemas), mounted on `/api/v1`.
- Alembic migrations adding tenant and domain tables; vector columns using the enabled `pgvector` extension.
- Additional RQ queues and job modules in `opspilot_api/worker/`.
- Authentication/authorization middleware in front of `/api/v1`.
- Metrics/tracing exporters alongside the existing correlation IDs.
