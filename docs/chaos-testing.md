# Dependency-failure drill

`python -m opspilot_api.maintenance.chaos` stops PostgreSQL, then Redis, in a **throwaway** Compose project and checks that the API degrades the way this repository says it does, then that it recovers without being restarted. Services are always started again, even when a check fails.

It refuses to run unless the Compose project name contains `check`, `chaos` or `drill` and the API reports a non-production environment, because it stops real containers. It signs in with the development-only local provider, so the stack needs `AUTH_PROVIDER=local`.

## Run it

Use a separate project name and host ports so your development stack is never involved:

```powershell
# An env file with throwaway values and non-default host ports (API_HOST_PORT, POSTGRES_HOST_PORT,
# REDIS_HOST_PORT), plus the required settings from .env.example.
docker compose -p chaos-check --env-file chaos.env up -d --wait postgres redis api
docker compose -p chaos-check --env-file chaos.env exec -T api alembic upgrade head
cd services/api
uv run python -m opspilot_api.maintenance.chaos --project chaos-check --env-file ..\..\chaos.env --api-base http://localhost:<API_HOST_PORT>
docker compose -p chaos-check --env-file ..\..\chaos.env down -v
```

## What it checks

| Failure | Expected behaviour |
| --- | --- |
| PostgreSQL stopped | `/health/ready` becomes 503 naming postgres as down; `/health/live` stays 200 (the process is not restarted for a dependency outage); an authenticated read fails fast as **503 with `Retry-After`** and a clean JSON error, not a 500 or a stack trace |
| PostgreSQL restarted | readiness is 200 again and reads work, with no API restart |
| Redis stopped | readiness 503 naming redis as down; reads still work (low-risk rate limits fail open); starting an investigation is **refused** (429), not silently accepted (high-risk rules fail closed) |
| Redis restarted | readiness 200 and an investigation can be started again (202) |

## Result and a defect it found

Run on 2026-10-31 on a laptop against a throwaway stack: all checks passed, recovery took 1 to 2 seconds after each dependency returned, and an authenticated read during the PostgreSQL outage failed in about 4 seconds.

The first run exposed a real defect: a PostgreSQL outage surfaced to clients as **HTTP 500** (`internal_error`), which looks like a bug and gives callers no retry hint. Database connectivity errors and connection-pool timeouts now return **503 `dependency_unavailable` with `Retry-After: 5`**, and only the error class is logged (the message can name hosts and users). A unit test covers the three error types and checks that no host or user leaks into the response.

## Not covered

Broker (Kafka) outages for the webhook pipeline are covered by `tests/test_observability_kafka.py`. This drill does not cover network partitions, slow dependencies (as opposed to down ones), a database failover, or the worker and consumers.
