# OpsPilot AI - Architecture (Modules 0-5)

## Container view

```mermaid
flowchart LR
    user([Browser])
    subgraph compose[Docker Compose]
        web["web<br/>Next.js operations console"]
        api["api<br/>FastAPI"]
        worker["worker<br/>RQ worker"]
        pg[("postgres<br/>PostgreSQL + pgvector")]
        redis[("redis<br/>queue / Pub/Sub")]
    end
    user -->|HTML, JS| web
    user -->|"REST /api/v1 + WebSocket"| api
    api -->|SQLAlchemy async, psycopg| pg
    api -->|redis-py asyncio| redis
    redis -->|RQ jobs| worker
    worker -->|domain rows + LangGraph checkpoints| pg
```

## Responsibilities

| Container | Responsibility in Module 0 |
| --- | --- |
| **web** | Serves the incident dashboard, list, detail, creation, and service settings. TanStack Query owns server state; a reconnecting WebSocket invalidates affected caches. |
| **api** | Versioned REST/WebSocket API, health endpoints, tenant-scoped incident application services, structured logging, and PostgreSQL/Redis lifecycles. |
| **worker** | Runs durable investigation graphs and smoke-test jobs. It owns model/tool execution and recovers stale running investigations at startup. |
| **postgres** | System of record for incidents, investigation steps/evidence/reviews, reports, and LangGraph checkpoints. Alembic owns application schema; checkpoint setup is explicit. |
| **pgvector** | PostgreSQL extension enabled by migration `0001`, reserved for later embedding storage. No vector columns exist yet. |
| **redis** | RQ backend and workspace-isolated incident Pub/Sub channels. Persisted with AOF to a named volume. |

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

## Module 3 infrastructure requirement

Module 3 accepts private operational files, so local development needs one durable, non-public
filesystem location shared by the API and worker. Docker Compose therefore adds a named
`knowledge_data` volume mounted at `/app/data/knowledge`; no new service, database, queue, or
network endpoint is introduced. PostgreSQL remains the metadata, full-text, vector, and citation
system of record. The filesystem abstraction keeps generated storage keys separate from user
filenames and leaves an S3-compatible implementation as a deployment adapter rather than a local
dependency.

## Module 1 data and isolation

`Workspace` is the future tenant boundary. Every service, incident, timeline, and ingested-event query includes `workspace_id`; nested resources return 404 when the resource exists in a different workspace. A composite service constraint allows the same slug in different workspaces. An incident can only reference a service found by `(workspace_id, service_id)`.

This is data scoping, not authorization. Workspace IDs are explicitly selected in URLs and the WebSocket path; Module 1 has no authenticated workspace claim. Authentication and authorization middleware can later replace the explicit context without changing domain-service signatures.

## Transaction and concurrency boundaries

- Manual creation writes the incident and `incident_created` timeline row in one transaction.
- Status, severity, and note operations use `UPDATE ... WHERE workspace_id = ? AND id = ? AND version = ?`; success increments `version`, while a stale write returns HTTP 409. The timeline append shares that transaction.
- Ingestion takes a transaction-scoped PostgreSQL advisory lock derived from `(workspace_id, idempotency_key)`. The unique constraint is the final concurrent-replay backstop. Correlation, incident creation, ingested-event persistence, and timeline append commit together.
- Redis publication happens after the database commit. Pub/Sub is intentionally ephemeral; Redis failure is logged by error class and does not turn a durable REST mutation into a false failure. Clients recover by refetching REST state.

## Deterministic event correlation

Module 1 uses SHA-256 over normalized workspace ID, service ID, event type, and error signature. Case and whitespace are normalized; the full alert summary is not included because timestamps, UUIDs, and changing metrics make it unstable. An unresolved incident with the same fingerprint is reused; otherwise a new incident is created. This is explainable, testable, and account-free. Semantic/AI-assisted correlation is deferred until deterministic behavior provides a safe baseline.

```mermaid
sequenceDiagram
    participant Monitor as External monitor
    participant API as FastAPI ingestion service
    participant PG as PostgreSQL
    participant Redis as Redis Pub/Sub
    participant Web as Next.js client
    Monitor->>API: POST /events + Idempotency-Key
    API->>PG: Begin + advisory lock
    API->>PG: Find prior key
    alt first submission
        API->>PG: SHA-256 fingerprint lookup (unresolved)
        alt matching incident
            API->>PG: Attach ingested event
        else no match
            API->>PG: Create incident + incident_created
        end
        API->>PG: Append alert_received timeline row
        API->>PG: Commit
    else replay
        PG-->>API: Original event and outcome
    end
    API->>Redis: Publish workspace envelope
    Redis-->>Web: incident.created/event_correlated
    Web->>API: Invalidate and refetch affected REST queries
    API-->>Monitor: event_id, incident_id, result
```

