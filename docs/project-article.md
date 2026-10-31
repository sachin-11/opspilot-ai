# OpsPilot AI: incident response where AI investigates and humans decide

*A walkthrough of the project, one screen at a time. Every screenshot is from the running application with seeded demo data. The AI provider in these screenshots is the deterministic one used for local development, so the reports show how the workflow behaves, not how smart a real model is.*

## The problem

When production breaks, the slow part is rarely typing the fix. It is working out what broke: correlating alerts, logs, recent deploys, runbooks and source code while people are awake at 2 AM. Automation can help with that reading and reasoning. What nobody wants is a model with the keys to production.

OpsPilot is my attempt at the middle path. The AI does the reading and proposes an explanation with its evidence. A human approves, rejects or asks for a revision. The system can write exactly one thing to the outside world, a fix branch and pull request, and only after an explicit approval. It never merges and never deploys.

## The architecture

![Architecture](architecture-diagram.png)

*Image 0. A modular monolith: a FastAPI API and an RQ worker from one codebase, a Next.js console that talks to the API only over HTTP, PostgreSQL with pgvector, Redis, Kafka for the signal pipeline, and Keycloak (or any OIDC provider) for sign-in.*

## 1. Sign in with your identity provider

![Sign in](images/01-sign-in.png)

*Image 1. The console never sees a password. It runs the OIDC authorization-code flow with PKCE and keeps the session in a sealed, HttpOnly cookie. Signing out also ends the identity provider's session, so the next sign-in asks for credentials again.*

## 2. The overview

![Overview](images/02-overview.png)

*Image 2. Active incidents by severity, the most recently updated incidents, and live system status. The page updates in real time over a WebSocket that authenticates with a single-use ticket and re-checks membership every few seconds.*

## 3. Incidents

![Incidents](images/03-incidents.png)

*Image 3. Incidents come from two places: people declaring them, and the signal pipeline turning alerts into incidents. Filters cover status, severity, service and environment, with cursor pagination.*

## 4. One incident, end to end

![Incident detail](images/04-incident-detail.png)

*Image 4. Severity, status, the service, and when the incident was opened, acknowledged, mitigated and resolved. Status changes use optimistic locking, so two responders cannot silently overwrite each other.*

![Timeline](images/05-incident-timeline.png)

*Image 5. The timeline is the incident's memory: alerts as they arrive, status changes, notes, and later the outcome of the investigation. Repeated alerts with the same fingerprint are correlated into one incident instead of opening a new one each time.*

## 5. AI investigations with a human in the loop

![Investigation steps](images/06-investigation-steps.png)

*Image 6. An investigation is a durable LangGraph workflow: load context, triage, plan, gather evidence, generate hypotheses, critique them, then write a report. Each step is recorded with its status and duration, and the run has hard limits on steps, tokens and cost.*

![Investigation report](images/07-investigation-report.png)

*Image 7. The report states a most likely root cause with a confidence, the impact, alternative hypotheses, containment, remediation and verification plans, and what is unknown. Claims cite the evidence they rest on.*

![Human review gate](images/08-human-review-gate.png)

*Image 8. The gate. The report says plainly that it is advisory and that OpsPilot does not execute remediation. A person can approve, request a revision with feedback, or reject.*

![A rejected investigation](images/09-investigation-rejected.png)

*Image 9. Rejection is a first-class outcome. Here a notification-queue incident arrived as a 12-alert storm, and the reviewer rejected the AI's hypothesis because the cited cause did not match what the team had found. The decision lands on the timeline.*

![A resolved incident](images/10-incident-resolved-lifecycle.png)

*Image 10. The full lifecycle of another incident: investigating, notes from the responders, mitigated, resolved, and an approved investigation recorded at the end.*

## 6. The signal pipeline

![Pipeline overview](images/11-pipeline-overview.png)

*Image 11. Monitoring tools send HMAC-signed webhooks. They are validated, published to Kafka, normalized, de-duplicated and correlated to incidents. The page shows throughput, latency percentiles, consumer lag, duplicates, failures and broker health.*

![Sources](images/12-pipeline-sources.png)

*Image 12. Sources are the webhook endpoints and read-only cloud integrations that feed the pipeline. Webhook secrets are encrypted at rest and shown only once.*

![Signals](images/13-pipeline-signals.png)

