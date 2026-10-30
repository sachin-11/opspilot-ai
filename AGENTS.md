# AGENTS.md

Repository rules for humans and coding agents working on OpsPilot AI.

- **Stay in scope.** Implement only the module you were asked for. Do not build future modules (agents, LangGraph, GitHub integration, auth, incidents, billing, Kafka, cloud deployment) while working on another.
- **Preserve module boundaries.** `apps/web` (UI) and `services/api` (API + worker) are separate; the web app talks to the API only over HTTP. Do not import across them.
- **Keep route handlers thin.** Handlers in `services/api` parse input, call a service, and shape output. Business logic and I/O live in service modules (see `health/service.py`).
- **Type everything.** Pydantic models for API/request/response shapes, strict MyPy on Python, strict TypeScript, zod for data crossing the network or environment.
- **Fail loudly.** Missing config must fail at startup. Never return a success-shaped response when a dependency is down. Never swallow exceptions silently; log the error class, never secrets or connection strings.
- **Test behavioral changes.** Add or update pytest/Vitest tests with the change. Run `scripts/check-all.ps1` before finishing.
- **Never commit secrets.** Only `.env.example` (throwaway local values) is tracked. `.env` is git-ignored.
- **No new infrastructure without a demonstrated requirement.** Do not add services, queues, databases, or microservices speculatively; record the justification in `docs/architecture.md` first.
- **Lockfiles are authoritative.** Change dependencies with `uv add` / `pnpm add` and commit `uv.lock` and `pnpm-lock.yaml`.
- **Database changes go through Alembic.** One migration per change; never edit applied migrations.
