# Load testing

Two tools, for two different questions. Both are for development and throwaway stacks; neither is meant to be pointed at production, and neither output is a capacity promise.

| Tool | Question | Needs |
| --- | --- | --- |
| `python -m opspilot_api.observability.loadgen` | How fast does the signed-webhook pipeline accept and process events, and does it stay correct (duplicates, dead letters, lag)? | Compose stack with the broker and consumers, `AUTH_PROVIDER=local` (or `--auth-token`) |
| `loadtests/api-reads.js` (k6) | Do the authenticated read paths and the health endpoint hold a latency and error budget at a given request rate? | `AUTH_PROVIDER=local` |

Both sign in through the development-only local identity provider, which needs your web origin (default `http://localhost:3300`) in `CORS_ORIGINS`. Neither works against a stack running `AUTH_PROVIDER=oidc`; start a separate throwaway stack instead (a different compose project name and host ports keeps it away from your development data).

## Webhook pipeline

```bash
cd services/api
uv run python -m opspilot_api.observability.loadgen --api-base http://localhost:8300 \
  --events 2000 --rate 200 --duplicate-percent 10 --invalid-percent 2 --seed 7
```

Control calls (workspace, services, sources, read-back) use a bearer token; the webhook deliveries are signed and unauthenticated, as in real use. Results go to `benchmark-results/` (git-ignored). Earlier runs are summarised in the README.

## Read paths (k6)

```powershell
docker run --rm -i --add-host=host.docker.internal:host-gateway `
  -v "${PWD}/loadtests:/scripts" grafana/k6 run `
  -e API=http://host.docker.internal:8300 -e RATE=30 -e DURATION=30s /scripts/api-reads.js
```

The script creates a throwaway workspace with 20 incidents, then holds a constant arrival rate on three paths: the incident list (`RATE` per second), incident detail and `/health/ready` (half of `RATE` each). It exits non-zero (99) when a threshold is crossed: under 1% failed requests, over 99% checks passing, p95 under 500 ms for list and detail, and p95 under 300 ms for readiness. Change the thresholds to match your own objective.

## Measured on 2026-10-31 (one Windows laptop, Docker Desktop, single API container with one worker)

These numbers describe this machine and this run only.

**Read paths**, 15 s to 30 s per run (arrival rate is for the incident list; detail and readiness run at half that):

| List rate | Result | p95 list / detail / readiness | Failed requests |
| ---: | --- | --- | ---: |
| 30/s | thresholds met | 368 / 344 / 191 ms | 0.0% |
| 100/s | thresholds crossed | about 8.9 s / 8.8 s / 2.2 s | 6.4% |
| 200/s | thresholds crossed | about 12 s / 11 s / 2.1 s | 6.7% |
| 400/s | thresholds crossed | about 10.8 s / 10.6 s / 2.1 s | 12.0% |

The knee is somewhere between 30/s and 100/s of list traffic here (the total including the other two paths is double that). It was not narrowed further.

**What failed, from the API logs under overload** (the interesting part):

- `/health/ready` answered **503** repeatedly. Its database probe shares the saturated connection pool and has a 2 second timeout, so a busy but healthy API reports itself as not ready. Behind an orchestrator that removes unready instances from rotation, this can turn overload into a cascade. Consider giving the probe its own tiny pool or a longer budget before relying on it for traffic decisions.
- Requests that waited longer than `DB_POOL_TIMEOUT_SECONDS` failed with `QueuePool limit of size 10 overflow 10 timed out` (HTTP 500). This is the intended fail-fast behaviour of the pool settings, but the 500 is not a clean "try again later"; mapping it to 503 with `Retry-After` would be friendlier.
- CPU use during the run was not measured, so the cause of the knee is not established. The likely suspects are the single uvicorn worker and the connection pool (the pool errors above point at the latter). More workers or replicas, a larger pool within PostgreSQL's `max_connections`, and read caching are the obvious levers to try; none were tried.

**Webhook pipeline**, 300 events at a requested 50/s, 10% duplicates, 2% invalid, two services, single consumers:

| Accepted/s | Processed/s | Request p50 / p95 | End to end p50 / p95 | Duplicates suppressed | Dead-letter rate | Errors | Max lag |
| ---: | ---: | --- | --- | --- | ---: | ---: | ---: |
| 26.1 | 18.5 | 478 / 2458 ms | 3.0 / 4.1 s | 34 of 34 | 2.3% | 0 | 50 |

This matches the earlier README benchmark qualitatively: the synchronous ingress path is the limit (the requested 50/s was not reached), correctness held (every duplicate suppressed, invalid payloads dead-lettered, zero non-2xx), and the backlog drained to zero.