*Image 13. Every accepted signal with its outcome: alerts, a log error and a metric threshold processed, and one signal dead-lettered. Redelivered events are recognised and suppressed rather than creating duplicates.*

![Dead letters](images/14-pipeline-dead-letters.png)

*Image 14. A malformed signal ends up here with the reason, the attempt count and a safe preview of the payload, never the raw secret-bearing body. Operators can replay it once the cause is fixed.*

## 7. Security and operations

![Audit log](images/15-audit-log.png)

*Image 15. An append-only audit log. Each event carries an HMAC integrity hash, database triggers reject updates and deletes, and a verification job recomputes the hashes. Secrets and tokens are never recorded.*

![Members and roles](images/16-members-and-roles.png)

*Image 16. Workspace-scoped roles enforced on the server for every route, with invitations, and a guard that keeps a workspace from losing its last owner. API keys are scoped and stored only as hashes.*

![Services](images/17-services.png)

*Image 17. The services a workspace monitors. Everything in the application is scoped to a workspace, and a request for another tenant's resource is indistinguishable from a request for something that does not exist.*

## How it is built

- **Backend:** Python 3.12, FastAPI, SQLAlchemy and Alembic, LangGraph, RQ. Strict typing with mypy, ruff for style.
- **Data:** PostgreSQL with pgvector for the runbook and code index, Redis for the queue, rate limits and realtime fan-out, Kafka (Redpanda) for the signal pipeline.
- **Frontend:** Next.js and React with TanStack Query, strict TypeScript, zod for anything that crosses the network.
- **Identity:** OIDC with PKCE (Keycloak locally, Cognito-compatible), roles checked on the server, single-use WebSocket tickets, an integrity-checked audit log.
- **LLM controls:** versioned prompts, evaluation gates in CI, per-run limits, budgets and circuit breakers.

## What I did to make it dependable

- **Tests:** 450+ backend tests and about 200 frontend tests, plus Playwright end-to-end tests that sign in through a real OIDC provider.
- **Pipeline checks in CI:** dependency audits, container image scanning with Trivy, Dependabot, a coverage floor, and migration up/down checks.
- **Things the tests found:** a database outage was answered with HTTP 500 instead of a retryable 503; a sidebar bug hid the Sign out button on short screens; a Keycloak restart could leave the browser stuck on a "session expired" page. All fixed, each with a regression test.
- **Operations:** backup and restore scripts with a restore drill, opt-in data retention, health probes, Prometheus HTTP metrics, optional Sentry with scrubbing, a k6 load test, and a drill that stops PostgreSQL and Redis to check the API degrades and recovers.
- **Honest numbers:** one API container on my laptop held 30 requests per second within its latency budget and degraded well before 100. That is written down, with its causes still to be investigated.

## Where it stands

It runs locally with Docker Compose and in GitHub Actions. It has **not** been deployed to a cloud yet, and the demo data and the deterministic AI provider in the screenshots are exactly that. Deployment is the next step.

---

### Images to upload, in order

| # | File | Caption in one line |
| --- | --- | --- |
| 0 | `docs/architecture-diagram.png` | Architecture |
| 1 | `images/01-sign-in.png` | OIDC sign-in |
| 2 | `images/02-overview.png` | Live overview |
| 3 | `images/03-incidents.png` | Incident list |
| 4 | `images/04-incident-detail.png` | Incident detail |
| 5 | `images/05-incident-timeline.png` | Timeline |
| 6 | `images/06-investigation-steps.png` | Investigation workflow |
| 7 | `images/07-investigation-report.png` | Investigation report |
| 8 | `images/08-human-review-gate.png` | Human review gate |
| 9 | `images/09-investigation-rejected.png` | A rejected investigation |
| 10 | `images/10-incident-resolved-lifecycle.png` | Resolved incident |
| 11 | `images/11-pipeline-overview.png` | Pipeline overview |
| 12 | `images/12-pipeline-sources.png` | Sources |
| 13 | `images/13-pipeline-signals.png` | Signals |
| 14 | `images/14-pipeline-dead-letters.png` | Dead letters |
| 15 | `images/15-audit-log.png` | Audit log |
| 16 | `images/16-members-and-roles.png` | Members and roles |
| 17 | `images/17-services.png` | Services |

Regenerate the screenshots with `node apps/web/scripts/capture-screenshots.mjs` against a stack that has the demo data.