## Real-time lifecycle

Each socket subscribes only to `opspilot:workspace:{workspace_id}:incidents`. Outgoing payloads are validated by `RealtimeEnvelope`. The server sends `connection.established`, handles application `ping`/`pong`, and unsubscribes/closes its Redis Pub/Sub object in `finally`. The browser reconnects with exponential backoff capped at 30 seconds and marks the connection degraded without blocking REST interaction.

## Future extension points (not implemented in Module 2)

- Authenticated workspace claims and authorization middleware in front of `/api/v1` and WebSockets.
- AI-assisted correlation and vector columns using the enabled `pgvector` extension.
- Additional RQ queues and job modules in `opspilot_api/worker/`.
- Metrics/tracing exporters alongside the existing correlation IDs.

## Investigation job lifecycle

```mermaid
sequenceDiagram
    participant Web
    participant API
    participant Redis
    participant Worker
    participant PG
    Web->>API: POST incident/investigations
    API->>PG: Insert queued investigation
    API->>Redis: Enqueue stable investigation ID
    API-->>Web: 202 queued
    Redis->>Worker: RQ job
    Worker->>PG: Run graph + persist steps/evidence/checkpoints
    Worker-->>Web: Workspace WebSocket progress via Redis
    Worker->>PG: Pause at awaiting_review
```

API handlers only validate/shape requests and enqueue IDs. A partial unique PostgreSQL index enforces one queued/running/awaiting-review run per incident under concurrency. Job re-delivery uses the stable graph thread ID and unique `(investigation_id, node_name, attempt)` / evidence-hash constraints. A worker restart fails provably abandoned `running` rows after the bounded stale threshold; checkpointed review waits remain resumable.

## LangGraph node flow

```mermaid
flowchart LR
    A[Load context] --> B[Triage]
    B --> C[Plan bounded tools]
    C --> D[Gather evidence]
    D --> E[Generate hypotheses]
    E --> F[Critique / abstain]
    F --> G[Produce cited report]
    G --> H{{Human review interrupt}}
    H -->|approve| I[Complete]
    H -->|revision feedback| J[Revision node]
    J --> H
    H -->|reject| K[Reject]
```

Graph state contains JSON-serializable IDs, snapshots, structured outputs, feedback, and usage totals only. Sessions, Redis clients, API keys, and provider objects live in worker runtime dependencies. `PostgresSaver.setup()` is run explicitly and every invocation uses `graph_thread_id`; a checkpoint is consulted before creating initial state.

## Human review and resume

```mermaid
sequenceDiagram
    participant Graph
    participant PG
    participant Reviewer
    participant API
    participant RQ
    Graph->>PG: Checkpoint + awaiting_review
    Reviewer->>API: approve / revise / reject + version
    API->>PG: Optimistic update + append-only review
    API->>RQ: Enqueue review ID
    RQ->>PG: Load scoped review and checkpoint
    RQ->>Graph: Command(resume=review payload)
    alt revision
        Graph->>Graph: Dedicated revision node (no full recollection)
    else approval or rejection
        Graph->>PG: Terminal status + incident timeline event
    end
```

## Model routing and circuit breakers

```mermaid
flowchart TD
    A[Validated structured request] --> B[Primary provider]
    B -->|valid output| C[Record usage/cost]
    B -->|retryable failure| D[Bounded exponential retry + jitter]
    D -->|exhausted and fallback configured| E[Fallback provider]
    B -->|auth/config/programming/validation/spend error| F[Fail safely; no fallback]
    E -->|valid output| C
    C --> G{Runtime/calls/tokens/cost within limits?}
    G -->|yes| H[Continue graph]
    G -->|no| F
```

Input/output prices are configuration, not model-name assumptions. Limits are checked before additional work and after usage is recorded. Limit failures are sanitized, durable, and published without prompts or raw logs.

## Evidence and reasoning boundary

