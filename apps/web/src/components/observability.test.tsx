import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({useRouter: () => ({push}), usePathname: () => "/observability"}));
vi.mock("./providers", () => ({
  useOps: () => ({
    baseUrl: "http://api.test",
    workspace: {id: "11111111-1111-4111-8111-111111111111", name: "Demo", slug: "demo"},
    workspaces: [], connection: "live",
  }),
}));

import { IncidentObservabilityPanel } from "./incident-observability-panel";
import { DeadLetterList } from "./observability-dead-letters";
import { PipelineOverview } from "./observability-overview";
import { SourceCreateForm } from "./observability-source-form";
import { SignalExplorer } from "./observability-signals";
import { SourceList } from "./observability-sources";

const ws = "11111111-1111-4111-8111-111111111111";
const svcId = "66666666-6666-4666-8666-666666666666";
const srcId = "22222222-2222-4222-8222-222222222222";
const sigId = "33333333-3333-4333-8333-333333333333";
const sigId2 = "44444444-4444-4444-8444-444444444444";
const incId = "88888888-8888-4888-8888-888888888888";
const dlId = "99999999-9999-4999-8999-999999999999";
const now = "2026-10-30T10:00:00Z";
const SECRET = "whsec_super_secret_value_123";

const service = {id: svcId, workspace_id: ws, name: "Checkout", slug: "checkout", description: null, environment: "production", created_at: now, updated_at: now};
const source = {id: srcId, workspace_id: ws, service_id: svcId, service_name: "Checkout", public_id: "pub1", name: "Prod webhook", provider: "generic_webhook", source_type: "webhook", status: "active", environment: "production", region: null, configuration: {}, has_secret: true, webhook_path: "/api/v1/webhooks/observability/pub1", last_event_at: now, last_success_at: now, last_failure_at: null, created_at: now, updated_at: now};
const signal = {signal_id: sigId, source_id: srcId, source_name: "Prod webhook", provider: "generic_webhook", service_id: svcId, service_name: "Checkout", signal_type: "alert", severity: "sev2", title: "High error rate", status: "processed", received_at: now, occurred_at: now, processed_at: now, incident_id: incId, ingest_result: "created", deduplication_key: "dk1", duplicate_of: null, attempt_count: 1, failure_code: null};
const dupSignal = {...signal, signal_id: sigId2, status: "duplicate", duplicate_of: sigId, title: "Repeat alert", signal_type: "alert-dup", incident_id: null};
const signalDetail = {...signal, provider_event_id: "evt-1", payload_hash: "abc123", schema_version: "1", failure_message: null, normalized: {title: "High error rate", labels: {team: "pay"}}};
const pipeline = {window_minutes: 60, generated_at: now, accepted: 100, processed: 90, duplicates: 7, failures: 3, dead_letters_open: 2, events_per_second: 1.5, latency_p50_ms: 120, latency_p95_ms: 2400, incidents_created: 4, events_correlated: 30, consumer_lag: 12, lag_available: true, broker_healthy: true, broker_backend: "redpanda", backpressure: {active: false, reasons: []}, sources: [{id: srcId, name: "Prod webhook", provider: "generic_webhook", status: "active", last_event_at: now, last_success_at: now, last_failure_at: null}]};
const deadLetter = {id: dlId, workspace_id: ws, source_id: srcId, source_name: "Prod webhook", signal_id: sigId, original_topic: "signals.raw", partition: 0, offset: 5, failure_code: "normalization_failed", failure_class: "retryable_exhausted", failure_message: "Schema mismatch", attempt_count: 5, first_failed_at: now, last_failed_at: now, replayed_at: null, resolved_at: null, replay_count: 0, replayable: true, payload_preview: {title: "preview-value"}};
const incidentObs = {incident_id: incId, has_source: true, source_id: srcId, source_name: "Prod CW", provider: "aws_cloudwatch", signals: [signal], logs: [{timestamp: now, message: "ERROR timeout calling db", severity: "ERROR", log_group: "/app/checkout"}], metric: {metric_name: "CPUUtilization", statistic: "Average", unit: "Percent", baseline_average: 20, incident_average: 80, change_percent: 300, baseline_points: [{timestamp: now, value: 20}], incident_points: [{timestamp: now, value: 80}]}, alarm: {alarm_name: "checkout-cpu-high", state: "ALARM", metric_name: "CPUUtilization", threshold: 70, comparison: ">", period_seconds: 60, transitions: [{at: now, from_state: "OK", to_state: "ALARM", reason: "Threshold crossed"}]}, unavailable: null, notice: "Evidence is bounded."};

