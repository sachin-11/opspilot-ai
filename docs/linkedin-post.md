# LinkedIn post (copy and paste)

Attach images in this order: 0 (architecture), 8 (human review gate), 2 (overview), 7 (report), 11 (pipeline), 14 (dead letters). Put the full walkthrough in the comments or as an article (`docs/project-article.md`).

---

I spent the last few weeks building OpsPilot AI, an incident-response platform where AI investigates outages but humans stay in control.

When production breaks at 2 AM, the slow part is rarely the fix. It is finding the cause across alerts, logs, deploys, runbooks and code. OpsPilot tries to shorten that part without handing the keys to a model.

What it does:
→ Turns signed webhooks from monitoring tools (via Kafka) into de-duplicated incidents
→ Runs an AI investigation that cites its evidence, then waits for a person to approve, reject or request a revision
→ Correlates incidents with runbooks and read-only GitHub history
→ Can propose a fix and open a pull request, but only after explicit approval. It never merges or deploys.

Choices I care about:
• Multi-tenant from day one: OIDC sign-in, server-side roles on every route, scoped API keys, an integrity-checked audit log
• LLM controls: versioned prompts, evaluation gates in CI, budgets, circuit breakers
• Fail loudly: a database outage returns a retryable 503, never a fake success

Stack: FastAPI, Next.js, PostgreSQL + pgvector, Redis, Kafka, LangGraph, Keycloak, Docker.

What building it taught me about "dependable":
• My own tests found real bugs: a database outage returned HTTP 500 instead of a retryable 503, and a layout bug hid the Sign out button on short screens
• A load test showed one API container holds up at 30 req/s on my laptop and degrades before 100. I wrote that down instead of guessing
• Backups only count once you have restored one, so the repo has a restore drill

450+ backend tests, ~200 frontend tests, Playwright end-to-end tests through a real OIDC login, dependency and container scanning, and a drill that stops Postgres and Redis.

Honest status: it runs locally and in CI, and it is not deployed to a cloud yet. The screenshots use seeded demo data and a deterministic AI provider. Deployment is next.

Code and the full walkthrough: [GitHub link]

#SoftwareEngineering #AI #SRE #FastAPI #NextJS #LLM #DevOps #BuildInPublic
