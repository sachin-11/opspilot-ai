"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchReadiness, type ReadinessResult } from "@/lib/readiness";
import { StatusCard, type ServiceStatus } from "./status-card";

const POLL_INTERVAL_MS = 10_000;

type ViewState = { phase: "loading" } | { phase: "loaded"; result: ReadinessResult };

type Summary = { title: string; detail: string; tone: string };

function summarise(state: ViewState): Summary {
  if (state.phase === "loading") {
    return {
      title: "Checking services…",
      detail: "Contacting the OpsPilot API.",
      tone: "border-line text-muted",
    };
  }
  switch (state.result.kind) {
    case "ready":
      return {
        title: "All systems ready",
        detail: "The API, PostgreSQL and Redis are responding.",
        tone: "border-ok/60 text-ok",
      };
    case "degraded":
      return {
        title: "Degraded",
        detail: "The API is up, but a required dependency is unavailable.",
        tone: "border-bad/70 text-bad",
      };
    case "unreachable":
      return {
        title: "Backend unreachable",
        detail: `The API could not be reached. ${state.result.reason}.`,
        tone: "border-warn/60 text-warn",
      };
  }
}

function statuses(state: ViewState): { api: ServiceStatus; postgres: ServiceStatus; redis: ServiceStatus } {
  if (state.phase === "loading") {
    return { api: "checking", postgres: "checking", redis: "checking" };
  }
  const { result } = state;
  if (result.kind === "unreachable") {
    return { api: "down", postgres: "unknown", redis: "unknown" };
  }
  const { dependencies } = result.readiness;
  return {
    api: "operational",
    postgres: dependencies.postgres === "up" ? "operational" : "down",
    redis: dependencies.redis === "up" ? "operational" : "down",
  };
}

export function StatusDashboard({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [state, setState] = useState<ViewState>({ phase: "loading" });

  // Resolves to null only when the request was aborted (component unmounting).
  const load = useCallback(
    async (signal?: AbortSignal): Promise<ViewState | null> => {
      try {
        return { phase: "loaded", result: await fetchReadiness(apiBaseUrl, signal) };
      } catch {
        return null;
      }
    },
    [apiBaseUrl],
  );

  const refresh = useCallback(
    (signal?: AbortSignal) => {
      void load(signal).then((next) => {
        if (next) setState(next);
      });
    },
    [load],
  );

  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal);
    const timer = setInterval(() => refresh(controller.signal), POLL_INTERVAL_MS);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
  }, [refresh]);

  const summary = summarise(state);
  const current = statuses(state);

  return (
    <section aria-labelledby="status-heading" className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="status-heading" className="font-mono text-xs uppercase tracking-[0.2em] text-muted">
          System status
        </h2>
        <button
          type="button"
          onClick={() => refresh()}
          className="rounded-sm border border-line px-3 py-1.5 font-mono text-xs uppercase tracking-wider text-ink transition hover:border-accent hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Refresh
        </button>
      </div>

      <div role="status" aria-live="polite" className={`rounded-md border bg-panel p-5 ${summary.tone}`}>
        <p className="text-lg font-semibold">{summary.title}</p>
        <p className="mt-1 text-sm text-muted">{summary.detail}</p>
      </div>

      <ul className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatusCard name="API" role="FastAPI service" status={current.api} />
        <StatusCard name="PostgreSQL" role="Primary database with pgvector" status={current.postgres} />
        <StatusCard name="Redis" role="Queue and cache" status={current.redis} />
      </ul>
    </section>
  );
}