let client: QueryClient;
function wrapper({children}: {children: ReactNode}) { return <QueryClientProvider client={client}>{children}</QueryClientProvider>; }
function response(body: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}})); }
type Routes = Record<string, (init?: RequestInit, url?: string) => Promise<Response>>;
function route(routes: Routes) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const full = String(input).replace("http://api.test/api/v1/workspaces/" + ws, "");
    const key = `${init?.method ?? "GET"} ${full.split("?")[0]}`;
    const handler = routes[key];
    if (!handler) return response({error: {code: "not_found", message: `unrouted ${key}`}}, 404);
    return handler(init, full);
  });
}
const base: Routes = {
  "GET /services": () => response([service]), "GET /observability/sources": () => response([source]),
  "GET /observability/pipeline": () => response(pipeline),
  "GET /observability/signals": () => response({items: [signal, dupSignal], next_cursor: null}),
  [`GET /observability/signals/${sigId}`]: () => response(signalDetail),
  "GET /observability/dead-letters": () => response({items: [deadLetter], next_cursor: null}),
};

beforeEach(() => { client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}}); vi.spyOn(window, "confirm").mockReturnValue(true); push.mockClear(); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); window.localStorage.clear(); window.sessionStorage.clear(); });

function cacheContains(text: string): boolean {
  const queries = JSON.stringify(client.getQueryCache().getAll().map((q) => q.state.data));
  const mutations = JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.data));
  return queries.includes(text) || mutations.includes(text);
}

describe("pipeline overview", () => {
  it("shows metrics, source health, broker health and lag", async () => {
    vi.stubGlobal("fetch", route(base));
    render(<PipelineOverview/>, {wrapper});
    expect(await screen.findByText("1.50/s")).toBeInTheDocument();
    expect(screen.getByText("120 ms")).toBeInTheDocument();
    expect(screen.getByText("2.40 s")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("Healthy")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Source health")).getByText("Prod webhook")).toBeInTheDocument();
    expect(screen.queryByText("Backpressure active")).toBeNull();
  });

  it("says lag is unavailable instead of inventing a number", async () => {
    vi.stubGlobal("fetch", route({...base, "GET /observability/pipeline": () => response({...pipeline, consumer_lag: null, lag_available: false})}));
    render(<PipelineOverview/>, {wrapper});
    expect(await screen.findByText("Lag unavailable")).toBeInTheDocument();
  });

  it("shows a prominent backpressure warning with reasons", async () => {
    vi.stubGlobal("fetch", route({...base, "GET /observability/pipeline": () => response({...pipeline, broker_healthy: false, backpressure: {active: true, reasons: ["consumer lag above threshold", "queue depth high"]}})}));
    render(<PipelineOverview/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Backpressure active"})).toBeInTheDocument();
    expect(screen.getByText("consumer lag above threshold")).toBeInTheDocument();
    expect(screen.getByText("queue depth high")).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "Broker unhealthy"})).toBeInTheDocument();
  });

  it("shows loading and error states", async () => {
    vi.stubGlobal("fetch", route({...base, "GET /observability/pipeline": () => response({error: {code: "x", message: "boom"}}, 500)}));
    render(<PipelineOverview/>, {wrapper});
    expect(screen.getByText("Loading pipeline status…")).toBeInTheDocument();
    expect(await screen.findByText(/Pipeline status could not be loaded/)).toBeInTheDocument();
  });

  it("generates demo signals and handles the disabled case", async () => {
    const generate = vi.fn(() => response({source_id: srcId, accepted: 18, duplicates: 2, rejected: 0}));
    vi.stubGlobal("fetch", route({...base, "POST /observability/demo/generate": generate}));
    render(<PipelineOverview/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Generate demo signals"}));
    expect(await screen.findByText(/18 accepted, 2 duplicates, 0 rejected/)).toBeInTheDocument();
    vi.unstubAllGlobals();
    vi.stubGlobal("fetch", route({...base, "POST /observability/demo/generate": () => response({error: {code: "not_found", message: "Not found"}}, 404)}));
    await userEvent.click(screen.getByRole("button", {name: "Generate demo signals"}));
    expect(await screen.findByText(/Demo generation is disabled/)).toBeInTheDocument();
  });
});