Every internal tool validates typed, bounded inputs and repeats workspace/incident predicates internally. Tools are read-only and have no shell, filesystem, arbitrary SQL, or unrestricted network capability. Results are redacted before model use and persistence, truncated to configured bounds, and deduplicated by a stable SHA-256 content hash. Hypotheses and report claims are rejected if they cite IDs not present in that investigation.

Hidden reasoning is intentionally absent from graph state, database rows, API schemas, logs, WebSocket messages, and UI. Persisting it would enlarge the sensitive-data surface without improving reviewability. Human review instead receives inspectable evidence, structured hypotheses, citations, confidence, assumptions, concise step summaries, and the advisory plan.

## Module 3 knowledge architecture

PostgreSQL is already the tenant-scoped system of record and supports transactional FTS plus pgvector. Keeping metadata, current-version selection, lexical candidates, vector candidates, citations, and retrieval logs in one database makes atomic replacement and workspace predicates auditable. A separate vector database would add replication, consistency, tenancy, backup, and failure modes without a demonstrated scale requirement.

```mermaid
sequenceDiagram
    participant Web
    participant API
    participant Store as Private object storage
    participant RQ
    participant Worker
    participant PG as PostgreSQL + pgvector
    Web->>API: Upload file or direct content
    API->>PG: Validate workspace/service; create version + queued job
    API->>Store: Atomic original write under generated key
    API->>RQ: Enqueue stable job ID
    API-->>Web: 202 queued
    RQ->>Worker: Ingestion job
    Worker->>Store: Read and verify hash
    Worker->>Worker: Extract, normalize, redact, deterministic chunk
    Worker->>Worker: Reuse/cache or batch embeddings
    Worker->>PG: Transactionally replace version chunks
    Worker->>PG: Mark ready + atomically switch current_version_id
    Worker-->>Web: Concise workspace WebSocket events
```

Markdown heading hierarchy, paragraph boundaries, PDF pages, and offsets are preserved. Heading context is included in redacted chunk text so lexical search remains structure-aware. Large blocks use deterministic token windows with bounded overlap; content hashes make repeated inputs and embedding cache keys stable. The original object is never silently rewritten.

```mermaid
flowchart LR
    Q[Validated scoped query] --> L[FTS candidates<br/>workspace + current ready filters]
    Q --> E[Fake/production embedding]
    E --> V[Cosine candidates<br/>workspace + model/dimension + current ready filters]
    L --> R[RRF by component ranks]
    V --> R
    R --> O{Rerank selected?}
    O -->|no| C[Relevance gate + top K]
    O -->|yes| B[Bounded reranker]
    B -->|success| C
    B -->|retryable failure| R
    C --> S[Stable citations + safe diagnostics + retrieval log]
```

Lexical and cosine scores are retained for diagnostics but never numerically mixed. RRF combines ranks and deduplicates chunk IDs. Variant-specific relevance gates allow an explicit no-useful-result response. Reranker authentication/configuration errors fail loudly; only retryable availability failures fall back to RRF.

```mermaid
sequenceDiagram
    participant Graph as Investigation graph
    participant Tool as search_runbooks tool
    participant Retrieval
    participant PG
    Graph->>Tool: Typed query, service, types, top K
    Tool->>Retrieval: Workspace + investigation IDs
    Retrieval->>PG: Scoped hybrid candidate queries
    PG-->>Retrieval: Current ready chunks only
    Retrieval-->>Tool: Bounded excerpts, scores, citations
    Tool-->>Graph: Knowledge evidence or no-useful-runbook observation
    Graph->>Tool: Optional get_knowledge_chunk scoped by workspace
    Tool-->>Graph: Untrusted EvidenceItem referencing chunk ID
```

Retrieved text is evidence data, never policy or system instruction. The graph may cite it but cannot use it to expand tool permissions. Retrieval unavailability or low relevance produces a bounded observation and does not by itself fail an otherwise valid investigation.

```mermaid
stateDiagram-v2
    [*] --> Uploaded
    Uploaded --> Extracting
    Extracting --> Chunking
    Chunking --> Embedding
    Embedding --> Ready
    Uploaded --> Failed
    Extracting --> Failed
    Chunking --> Failed
    Embedding --> Failed
    Ready --> Current: atomic switch after all chunks persist
    Failed --> PriorCurrent: prior ready version remains selected
    Current --> ReplacementUploaded: new upload/reindex
    ReplacementUploaded --> Current: replacement fails or is cancelled
    ReplacementUploaded --> NewCurrent: replacement becomes fully ready
```

