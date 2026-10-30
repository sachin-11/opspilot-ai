import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./providers", () => ({
  useOps: () => ({
    baseUrl: "http://api.test",
    workspace: {id: "11111111-1111-4111-8111-111111111111"},
    connection: "live",
  }),
}));

import { InvestigationPanel } from "./investigation-panel";

const investigationId = "55555555-5555-4555-8555-555555555555";
const incidentId = "22222222-2222-4222-8222-222222222222";
const evidenceId = "66666666-6666-4666-8666-666666666666";
const now = "2026-10-30T10:00:00Z";
const summary = {
  id: investigationId, workspace_id: "11111111-1111-4111-8111-111111111111", incident_id: incidentId, retry_of_id: null,
  status: "awaiting_review", trigger: "manual", prompt_version: "investigation-v1.0.0", primary_provider: "fake", primary_model: "deterministic-v1",
  fallback_provider: null, fallback_model: null, title: "Checkout investigation", executive_summary: "Repeated failures were observed.",
  root_cause: "A repeated service failure pattern.", confidence: 0.72, total_input_tokens: 120, total_output_tokens: 60,
  estimated_cost_usd: 0, revision_count: 0, version: 2, started_at: now, review_requested_at: now, completed_at: null,
  failed_at: null, cancelled_at: null, failure_code: null, failure_message: null, created_at: now, updated_at: now,
};
const detail = {
  ...summary,
  steps: [{id: "77777777-7777-4777-8777-777777777777", node_name: "produce_report", display_name: "Produce report", status: "completed", attempt: 1, summary: "Report prepared", structured_output: null, error_code: null, error_message: null, input_tokens: 20, output_tokens: 20, estimated_cost_usd: 0, started_at: now, completed_at: now}],
  evidence: [{id: evidenceId, evidence_type: "ingested_event", source_type: "event", source_id: "event-1", title: "HTTP errors", excerpt: "Five HTTP 500 events", metadata: {}, content_hash: "a".repeat(64), collected_at: now}],
  reviews: [],
  report: {title: "Checkout investigation", executive_summary: "Repeated failures were observed.", root_cause: "A repeated service failure pattern.", confidence: 0.72, impact: "Checkout reliability degraded.", evidence_citations: [evidenceId], alternative_hypotheses: ["Dependency latency"], immediate_containment_steps: [{order: 1, action: "Validate rollback", rationale: "Compare error rate", evidence_ids: [evidenceId]}], remediation_plan: [], verification_plan: [], unknowns_and_assumptions: ["No external telemetry"], abstained: false},
};

function wrapper({children}: {children: ReactNode}) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}}));
}

afterEach(() => vi.unstubAllGlobals());

describe("Module 2 investigation UI", () => {
  it("starts a manual investigation from the empty state", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST" ? response({investigation_id: investigationId, status: "queued"}, 202) : response([]),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<InvestigationPanel incidentId={incidentId}/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Start investigation"}));
    expect(await screen.findByText("Investigation queued.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/investigations"), expect.objectContaining({method: "POST"}));
  });

  it("renders progress, report, evidence citations and advisory status", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => String(input).includes(`/investigations/${investigationId}`) ? response(detail) : response([summary])));
    render(<InvestigationPanel incidentId={incidentId}/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Checkout investigation"})).toBeInTheDocument();
    expect(screen.getByText("Report prepared")).toBeInTheDocument();
    expect(screen.getByText(/72% confidence/)).toBeInTheDocument();
    expect(screen.getAllByText("HTTP errors")).not.toHaveLength(0);
    expect(screen.getByText("Advisory only. OpsPilot does not execute remediation.")).toBeInTheDocument();
  });

  it("requires feedback and confirms a rejection before submitting", async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return response({investigation_id: investigationId, status: "running", version: 3}, 202);
      return String(input).includes(`/investigations/${investigationId}`) ? response(detail) : response([summary]);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InvestigationPanel incidentId={incidentId}/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Reject report"}));
    expect(screen.getByText("Feedback is required for revision or rejection.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Feedback"), "Evidence contradicts the conclusion.");
    await userEvent.click(screen.getByRole("button", {name: "Reject report"}));
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/review"), expect.objectContaining({method: "POST", body: expect.stringContaining('"action":"rejected"')})));
  });

  it("offers a new investigation when the latest run failed", async () => {
    const failed = {...summary, status: "failed", failure_code: "limit_exceeded", failure_message: "Cost limit exceeded"};
    const failedDetail = {...detail, ...failed, report: null};
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return response({investigation_id: "88888888-8888-4888-8888-888888888888", status: "queued"}, 202);
      return String(input).includes(`/investigations/${investigationId}`) ? response(failedDetail) : response([failed]);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<InvestigationPanel incidentId={incidentId}/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Retry as new investigation"}));
    expect(await screen.findByText("Retry queued as a new investigation.")).toBeInTheDocument();
  });
});