describe("source creation and copy-once secret", () => {
  async function fillAndCreate() {
    await screen.findByRole("option", {name: /Checkout/});
    await userEvent.type(screen.getByLabelText("Name"), "Prod webhook");
    await userEvent.selectOptions(screen.getByLabelText("Service"), svcId);
    await userEvent.click(screen.getByRole("button", {name: "Create source"}));
  }

  it("shows the secret once after creation, copies it, and clears it on dismiss without caching or storing it", async () => {
    const created = vi.fn(() => response({...source, webhook_secret: SECRET}, 201));
    vi.stubGlobal("fetch", route({...base, "POST /observability/sources": created}));
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {configurable: true, value: {writeText}});
    render(<SourceCreateForm/>, {wrapper});
    await fillAndCreate();
    expect(await screen.findByTestId("webhook-secret")).toHaveTextContent(SECRET);
    expect(screen.getByText(/will not be shown again/)).toBeInTheDocument();
    expect(screen.getByText(/X-OpsPilot-Signature/)).toBeInTheDocument();
    expect(screen.getByText("http://api.test/api/v1/webhooks/observability/pub1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Copy secret"}));
    expect(writeText).toHaveBeenCalledWith(SECRET);
    expect(cacheContains(SECRET)).toBe(false);
    expect(JSON.stringify({...window.localStorage}) + JSON.stringify({...window.sessionStorage})).not.toContain(SECRET);
    expect(window.location.href).not.toContain(SECRET);
    await userEvent.click(screen.getByRole("button", {name: /dismiss/}));
    expect(screen.queryByText(SECRET)).toBeNull();
    expect(push).toHaveBeenCalledWith("/observability/sources");
  });

  it("does not render the secret when the source list is refetched", async () => {
    vi.stubGlobal("fetch", route(base));
    render(<SourceList/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Prod webhook"})).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET);
    expect(screen.queryByTestId("webhook-secret")).toBeNull();
  });

  it("creates a CloudWatch source with no credential inputs and redirects", async () => {
    const created = vi.fn((init?: RequestInit) => { void init; return response({...source, provider: "aws_cloudwatch", webhook_path: null, webhook_secret: null}, 201); });
    vi.stubGlobal("fetch", route({...base, "POST /observability/sources": created}));
    render(<SourceCreateForm/>, {wrapper});
    await screen.findByRole("option", {name: /Checkout/});
    await userEvent.click(screen.getByLabelText("AWS CloudWatch (read-only)"));
    expect(screen.getByText(/Never paste AWS access keys/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/access key|secret key/i)).toBeNull();
    await userEvent.type(screen.getByLabelText("Name"), "Prod CW");
    await userEvent.selectOptions(screen.getByLabelText("Service"), svcId);
    await userEvent.type(screen.getByLabelText("IAM role ARN"), "arn:aws:iam::123456789012:role/opspilot-readonly");
    await userEvent.type(screen.getByLabelText(/Allowed log groups/), "/app/checkout");
    await userEvent.click(screen.getByRole("button", {name: "Create source"}));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/observability/sources"));
    const call = created.mock.calls[0]?.[0];
    const body = JSON.parse(String(call?.body)) as {provider: string; configuration: Record<string, unknown>};
    expect(body.provider).toBe("aws_cloudwatch");
    expect(body.configuration.role_arn).toBe("arn:aws:iam::123456789012:role/opspilot-readonly");
    expect(body.configuration.allowed_log_groups).toEqual(["/app/checkout"]);
  });

  it("validates the role ARN and shows API errors", async () => {
    vi.stubGlobal("fetch", route({...base, "POST /observability/sources": () => response({error: {code: "conflict", message: "Name already used"}}, 409)}));
    render(<SourceCreateForm/>, {wrapper});
    await screen.findByRole("option", {name: /Checkout/});
    await userEvent.click(screen.getByLabelText("AWS CloudWatch (read-only)"));
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.selectOptions(screen.getByLabelText("Service"), svcId);
    await userEvent.type(screen.getByLabelText("IAM role ARN"), "nope");
    await userEvent.click(screen.getByRole("button", {name: "Create source"}));
    expect(await screen.findByText(/valid IAM role ARN/)).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Generic webhook"));
    await userEvent.click(screen.getByRole("button", {name: "Create source"}));
    expect(await screen.findByText("Name already used")).toBeInTheDocument();
  });
});