Version numbers are allocated under a PostgreSQL advisory lock. Jobs check cancellation between stages and use unique version/chunk constraints for idempotency. Retry reuses identical embeddings by workspace, content hash, model, and dimension. Stale running jobs are recovered at worker startup; stored failures contain sanitized codes/messages, not stack traces or connection strings.

All document, chunk, job, retrieval-log, and RAG-tool repository methods begin with `workspace_id` predicates. Both lexical and vector branches enforce the tenant boundary before ranking, and the current-version join excludes archived, failed, processing, and stale content. This remains data isolation rather than authentication until a later module supplies trusted workspace claims.

## Module 4 GitHub architecture (read-only)

No new infrastructure is introduced: the integration reuses PostgreSQL (metadata, full-text, pgvector), Redis/RQ (jobs), and the workspace WebSocket. The worker now runs with the RQ scheduler enabled so rate-limited jobs can be released at the provider's reset time.

### GitHub App authentication
The provider interface (`RepositoryProvider`) exposes only `get_*`, `list_*`, and `last_rate_limit` methods; `GitHubProvider` and `FixtureProvider` implement it and nothing outside `github_provider.py` knows GitHub response shapes. `GitHubProvider` signs a 9-minute RS256 app JWT from a server-side key (environment value or file reference) and exchanges it for an installation token requesting only `contents`, `metadata`, `pull_requests`, and `deployments` at `read`. That exchange is the only non-GET request. Tokens are cached in process memory until one minute before expiry; they are never persisted, returned, or logged, and provider errors carry only status classes. Repository, owner, revision, and path arguments are validated before a URL is built, so no caller-supplied URL or revision reaches GitHub.

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant API as FastAPI webhook route
    participant PG as PostgreSQL
    participant RQ as Redis / RQ
    participant W as Worker
    GH->>API: POST /integrations/github/webhook (raw body + headers)
    API->>API: Size bound, then HMAC-SHA256 over raw bytes (constant time)
    alt invalid or missing signature
        API-->>GH: 401 (nothing stored, nothing enqueued)
    else valid
        API->>API: Require delivery ID and event, parse JSON, normalize documented fields
        API->>PG: Insert delivery (unique delivery ID)
        alt duplicate delivery
            API-->>GH: 200 duplicate (no new work)
        else unsupported event
            API-->>GH: 202 ignored (recorded)
        else supported
            API->>RQ: Enqueue job (delivery row ID)
            API-->>GH: 202 queued
            RQ->>W: run_webhook_delivery
            W->>PG: Resolve installation to workspace, apply event idempotently
            W-->>GH: (no call back) publish repository.webhook_processed
        end
    end
```

### Webhook verification and queueing
Verification precedes parsing and database access. Only selected normalized fields are stored in `github_webhook_deliveries` (never the raw payload), and the delivery ID is unique so GitHub redelivery cannot duplicate work; the single exception is a delivery whose own enqueue failed (`enqueue_failed`), which is re-enqueued. The worker maps the installation to its workspace, ignores unknown installations/repositories, and applies installation suspend/unsuspend/delete, repository add/remove, push (default branch only), pull-request, deployment, and deployment-status events idempotently.

### Repository synchronization and snapshot activation
```mermaid
flowchart TD
    A[Sync job / push webhook / manual reindex] --> B{Installation active?\nRepository not removed?}
    B -- no --> X[Fail loudly: no provider call]
    B -- yes --> C[Advisory lock per repository]
    C --> D[Refresh metadata, commits, deployments, pull requests]
    D --> E[Create or reuse unique snapshot for head commit]
    E --> F[Read bounded tree and apply allow/deny policy]
    F --> G[Fetch allowed files; skip binary, generated, oversize]
    G --> H[Redact secrets line by line; chunk by symbol with exact lines]
    H --> I[Reuse cached embeddings; embed the rest in batches]
    I --> J{Limits, timeout, parse threshold,\ncancellation OK?}
    J -- no --> K[Snapshot failed or cancelled\nprevious ready snapshot stays active]
    J -- yes --> L[One transaction: insert files and chunks,\nsnapshot ready, switch active_snapshot_id]
    L --> M[Prune old snapshot rows, publish events]
