# OpsPilot AI: incident response where AI investigates and humans decide

*A walkthrough of the project, one screen at a time. Every screenshot is from the running application with seeded demo data. The AI provider in these screenshots is the deterministic one used for local development, so the reports show how the workflow behaves, not how smart a real model is.*

## The problem

When production breaks, the slow part is rarely typing the fix. It is working out what broke: correlating alerts, logs, recent deploys, runbooks and source code while people are awake at 2 AM. Automation can help with that reading and reasoning. What nobody wants is a model with the keys to production.

OpsPilot is my attempt at the middle path. The AI does the reading and proposes an explanation with its evidence. A human approves, rejects or asks for a revision. The system can write exactly one thing to the outside world, a fix branch and pull request, and only after an explicit approval. It never merges and never deploys.

## The architecture

![Architecture](architecture-diagram.png)

*Image 0. A modular monolith: a FastAPI API and an RQ worker from one codebase, a Next.js console that talks to the API only over HTTP, PostgreSQL with pgvector, Redis, Kafka for the signal pipeline, and Keycloak (or any OIDC provider) for sign-in.*

## 1. The overview

![Overview](images/02-overview.png)

*Image 1. Active incidents by severity, the most recently updated incidents, and live system status. The page updates in real time over a WebSocket that authenticates with a single-use ticket and re-checks membership every few seconds.*

## 2. One incident, end to end

![Incident detail](images/04-incident-detail.png)

*Image 2. Severity, status, the service, and when the incident was opened, acknowledged, mitigated and resolved. Status changes use optimistic locking, so two responders cannot silently overwrite each other.*

## 3. AI investigations with a human in the loop

![Investigation steps](images/06-investigation-steps.png)

*Image 3. An investigation is a durable LangGraph workflow: load context, triage, plan, gather evidence, generate hypotheses, critique them, then write a report. Each step is recorded with its status and duration, and the run has hard limits on steps, tokens and cost.*

![Investigation report](images/07-investigation-report.png)

*Image 4. The report states a most likely root cause with a confidence, the impact, alternative hypotheses, containment, remediation and verification plans, and what is unknown. Claims cite the evidence they rest on.*

![Human review gate](images/08-human-review-gate.png)

*Image 5. The gate. The report says plainly that it is advisory and that OpsPilot does not execute remediation. A person can approve, request a revision with feedback, or reject.*

![A rejected investigation](images/09-investigation-rejected.png)

*Image 6. Rejection is a first-class outcome. Here a notification-queue incident arrived as a 12-alert storm, and the reviewer rejected the AI's hypothesis because the cited cause did not match what the team had found. The decision lands on the timeline.*

![A resolved incident](images/10-incident-resolved-lifecycle.png)

*Image 7. The full lifecycle of another incident: investigating, notes from the responders, mitigated, resolved, and an approved investigation recorded at the end.*

## 4. The signal pipeline

![Pipeline overview](images/11-pipeline-overview.png)

*Image 8. Monitoring tools send HMAC-signed webhooks. They are validated, published to Kafka, normalized, de-duplicated and correlated to incidents. The page shows throughput, latency percentiles, consumer lag, duplicates, failures and broker health.*

![Signals](images/13-pipeline-signals.png)

*Image 9. Every accepted signal with its outcome: alerts, a log error and a metric threshold processed, and one signal dead-lettered. Redelivered events are recognised and suppressed rather than creating duplicates.*

![Dead letters](images/14-pipeline-dead-letters.png)

*Image 10. A malformed signal ends up here with the reason, the attempt count and a safe preview of the payload, never the raw secret-bearing body. Operators can replay it once the cause is fixed.*

## 5. Security and operations

![Audit log](images/15-audit-log.png)

*Image 11. An append-only audit log. Each event carries an HMAC integrity hash, database triggers reject updates and deletes, and a verification job recomputes the hashes. Secrets and tokens are never recorded.*

![Members and roles](images/16-members-and-roles.png)

*Image 12. Workspace-scoped roles enforced on the server for every route, with invitations, and a guard that keeps a workspace from losing its last owner. API keys are scoped and stored only as hashes.*

## 6. Evaluation gates for the LLM

![LLMOps overview](images/18-llmops-overview.png)

*Image 13. Every change to a prompt or model is evaluated against a 104-case golden dataset before it can be published. The trend shows two baseline runs near 99% and a deliberately broken variant dropping to 39%.*

![Evaluation runs](images/19-llmops-evaluations.png)

*Image 14. Runs are started from the console and executed by the worker. Each run records the dataset version, variant, purpose and pass counts. Token and latency figures are labelled "synthetic (not measured)" because this run uses the deterministic provider.*

![A regression the gate catches](images/20-llmops-regression-blocked.png)

*Image 15. The regressing variant passes 41 of 104 cases, and the hard safety gates fail: zero tenant leaks (5 violations) and zero secret leaks (4 violations). Hard gates can never be overridden, so this prompt could not be published.*

These runs use the deterministic provider, so they test the harness, the graders and the gates, not the quality of a real model.

**A first real-model check.** A separate, capped, opt-in command sends redacted golden cases to a real model. I ran it with Claude Haiku on 10 triage cases for roughly ten US cents. It first scored **0 of 10**. The cause was not the model: my evaluation adapter forbade the model from returning a field the grader requires and never defined the label vocabulary, so those cases could not pass by construction, something the offline runs could never reveal. After fixing the adapter (with a regression test) the same cases scored **10 of 10**. That is a small sample and the prompt was adjusted after seeing those failures, so I treat it as a smoke test that proved the harness and the model now agree, not as a benchmark. Both reports and the caveats are in [real-model-eval.md](real-model-eval.md).

![Real-model evaluation](images/21-real-model-eval.png)

*Image 16. The two real-model runs side by side. The report command writes files rather than database rows, so this card is rendered from the two reports (source: `docs/real-model-eval-card.html`).*

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
| 1 | `images/02-overview.png` | Overview |
| 2 | `images/04-incident-detail.png` | Incident detail |
| 3 | `images/06-investigation-steps.png` | Investigation steps |
| 4 | `images/07-investigation-report.png` | Investigation report |
| 5 | `images/08-human-review-gate.png` | Human review gate |
| 6 | `images/09-investigation-rejected.png` | A rejected investigation |
| 7 | `images/10-incident-resolved-lifecycle.png` | A resolved incident |
| 8 | `images/11-pipeline-overview.png` | Pipeline overview |
| 9 | `images/13-pipeline-signals.png` | Signals |
| 10 | `images/14-pipeline-dead-letters.png` | Dead letters |
| 11 | `images/15-audit-log.png` | Audit log |
| 12 | `images/16-members-and-roles.png` | Members and roles |
| 13 | `images/18-llmops-overview.png` | LLMOps overview |
| 14 | `images/19-llmops-evaluations.png` | Evaluation runs |
| 15 | `images/20-llmops-regression-blocked.png` | A regression the gate catches |
| 16 | `images/21-real-model-eval.png` | Real-model evaluation |

Regenerate the screenshots with `node apps/web/scripts/capture-screenshots.mjs` against a stack that has the demo data.