describe("source management", () => {
  it("rotates a secret after confirmation and shows it once", async () => {
    const rotate = vi.fn(() => response({source_id: srcId, webhook_secret: SECRET, rotated_at: now}));
    vi.stubGlobal("fetch", route({...base, [`POST /observability/sources/${srcId}/rotate-secret`]: rotate}));
    render(<SourceList/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Rotate secret"}));
    expect(window.confirm).toHaveBeenCalled();
    expect(await screen.findByTestId("webhook-secret")).toHaveTextContent(SECRET);
    expect(cacheContains(SECRET)).toBe(false);
    await userEvent.click(screen.getByRole("button", {name: /dismiss/}));
    expect(screen.queryByText(SECRET)).toBeNull();
  });

  it("does not rotate when confirmation is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const rotate = vi.fn(() => response({source_id: srcId, webhook_secret: SECRET, rotated_at: now}));
    vi.stubGlobal("fetch", route({...base, [`POST /observability/sources/${srcId}/rotate-secret`]: rotate}));
    render(<SourceList/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Rotate secret"}));
    expect(rotate).not.toHaveBeenCalled();
    expect(screen.queryByTestId("webhook-secret")).toBeNull();
  });

  it("toggles status through PATCH", async () => {
    const patch = vi.fn((init?: RequestInit) => { void init; return response({...source, status: "disabled"}); });
    vi.stubGlobal("fetch", route({...base, [`PATCH /observability/sources/${srcId}`]: patch}));
    render(<SourceList/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Disable"}));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(JSON.parse(String(patch.mock.calls[0]?.[0]?.body))).toEqual({status: "disabled"});
  });

  it.each([["ok", "Connection OK"], ["throttled", "Throttled by provider"], ["auth_failed", "Authentication failed"], ["unavailable", "Provider unavailable"], ["misconfigured", "Misconfigured"]])("shows %s connectivity result", async (status, label) => {
    vi.stubGlobal("fetch", route({...base, [`POST /observability/sources/${srcId}/test`]: () => response({ok: status === "ok", status, message: "detail text", latency_ms: 42})}));
    render(<SourceList/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Test connection"}));
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.getByText(/detail text/)).toBeInTheDocument();
  });

  it("shows failing sources, empty state, and list failure", async () => {
    vi.stubGlobal("fetch", route({...base, "GET /observability/sources": () => response([{...source, status: "failing", last_failure_at: now}])}));
    const {unmount} = render(<SourceList/>, {wrapper});
    expect(await screen.findByText(/This source is failing/)).toBeInTheDocument();
    unmount();
    client.clear();
    vi.stubGlobal("fetch", route({...base, "GET /observability/sources": () => response([])}));
    const second = render(<SourceList/>, {wrapper});
    expect(await screen.findByText("No sources yet")).toBeInTheDocument();
    second.unmount(); client.clear();
    vi.stubGlobal("fetch", route({...base, "GET /observability/sources": () => response({error: {code: "x", message: "m"}}, 503)}));
    render(<SourceList/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Service unavailable"})).toBeInTheDocument();
  });
});

describe("signal explorer", () => {
  it("lists signals with duplicate indicator and incident link, and opens the sanitized payload", async () => {
    vi.stubGlobal("fetch", route(base));
    render(<SignalExplorer/>, {wrapper});
    expect(await screen.findByText(/duplicate of 33333333/)).toBeInTheDocument();
    expect(screen.getByRole("link", {name: /Incident 88888888/})).toHaveAttribute("href", `/incidents/${incId}`);
    await userEvent.click(screen.getByRole("button", {name: /View signal High error rate/}));
    const panel = await screen.findByRole("complementary", {name: "Signal detail"});
    expect(await within(panel).findByText(/"team": "pay"/)).toBeInTheDocument();
  });

  it("sends filters to the API and supports Load more", async () => {
    const urls: string[] = [];
    const handler = (_init?: RequestInit, url?: string) => {
      urls.push(url ?? "");
      const cursor = new URL(`http://x${url}`).searchParams.get("cursor");
      return response(cursor ? {items: [dupSignal], next_cursor: null} : {items: [signal], next_cursor: "c1"});
    };
    vi.stubGlobal("fetch", route({...base, "GET /observability/signals": handler}));
    render(<SignalExplorer/>, {wrapper});
    await screen.findByText("alert");
    await userEvent.click(screen.getByRole("button", {name: "Load more"}));
    expect(await screen.findByText("alert-dup")).toBeInTheDocument();
    expect(urls.some((u) => u.includes("cursor=c1"))).toBe(true);
    await userEvent.selectOptions(screen.getByLabelText("Status"), "duplicate");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "sev1");
    await userEvent.selectOptions(screen.getByLabelText("Time range"), "1h");
    await waitFor(() => expect(urls.some((u) => u.includes("status=duplicate") && u.includes("severity=sev1") && u.includes("from="))).toBe(true));
  });

  it("shows empty and error states", async () => {
    vi.stubGlobal("fetch", route({...base, "GET /observability/signals": () => response({items: [], next_cursor: null})}));
    const first = render(<SignalExplorer/>, {wrapper});
    expect(await screen.findByText("No matching signals")).toBeInTheDocument();
    first.unmount(); client.clear();
    vi.stubGlobal("fetch", route({...base, "GET /observability/signals": () => response({error: {code: "x", message: "m"}}, 500)}));
    render(<SignalExplorer/>, {wrapper});
    expect(await screen.findByText(/Signals could not be loaded/)).toBeInTheDocument();
  });
});

describe("dead letters", () => {
  it("lists classification, attempts and a safe preview, then replays after confirmation", async () => {
    const replay = vi.fn(() => response({status: "replayed", dead_letter_id: dlId, replayed_at: now, replay_count: 1}));
    vi.stubGlobal("fetch", route({...base, [`POST /observability/dead-letters/${dlId}/replay`]: replay}));
    render(<DeadLetterList/>, {wrapper});
    expect(await screen.findByText("retryable exhausted")).toBeInTheDocument();
    expect(screen.getByText("Schema mismatch")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText(/preview-value/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Replay"}));
    expect(await screen.findByText(/Replayed \(replay count 1\)/)).toBeInTheDocument();
  });

  it("reports already_replayed and disables non-replayable entries", async () => {
    vi.stubGlobal("fetch", route({...base, "GET /observability/dead-letters": () => response({items: [deadLetter, {...deadLetter, id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", failure_class: "non_retryable", replayable: false}], next_cursor: null}), [`POST /observability/dead-letters/${dlId}/replay`]: () => response({status: "already_replayed", dead_letter_id: dlId, replayed_at: now, replay_count: 1})}));
    render(<DeadLetterList/>, {wrapper});
    const buttons = await screen.findAllByRole("button", {name: "Replay"});
    expect(buttons[1]).toBeDisabled();
    expect(screen.getByText("non retryable")).toBeInTheDocument();
    await userEvent.click(buttons[0]!);
    expect(await screen.findByText(/Already replayed/)).toBeInTheDocument();
  });

  it("shows empty state", async () => {
    vi.stubGlobal("fetch", route({...base, "GET /observability/dead-letters": () => response({items: [], next_cursor: null})}));
    render(<DeadLetterList/>, {wrapper});
    expect(await screen.findByText("No dead letters")).toBeInTheDocument();
  });
});

describe("incident observability panel", () => {
  it("shows signals, metric comparison, logs, alarm and citation labels", async () => {
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/observability`]: () => response(incidentObs)}));
    render(<IncidentObservabilityPanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByText("High error rate")).toBeInTheDocument();
    expect(screen.getByText("+300.0%")).toBeInTheDocument();
    expect(screen.getByLabelText("Baseline average")).toBeInTheDocument();
    expect(screen.getByText(/ERROR timeout calling db/)).toBeInTheDocument();
    expect(screen.getByText("checkout-cpu-high")).toBeInTheDocument();
    expect(screen.getByText(/OK → ALARM|OK →/)).toBeInTheDocument();
    expect(screen.getByText("[Metric: CPUUtilization Average]")).toBeInTheDocument();
    expect(screen.getByText("[Alarm: checkout-cpu-high]")).toBeInTheDocument();
    expect(screen.getByText(/\[Signal: Prod webhook 33333333\]/)).toBeInTheDocument();
  });

  it("bounds log lines", async () => {
    const logs = Array.from({length: 80}, (_, i) => ({timestamp: now, message: `line-${i}`, severity: null, log_group: "g"}));
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/observability`]: () => response({...incidentObs, logs})}));
    render(<IncidentObservabilityPanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByText(/Showing the first 50 of 80 lines/)).toBeInTheDocument();
    expect(screen.queryByText(/line-60/)).toBeNull();
  });

  it("handles provider unavailable, no source, and request failure", async () => {
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/observability`]: () => response({...incidentObs, metric: null, alarm: null, logs: [], unavailable: "CloudWatch throttled the request."})}));
    const first = render(<IncidentObservabilityPanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByText(/CloudWatch throttled the request/)).toBeInTheDocument();
    first.unmount(); client.clear();
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/observability`]: () => response({...incidentObs, has_source: false, source_id: null, source_name: null, provider: null, signals: []})}));
    const second = render(<IncidentObservabilityPanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByText("No observability source")).toBeInTheDocument();
    second.unmount(); client.clear();
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/observability`]: () => response({error: {code: "x", message: "m"}}, 500)}));
    render(<IncidentObservabilityPanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByText(/Observability evidence could not be loaded/)).toBeInTheDocument();
  });
});

describe("mobile layout", () => {
  const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");

  it("keeps wide content scrollable inside its own container and collapses grids", () => {
    expect(css).toMatch(/\.payload-preview \{[^}]*max-width: 100%/);
    expect(css).toMatch(/\.payload-preview \{[^}]*overflow: auto/);
    expect(css).toMatch(/\.log-lines \{[^}]*overflow: auto/);
    expect(css).toMatch(/\.secret-value \{[^}]*overflow-wrap: anywhere/);
    expect(css).toMatch(/\.table-wrap \{ overflow-x: auto/);
    expect(css).toContain(".source-card dl");
  });

  it("renders observability screens without fixed-width inline styles at 375px", async () => {
    Object.defineProperty(window, "innerWidth", {configurable: true, value: 375});
    vi.stubGlobal("fetch", route(base));
    const {container} = render(<SignalExplorer/>, {wrapper});
    await screen.findByText(/duplicate of/);
    expect(container.querySelectorAll("[style*='width']")).toHaveLength(0);
  });
});