```
`repositories.active_snapshot_id` is the single activation point. Search joins it, so an unfinished, failed, cancelled, or stale snapshot can never be searched, and a failed run changes nothing visible. Snapshots are unique per (repository, commit, branch), so retries and duplicate pushes are idempotent. Incremental sync deliberately builds a new bounded snapshot at the new head instead of patching a live one: it is simpler to reason about and can never expose a half-updated index, while the content-keyed embedding cache keeps the cost of re-embedding unchanged chunks near zero.

### Code chunking and retrieval
Chunking is deterministic and works on already-redacted text whose line count equals the original's, so `start_line`/`end_line` map exactly onto the source. Python uses `ast` (parse only, never executed) for functions, classes, and methods; TypeScript/JavaScript, Go, Java/Rust, shell, Terraform, SQL, and Markdown use column-0 declaration heuristics; other text uses bounded windows. Oversized symbols split into windows with a small overlap only because a split is required, and a file that fails to parse falls back to windows and counts toward a configurable failure ratio. Chunks are de-duplicated per snapshot by content hash. The full-text vector treats identifier punctuation and camelCase boundaries as word breaks; lexical queries use OR semantics over sanitized terms bound as a parameter. Hybrid retrieval fuses lexical and cosine ranks with Reciprocal Rank Fusion (never raw scores) and optionally reranks a bounded candidate set with the Module 3 reranker abstraction. Every query repeats the workspace predicate and joins the active snapshot.

### Incident-to-change correlation
```mermaid
flowchart LR
    I[Incident: service, environment,\nopened time, terms from title and events] --> M[Mapped repositories\nenvironment and branch match]
    M --> C[Commits and deployments\nin the lookback window]
    C --> P{Inside mapped\npath prefix?}
    P -- no --> D[Excluded]
    P -- yes --> S[Sum bounded signal weights:\ndeploy timing, term overlap, recency,\nprefix, revert relations, environment]
    S --> V[Apply contradicting signals:\nreverted before incident x0.3,\nfailed or other-environment deploy]
    V --> T{Score >= minimum?}
    T -- no --> D
    T -- yes --> R[Rank by score, then proximity, then SHA\nconfidence category + signals + citation]
```
The score is a fixed, documented sum (`repositories/correlation.py`), so identical inputs always rank identically and every signal is explainable. It describes correlation only; every response and evidence item says so.

### LangGraph code investigation
```mermaid
sequenceDiagram
    participant G as Investigation graph
    participant T as Code tools (read-only)
    participant PG as PostgreSQL
    G->>T: list_recent_changes / rank_suspect_changes / search_repository_code
    T->>PG: Scope from trusted context: workspace, incident, service mappings
    alt no mapping or data unavailable
        T-->>G: Explicit observation, no evidence
    else mapped
        PG-->>T: Recent commits, deployments, ranked candidates, active-snapshot chunks
        T-->>G: Evidence items: repository_commit, deployment, commit_diff, code_chunk
    end
    G->>T: Optional get_commit_summary / get_commit_diff / get_code_excerpt / get_pull_request_context
    T->>PG: Revision or chunk must belong to a mapped repository of this workspace
    G->>G: Hypotheses and report cite evidence IDs; code findings are labeled correlation
```
Tools call no provider, so a GitHub outage cannot fail an investigation; they read synchronized state and report data freshness (repository status, last sync, failure code). Arguments are bounded and validated; unknown or ambiguous revisions, unmapped repositories, and other workspaces' chunks become rejected observations rather than errors or leaks. Source text, commit messages, and diffs are untrusted evidence and cannot alter tool policy or system instructions.

### Rate limits and failure handling
`ProviderRateLimitError` carries a retry delay derived from `Retry-After` or the rate-limit reset header. Jobs reschedule themselves with the RQ scheduler (bounded by `REPO_RATE_LIMIT_MAX_RETRIES`; transient outages use exponential backoff), the repository is marked `failed` with a sanitized code while its last ready snapshot stays searchable, and the last observed rate-limit headers are stored on the installation without credentials. Authentication failures are not retried. Stale `indexing`/`syncing` work left by a crashed worker is failed at worker startup.

## Module 5 observability pipeline

The pipeline turns external production signals into Module 1 incident events without any step that can silently lose or duplicate one. It adds one infrastructure component, a Kafka-compatible broker (Redpanda locally, no ZooKeeper), behind an `EventBus` abstraction; PostgreSQL stays the system of record and Redis stays the WebSocket fan-out and rate-limit state store.

### Webhook ingress
`POST /api/v1/ingest/webhooks/{public_id}` addresses a source by an unguessable 192-bit public ID. The handler reads raw bytes (bounded before any parsing), recomputes `HMAC-SHA256("{timestamp}.{raw body}")` with the source secret in constant time, rejects missing/invalid/expired signatures (replay window `OBS_WEBHOOK_REPLAY_WINDOW_SECONDS`), and only then parses JSON, checks backpressure, and accepts the signal. Acceptance is *durable*: the receipt row is committed and the raw-topic record is acknowledged by the broker (`acks=all`, idempotent producer) before HTTP 202. No incident work happens in the request. Secrets are generated with 256 bits of entropy, shown once, and stored Fernet-encrypted (`OBS_SECRET_ENCRYPTION_KEY`); stronger key management is deferred to a deployment/security module.

```mermaid
sequenceDiagram
    participant S as External system
    participant API as FastAPI ingress
    participant PG as PostgreSQL
    participant K as Kafka-compatible broker
    participant R as Raw consumer
    participant N as Normalized consumer
    participant M1 as Module 1 ingest_event
    S->>API: POST webhook (raw body, timestamp, signature)
    API->>API: size limit, HMAC, replay window, JSON, backpressure
    API->>PG: insert SignalReceipt (unique ingress key) = received
    alt duplicate ingress key / event id
        API-->>S: 200 duplicate (original signal_id)
    else new
        API->>K: publish raw (key workspace:service, acks=all)
        alt not acknowledged
            API->>PG: receipt = failed(publish_failed)
            API-->>S: 503 Retry-After (never 202)
        else acknowledged
            API->>PG: receipt = published (guarded)
            API-->>S: 202 accepted
        end
    end
    R->>K: consume raw
    R->>R: validate schema, normalize, redact
    R->>K: publish normalized
    R->>PG: receipt = normalized (guarded) then commit offset
    N->>K: consume normalized
    N->>PG: receipt state + duplicate check
    N->>M1: ingest_event (idempotent, commits incident+timeline)
    N->>PG: receipt = processed, then commit offset
    N-->>S: WebSocket observability.signal_processed
```

### Topics, keys, and ordering
`opspilot.observability.raw.v1`, `.normalized.v1`, and `.dead-letter.v1` (three partitions locally). The record key is `workspace_id:service_id`, so one service's signals share a partition and are consumed in publish order; signals of different services (or workspaces) may interleave. Ordering is therefore guaranteed *per workspace+service only*, and not across the raw and normalized stages when retries occur. Headers carry routing metadata only (`schema_version`, `signal_id`, `workspace_id`, `source_id`, `replay_of`); the bus drops any other header, so credentials cannot travel as headers. Messages carry a schema version; an unsupported version is dead-lettered, never processed as a current message.

### Consumer responsibilities
* **Raw consumer** (`--role raw`, group `opspilot-raw-consumer`): validate envelope and version, verify the source belongs to the stated workspace/service, normalize deterministically (no LLM), redact, publish the normalized record, mark the receipt `normalized` (guarded so it can never downgrade a later state), then commit.
* **Normalized consumer** (`--role normalized`, group `opspilot-normalized-consumer`): validate, persist normalized state, suppress duplicates, call the existing Module 1 `ingest_event` (no correlation logic is duplicated), update the receipt, publish WebSocket updates, then commit.

### Delivery guarantees, idempotency, and transaction boundaries
The pipeline is **at-least-once**, not exactly-once. Offsets are committed only after the effect is durable, so a crash between effect and commit redelivers the message. Exactly-once would require one atomic transaction spanning the broker, PostgreSQL, and Redis; none exists here (a Kafka transaction cannot include a database commit, and a database commit cannot include a broker offset), so duplicates are made harmless instead of being claimed impossible:

| Step | Transaction | If the process dies afterwards |
| --- | --- | --- |
| Ingress | receipt insert (unique ingress key) -> broker ack -> guarded `published` | sender retries; the unique key returns the original signal or finishes a `publish_failed` one |
| Raw handler | publish normalized -> guarded `normalized` -> commit | redelivery republishes an identical normalized record; receipt state absorbs it |
| Normalized handler | `ingest_event` (one transaction: incident, event, timeline, keyed by idempotency key) -> receipt `processed` -> commit | redelivery replays into the same Module 1 result via the idempotency key |

Deduplication priority: trusted provider event ID, then source idempotency key, then a normalized fingerprint (`fp:` over workspace, service, source, type, error signature, and title) suppressed inside a sliding `OBS_DEDUP_WINDOW_SECONDS`. Identical raw bytes without any ID collapse within the same window bucket at ingress.

### Retry and dead-letter flow
```mermaid
flowchart TD
    M[Message consumed] --> V{Valid schema, version,\ntenant relationship?}
    V -- no --> T[Terminal: dead-letter immediately]
    V -- yes --> P[Process]
    P -- success --> C[Commit offset]
    P -- retryable failure\nbroker, DB, throttling --> B{Attempts < max\nand age < max age?}
    B -- yes --> W[Exponential backoff + jitter] --> P
    B -- no --> X[Dead-letter: retries_exhausted]
    T --> D[Persist DeadLetterSignal sanitized + bounded\npublish dead-letter topic\nreceipt = dead_lettered, source = failing]
    X --> D
    D --> C
    D -- dead-lettering itself fails --> U[Leave uncommitted; redelivered, never dropped]
    C --> R[Manual replay endpoint]
    R --> Q[Republish sanitized payload once,\nreplay_of header, audit row]
    Q --> M
```
Retries are bounded by attempts and message age, so there is no infinite loop; the only unbounded wait is when the failure store itself is down, where the message intentionally stays uncommitted. Dead letters keep a redacted payload up to 64 KiB (larger ones are kept without payload and are not replayable). Replay publishes once; later requests return `already_replayed` and each request writes an audit row. A replayed message that is still invalid is dead-lettered again rather than looping.

### CloudWatch evidence query
```mermaid
sequenceDiagram
    participant G as Investigation graph
    participant T as Observability tool
    participant PG as PostgreSQL
    participant P as Provider (CloudWatch or fixture)
    G->>T: query_service_logs / get_service_metrics / compare_metric_windows / get_alarm_context
    T->>PG: scope from trusted context: workspace, incident, service, source
    alt no source
        T-->>G: explicit "no observability source" observation
    else source configured
        T->>T: validate terms, clamp window and limits to the source configuration
        T->>P: bounded read-only call (allow-listed groups, namespaces, alarms)
        alt throttled / auth / unavailable
            P-->>T: classified error
            T->>PG: audit row (status, failure code, no results)
            T-->>G: unavailable observation (reason, retryable)
        else ok
            P-->>T: logs / datapoints / alarm
            T->>PG: audit row (counts and latency only)
            T-->>G: redacted EvidenceItems with citation labels
        end
    end
```
The CloudWatch adapter is read-only and bounded: it can call only `sts:AssumeRole` (temporary credentials from the configured role and an environment-variable external-ID reference), `logs:FilterLogEvents`, `cloudwatch:GetMetricStatistics`, `cloudwatch:DescribeAlarms`, and `cloudwatch:DescribeAlarmHistory`. Log groups, namespaces, and alarm names come only from the stored source configuration; tool inputs cannot select accounts, roles, regions, or groups. No access key is stored or returned.

### Backpressure and recovery
```mermaid
stateDiagram-v2
    [*] --> Accepting
    Accepting --> RateLimited: per-source or per-workspace rate exceeded (HTTP 429)
    Accepting --> Unavailable: broker down, rate state unavailable,\nconsumer lag over threshold, or dead-letter burst (HTTP 503)
    RateLimited --> Accepting: next window under the limit
    Unavailable --> Accepting: condition clears (health, lag, dead-letter window)
    RateLimited --> Unavailable: downstream also unhealthy
    note right of Accepting
        started/cleared transitions are published once
        (observability.backpressure_started / _cleared)
    end note
```
Nothing is buffered in memory: a signal is either durably accepted (202), rejected as a duplicate (200), or refused with an honest 429/503 and `Retry-After`. Rate-limit state is held in Redis and fails closed (503) if Redis is unavailable.

### LangGraph observability tools
`query_service_logs`, `get_service_metrics`, `compare_metric_windows`, and `get_alarm_context` are read-only and bounded. Workspace, incident, service, and source scope comes from the trusted investigation context; results are redacted, size-limited, audited without result contents, and returned as `observability_log`, `observability_metric`, `observability_alarm`, and `normalized_signal` evidence with stable citation labels. Provider failures produce explicit unavailable observations, and investigations without a source are unchanged. Returned text is untrusted evidence and shows correlation, not proven causation.
