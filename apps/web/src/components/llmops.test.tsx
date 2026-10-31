import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ALL = ["llmops.read", "llmops.prompts.manage", "llmops.evals.run", "llmops.evals.label", "llmops.experiments.manage", "llmops.budgets.manage"];
const ops = vi.hoisted(() => ({
  value: {
    baseUrl: "http://api.test",
    workspace: {id: "11111111-1111-4111-8111-111111111111", name: "Acme"},
    workspaces: [{id: "11111111-1111-4111-8111-111111111111", name: "Acme"}],
    connection: "live",
    user: {id: "99999999-9999-4999-8999-999999999999", email: "me@example.test", display_name: "Me", identity_provider: "local", status: "active"},
    role: "owner",
    permissions: [] as string[],
    selectWorkspace: vi.fn(),
    refreshMemberships: vi.fn(async () => undefined),
  } as Record<string, unknown>,
}));
vi.mock("./providers", () => ({useOps: () => ops.value}));
vi.mock("next/navigation", () => ({usePathname: () => "/llmops"}));

import { setCsrfToken, setSessionExpiredHandler } from "@/lib/api";
import { diffLines, diffStats } from "@/lib/text-diff";
import { AppShell } from "./app-shell";
import { LlmopsEvalDetail } from "./llmops-eval-detail";
import { LlmopsEvaluations } from "./llmops-evaluations";
import { LlmopsExperiments } from "./llmops-experiments";
import { LlmopsOverview } from "./llmops-overview";
import { LlmopsPrompts } from "./llmops-prompts";
import { LlmopsProviders } from "./llmops-providers";
import { LlmopsUsage } from "./llmops-usage";

const WS = "11111111-1111-4111-8111-111111111111";
const PROMPT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const V1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const V2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const RUN = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const RUN_OLD = "dddddddd-0000-4ddd-8ddd-dddddddddddd";
const DATASET = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const USER = "99999999-9999-4999-8999-999999999999";
const RESULT = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const EXP = "12121212-1212-4212-8212-121212121212";
const BUDGET = "13131313-1313-4313-8313-131313131313";
const now = "2026-10-31T10:00:00Z";
const BASE = `http://api.test/api/v1/workspaces/${WS}/llmops`;

const gate = (gateName: string, kind: "hard" | "soft", status: "pass" | "fail" | "skipped", detail = "ok") => ({gate: gateName, kind, status, detail, overridden: false});
const PASSING = {config_version: "gates-v1", results: [gate("zero_tenant_leaks", "hard", "pass", "0 violation(s)"), gate("schema_validity", "soft", "pass")]};
const SOFT_FAIL = {config_version: "gates-v1", results: [gate("zero_tenant_leaks", "hard", "pass"), gate("case_regressions", "soft", "fail", "1 previously passing case(s) now fail")]};
const HARD_FAIL = {config_version: "gates-v1", results: [gate("zero_secret_leaks", "hard", "fail", "2 violation(s)"), gate("case_regressions", "soft", "fail", "1 previously passing case(s) now fail")]};

const version = (id: string, semantic: string, status: string, isDefault: boolean, system: string, extra: Record<string, unknown> = {}) => ({
  id, semantic_version: semantic, status, is_default: isDefault, change_notes: `notes ${semantic}`, content_hash: `${semantic.replace(/\./g, "")}abcdef0123456789`.padEnd(64, "0"),
  system_template: system, developer_template: null, created_by_user_id: USER, published_by_user_id: null, override_reason: null, gate_report: null,
  eval_run_id: null, created_at: semantic === "1.0.0" ? "2026-10-01T10:00:00Z" : now, published_at: null, retired_at: null, ...extra,
});
const PROMPT_SUMMARY = {id: PROMPT, name: "root-cause", workflow: "investigation", node_name: "root_cause", description: "Root cause prompt.", status: "active", default_version: "1.0.0", version_count: 2, updated_at: now};
const PROMPT_DETAIL = {...PROMPT_SUMMARY, versions: [version(V1, "1.0.0", "published", true, "You are an SRE.\nCite evidence.\nBe brief."), version(V2, "1.1.0", "candidate", false, "You are an SRE.\nCite every evidence id.\nBe brief.\nAbstain when unsure.")]};

const runSummary = (id: string, overrides: Record<string, unknown> = {}) => ({
  id, dataset_id: DATASET, dataset_version: 1, run_type: "candidate_comparison", purpose: "gate", variant: "candidate", status: "completed", repeat_count: 1,
  total_cases: 10, passed_cases: 9, failed_cases: 1, total_tokens: 4200, estimated_cost_usd: null, metrics_source: "synthetic", started_at: now, completed_at: now,
  created_at: now, error: null, ...overrides,
});
const resultOut = (overrides: Record<string, unknown> = {}) => ({
  id: RESULT, case_key: "case-017", suite: "safety", variant: "candidate", repeat_index: 0, passed: false, safety_failed: false, failure_reasons: ["missing citation e-4"],
  deterministic_scores: {schema_validity: 1, evidence_id_validity: 0}, latency_ms: 12, tokens: 300, estimated_cost_usd: null, labelled: false, ...overrides,
});
const METRICS = {pass_rate: 0.9, schema_validity_rate: 1, tool_selection_accuracy: 0.97, citation_validity_rate: 0.9, required_abstention_rate: 1, change_correlation_recall: null, retrieval_recall_at_k: null, latency_p50_ms: 12, latency_p95_ms: 40, avg_cost_usd: null, total_tokens: 4200};
const runDetail = (id: string, overrides: Record<string, unknown> = {}) => ({
  ...runSummary(id), prompt_versions: {"investigation.root_cause": V2}, routing_policy_version: null, metrics: METRICS, violations: {tenant_leaks: 0, secret_leaks: 0},
  by_suite: {safety: 0.8, retrieval: 1}, by_tag: {injection: 0.5}, flaky_cases: [], gate_report: PASSING,
  comparison: {control_variant: "baseline", control_metrics: {...METRICS, pass_rate: 0.95, latency_p95_ms: 30}, control_violations: {tenant_leaks: 0, secret_leaks: 0}, control_pass_rate_by_suite: {safety: 1}},
  failed_results: [resultOut()], grader_breakdown: {schema_validity: 1, evidence_id_validity: 0.9}, label_queue: [resultOut()], markdown_report: "# Report\n", ...overrides,
});

const usageBody = (overrides: Record<string, unknown> = {}) => ({
  group_by: "workflow", since: now, until: now, total_requests: 120, total_input_tokens: 5000, total_output_tokens: 2000, latency_p50_ms: 800, latency_p95_ms: 2400,
  fallback_rate: 0.05, error_rate: 0.02, buckets: [{key: "investigation", requests: 120, input_tokens: 5000, output_tokens: 2000, cost_usd: 0.5, cost_unavailable_requests: 0}], ...overrides,
});
const bucket = (key: string, cost: number, unpriced = 0) => ({key, requests: 10, input_tokens: 100, output_tokens: 50, cost_usd: cost, cost_unavailable_requests: unpriced});
const costsBody = (overrides: Record<string, unknown> = {}) => ({
  since: now, until: now, daily: [{day: "2026-10-30", cost_usd: 0.25, cost_unavailable_requests: 0}, {day: "2026-10-31", cost_usd: 0.75, cost_unavailable_requests: 2}],
  by_workflow: [bucket("investigation", 1)], by_model: [bucket("model-a", 1)], by_provider: [bucket("fake", 1)], by_user: [bucket("user-1", 1)],
  cost_per_investigation_usd: 0.1234, cost_per_completed_remediation_usd: null, reserved_vs_actual: {reserved_usd: 0.2, actual_usd: 0.15},
  limit_events: [{kind: "soft_limit", scope_key: "workspace:investigation", detail: "soft limit crossed", at: now}], total_cost_usd: 1, unavailable_cost_requests: 2, ...overrides,
});
const budget = (state: "ok" | "soft_warning" | "hard_limit", overrides: Record<string, unknown> = {}) => ({
  id: BUDGET, scope: "workspace", workflow: "investigation", hard_limit_usd: 10, soft_fraction: 0.8, enabled: true, spent_usd: state === "ok" ? 1 : state === "soft_warning" ? 8.5 : 10,
  reserved_usd: 0, unmetered_calls: 0, soft_threshold_usd: 8, state, updated_at: now, ...overrides,
});
const provider = (state: string, overrides: Record<string, unknown> = {}) => ({provider: "fake", model: "m1", breaker_state: state, detail: "", consecutive_failures: 0, recent_error_rate: 0.01, requests_24h: 50, errors_24h: 1, ...overrides});

const analysis = (overrides: Record<string, unknown> = {}) => ({
  control_n: 20, candidate_n: 18, minimum_sample_size: 100, sample_size_reached: false,
  metrics: [{metric: "pass_rate", guardrail: false, control_rate: 0.9, candidate_rate: 0.95, difference: 0.05, ci_low: -0.04, ci_high: 0.14, p_value: 0.31, significant: false, regressed: false, note: "interval includes zero"},
    {metric: "safety_rate", guardrail: true, control_rate: 1, candidate_rate: 0.9, difference: -0.1, ci_low: -0.2, ci_high: -0.01, p_value: 0.02, significant: true, regressed: true, note: "guardrail regressed"}],
  safety_stop: false, warnings: ["minimum sample size not reached: results are not decision-grade", "this is look #3: repeated looks inflate false positives"], promotable_by_data: false,
  promotion_note: "Promotion is a manual, audited decision.", assumptions: ["Arms are independent.", "Normal approximation used."], ...overrides,
});
const experiment = (status: string, overrides: Record<string, unknown> = {}) => ({
  id: EXP, name: "tighter-prompt", workflow: "investigation", status, control_variant: {label: "control", eval_variant: "baseline"}, candidate_variant: {label: "cand", eval_variant: "candidate"},
  allocation_percentage: 10, unit: "investigation", primary_metric: "pass_rate", guardrail_metrics: ["safety_rate"], minimum_sample_size: 100, stop_reason: null,
  started_at: null, ended_at: null, created_at: now, ...overrides,
});

type Route = {test: (url: string, method: string) => boolean; body: unknown; status?: number};
const get = (fragment: string, body: unknown, status = 200): Route => ({test: (url, method) => method === "GET" && url.includes(fragment), body, status});
const post = (fragment: string, body: unknown, status = 200): Route => ({test: (url, method) => method !== "GET" && url.includes(fragment), body, status});
type Call = {url: string; method: string; body: unknown};
function mockApi(...routes: Route[]) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), method = init?.method ?? "GET";
    calls.push({url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined});
    const route = routes.find((candidate) => candidate.test(url, method));
    return Promise.resolve(new Response(JSON.stringify(route ? route.body : {error: {code: "unmocked", message: "unmocked route"}}), {status: route ? route.status ?? 200 : 500, headers: {"Content-Type": "application/json"}}));
  }));
  return calls;
}
function renderUi(ui: ReactElement) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}
const as = (role: string, permissions: string[]) => { ops.value.role = role; ops.value.permissions = permissions; };
const ADMIN = ["llmops.read", "llmops.prompts.manage", "llmops.evals.run", "llmops.experiments.manage", "llmops.budgets.manage"];
const RESPONDER = ["llmops.read", "llmops.evals.label"];
async function pickRun() {
  const select = await screen.findByLabelText(/evaluation run/i);
  await screen.findByRole("option", {name: /dddddddd/});
  await userEvent.selectOptions(select, RUN);
}

beforeEach(() => { setCsrfToken("csrf"); setSessionExpiredHandler(() => undefined); as("owner", ALL); });
afterEach(() => vi.unstubAllGlobals());

describe("text diff", () => {
  it("marks added, removed, and unchanged lines with numbers", () => {
    const lines = diffLines("a\nb\nc", "a\nB\nc\nd");
    expect(lines.map((l) => `${l.kind}:${l.text}`)).toEqual(["same:a", "del:b", "add:B", "same:c", "add:d"]);
    expect(diffStats(lines)).toEqual({added: 2, removed: 1});
    expect(lines[0]).toMatchObject({oldNo: 1, newNo: 1});
    expect(diffLines("", "")).toEqual([]);
  });
});

describe("overview", () => {
  it("renders quality trend, synthetic labels, unavailable cost, breaker state, and budgets", async () => {
    mockApi(
      get("/eval-runs?", [runSummary(RUN), runSummary(RUN_OLD, {created_at: "2026-10-30T10:00:00Z", variant: "baseline", passed_cases: 8})]),
      get(`/eval-runs/${RUN}`, runDetail(RUN, {violations: {tenant_leaks: 0, secret_leaks: 1}})),
      get("/usage", usageBody()), get("/costs", costsBody({total_cost_usd: 0, unavailable_cost_requests: 5})),
      get("/budgets", [budget("ok")]), get("/providers/health", [provider("closed"), provider("unknown", {model: "m2", consecutive_failures: null, recent_error_rate: null})]),
    );
    renderUi(<LlmopsOverview/>);
    expect(await screen.findByTestId("trend-chart")).toBeInTheDocument();
    expect(screen.getByRole("img", {name: /pass rate per completed evaluation run/i})).toBeInTheDocument();
    expect(await screen.findAllByText(/synthetic \(not measured\)/i)).not.toHaveLength(0);
    expect(await screen.findByText("Cost unavailable")).toBeInTheDocument();
    expect(screen.getByText(/5 request\(s\) have no pricing/)).toBeInTheDocument();
    expect(await screen.findByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.getByText("Within budget")).toBeInTheDocument();
  });

  it("shows empty states and draws no chart without data", async () => {
    mockApi(get("/eval-runs?", []), get("/usage", usageBody({total_requests: 0, total_input_tokens: 0, total_output_tokens: 0, latency_p50_ms: null, latency_p95_ms: null, fallback_rate: null, error_rate: null, buckets: []})),
      get("/costs", costsBody({daily: [], total_cost_usd: 0, unavailable_cost_requests: 0})), get("/budgets", []), get("/providers/health", []));
    renderUi(<LlmopsOverview/>);
    expect(await screen.findByText("No completed evaluation runs")).toBeInTheDocument();
    expect(screen.queryByTestId("trend-chart")).not.toBeInTheDocument();
    expect(await screen.findByText("No LLM requests in this window")).toBeInTheDocument();
    expect(await screen.findByText("No budgets configured.")).toBeInTheDocument();
  });

  it("shows a loading state, then the API error code and message", async () => {
    mockApi(get("/eval-runs?", {error: {code: "llmops_unavailable", message: "database offline"}}, 503), get("/usage", usageBody()), get("/costs", costsBody()), get("/budgets", []), get("/providers/health", []));
    renderUi(<LlmopsOverview/>);
    expect(screen.getAllByRole("status").some((el) => /loading/i.test(el.textContent ?? ""))).toBe(true);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("llmops_unavailable: database offline");
    expect(alert.textContent).not.toMatch(/at \w+ \(/);
  });
});

describe("prompts", () => {
  const baseRoutes = (report: unknown, runBody: Record<string, unknown> = {}): Route[] => [
    get("/prompts?", []), get(`/prompts/${PROMPT}`, PROMPT_DETAIL), get("/prompts", [PROMPT_SUMMARY]),
    get("/eval-runs?", [runSummary(RUN)]), get(`/eval-runs/${RUN}`, runDetail(RUN, {gate_report: report, ...runBody})),
  ];

  it("lists immutable versions with hash prefixes and diffs two versions", async () => {
    mockApi(...baseRoutes(PASSING));
    renderUi(<LlmopsPrompts/>);
    const table = await screen.findByRole("table", {name: /versions of root-cause/i});
    expect(within(table).getByText("110abcdef012")).toBeInTheDocument();
    expect(within(table).getByText("100abcdef012")).toBeInTheDocument();
    expect(within(table).getByText("✓ Default")).toBeInTheDocument();
    const diff = await screen.findByRole("group", {name: /system template diff/i});
    expect(within(diff).getByText("Cite evidence.")).toBeInTheDocument();
    expect(within(diff).getByText("Abstain when unsure.")).toBeInTheDocument();
    expect(within(diff).getByText("+2 / −1")).toBeInTheDocument();
  });

  it("disables publish on a failed hard gate, explains why, and never offers an override", async () => {
    mockApi(...baseRoutes(HARD_FAIL));
    renderUi(<LlmopsPrompts/>);
    await pickRun();
    expect(await screen.findByText(/safety gates failed/i)).toHaveTextContent(/can never be overridden/i);
    expect(screen.getByRole("button", {name: /publish version/i})).toBeDisabled();
    expect(screen.queryByLabelText(/override reason/i)).not.toBeInTheDocument();
    expect(screen.getByRole("table", {name: /quality gates/i})).toHaveTextContent("Hard (safety, never overridable)");
  });

  it("lets only an owner override soft-only failures, with a reason, via a confirmation dialog", async () => {
    const calls = mockApi(post("/publish", {prompt_id: PROMPT, version_id: V2, semantic_version: "1.1.0", status: "published", is_default: true, overridden_gates: ["case_regressions"]}), ...baseRoutes(SOFT_FAIL));
    renderUi(<LlmopsPrompts/>);
    await pickRun();
    const reason = await screen.findByLabelText(/override reason \(owner-only/i);
    const publish = screen.getByRole("button", {name: /publish version/i});
    expect(publish).toBeDisabled();
    await userEvent.type(reason, "accepted known regression");
    expect(publish).toBeEnabled();
    await userEvent.click(publish);
    const dialog = await screen.findByRole("dialog", {name: /publish 1\.1\.0/i});
    expect(dialog).toHaveTextContent(/becomes the default/i);
    expect(dialog).toHaveTextContent(/despite failed soft gates/i);
    await userEvent.click(within(dialog).getByRole("button", {name: "Publish"}));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeDefined());
    const sent = calls.find((c) => c.method === "POST")!;
    expect(sent.url).toBe(`${BASE}/prompts/${PROMPT}/publish`);
    expect(sent.body).toEqual({version_id: V2, eval_run_id: RUN, override_reason: "accepted known regression"});
    expect(await screen.findByText(/published 1\.1\.0 as the default/i)).toBeInTheDocument();
  });

  it("blocks soft-gate overrides for admins", async () => {
    as("admin", ADMIN);
    mockApi(...baseRoutes(SOFT_FAIL));
    renderUi(<LlmopsPrompts/>);
    await pickRun();
    expect(await screen.findByText(/only an owner can override/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/override reason/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: /publish version/i})).toBeDisabled();
  });

  it("publishes a passing version after confirmation without an override reason", async () => {
    as("admin", ADMIN);
    const calls = mockApi(post("/publish", {prompt_id: PROMPT, version_id: V2, semantic_version: "1.1.0", status: "published", is_default: true, overridden_gates: []}), ...baseRoutes(PASSING));
    renderUi(<LlmopsPrompts/>);
    await pickRun();
    const publish = screen.getByRole("button", {name: /publish version/i});
    await waitFor(() => expect(publish).toBeEnabled());
    await userEvent.click(publish);
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", {name: "Publish"}));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({version_id: V2, eval_run_id: RUN}));
  });

  it("disables publish when the run did not evaluate the selected version", async () => {
    as("admin", ADMIN);
    mockApi(...baseRoutes(PASSING, {prompt_versions: {"investigation.root_cause": V1}}));
    renderUi(<LlmopsPrompts/>);
    await pickRun();
    expect(await screen.findByText(/did not evaluate the selected version/i)).toBeInTheDocument();
    expect(screen.getByRole("button", {name: /publish version/i})).toBeDisabled();
  });

  it("rolls back with a reason after confirmation", async () => {
    as("admin", ADMIN);
    const detail = {...PROMPT_DETAIL, versions: [version(V1, "1.0.0", "retired", false, "old"), version(V2, "1.1.0", "published", true, "new")]};
    const calls = mockApi(post("/rollback", {prompt_id: PROMPT, version_id: V1, semantic_version: "1.0.0", status: "published", is_default: true, overridden_gates: []}),
      get(`/prompts/${PROMPT}`, detail), get("/prompts", [PROMPT_SUMMARY]), get("/eval-runs?", []));
    renderUi(<LlmopsPrompts/>);
    const button = await screen.findByRole("button", {name: /roll back…/i});
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/^reason/i), "regression in prod");
    await userEvent.click(button);
    const dialog = await screen.findByRole("dialog", {name: /roll back to 1\.0\.0/i});
    expect(dialog).toHaveTextContent(/becomes the default .* immediately, replacing 1\.1\.0/i);
    await userEvent.click(within(dialog).getByRole("button", {name: "Roll back"}));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeDefined());
    expect(calls.find((c) => c.method === "POST")).toMatchObject({url: `${BASE}/prompts/${PROMPT}/rollback`, body: {target_version_id: V1, reason: "regression in prod"}});
  });

  it("responders cannot publish or roll back", async () => {
    as("responder", RESPONDER);
    mockApi(...baseRoutes(PASSING));
    renderUi(<LlmopsPrompts/>);
    await screen.findByRole("table", {name: /versions of/i});
    expect(screen.queryByRole("button", {name: /publish version/i})).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /roll back/i})).not.toBeInTheDocument();
    expect(screen.getByText(/requires the prompt manager permission/i)).toBeInTheDocument();
  });
});

describe("evaluations", () => {
  const datasets = [{id: DATASET, name: "golden", workflow: "investigation", description: "", version: 1, status: "published", content_hash: "x", case_count: 30, suite_counts: {}, split_counts: {}, published_at: now}];

  it("lists runs with synthetic tag and unavailable cost, and starts a run with the gate purpose locked for ci", async () => {
    const calls = mockApi(post("/eval-runs", runSummary(RUN, {status: "queued"}), 202), get("/eval-runs?", [runSummary(RUN)]), get("/datasets", datasets));
    renderUi(<LlmopsEvaluations/>);
    const table = await screen.findByRole("table", {name: /evaluation runs/i});
    expect(within(table).getByText("Cost unavailable")).toBeInTheDocument();
    expect(within(table).getByText(/synthetic \(not measured\)/i)).toBeInTheDocument();
    await screen.findByRole("option", {name: /golden v1/i});
    await userEvent.selectOptions(screen.getByLabelText("Run type"), "ci");
    expect(screen.getByLabelText("Purpose")).toBeDisabled();
    expect(screen.getByLabelText("Purpose")).toHaveValue("gate");
    await userEvent.clear(screen.getByLabelText(/repeats/i));
    await userEvent.type(screen.getByLabelText(/repeats/i), "3");
    await userEvent.click(screen.getByRole("button", {name: "Start run"}));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeDefined());
    expect(calls.find((c) => c.method === "POST")).toMatchObject({url: `${BASE}/eval-runs`, body: {dataset_id: DATASET, variant: "candidate", control_variant: "baseline", run_type: "ci", purpose: "gate", repeat_count: 3}});
  });

  it("hides the start form without the run permission and shows an empty state", async () => {
    as("responder", RESPONDER);
    mockApi(get("/eval-runs?", []));
    renderUi(<LlmopsEvaluations/>);
    expect(await screen.findByText("No evaluation runs")).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: "Start run"})).not.toBeInTheDocument();
  });
});

describe("evaluation report", () => {
  it("shows metrics, regressions, failed cases, grader breakdown, flaky cases, and exports files", async () => {
    mockApi(get(`/eval-runs/${RUN}`, runDetail(RUN, {repeat_count: 3, flaky_cases: ["case-099"], violations: {tenant_leaks: 0, secret_leaks: 2}})));
    const created: Blob[] = [];
    vi.stubGlobal("URL", Object.assign(URL, {createObjectURL: vi.fn((blob: Blob) => { created.push(blob); return "blob:x"; }), revokeObjectURL: vi.fn()}));
    const downloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { downloads.push(this.download); });
    renderUi(<LlmopsEvalDetail evalRunId={RUN}/>);
    const metrics = await screen.findByRole("table", {name: /metrics compared with control \(baseline\)/i});
    expect(within(metrics).getByText("Pass rate").closest("tr")).toHaveTextContent("90.0%");
    expect(within(metrics).getByText("Pass rate").closest("tr")).toHaveTextContent("Regressed");
    expect(screen.getAllByText(/synthetic \(not measured\)/i).length).toBeGreaterThan(0);
    expect(screen.getByRole("region", {name: /regression list/i})).toHaveTextContent("Pass rate is worse than control");
    const failed = screen.getByRole("table", {name: /failed cases/i});
    expect(within(failed).getByText("case-017")).toBeInTheDocument();
    expect(within(failed).getByText(/missing citation e-4/)).toBeInTheDocument();
    expect(screen.getByRole("table", {name: /safety violations/i})).toHaveTextContent("2");
    expect(screen.getByText(/evidence id validity/i)).toBeInTheDocument();
    expect(screen.getByText("case-099")).toBeInTheDocument();
    expect(screen.getByText("Avg cost per case").parentElement).toHaveTextContent("Cost unavailable");
    await userEvent.click(screen.getByRole("button", {name: /download run json/i}));
    await userEvent.click(screen.getByRole("button", {name: /download markdown report/i}));
    expect(downloads).toEqual(["eval-run-dddddddd.json", "eval-run-dddddddd.md"]);
    expect(created.map((b) => b.type)).toEqual(["application/json", "text/markdown"]);
    vi.restoreAllMocks();
  });

  it("submits a human label for a queued case", async () => {
    const calls = mockApi(post("/human-label", {id: "14141414-1414-4141-8141-141414141414", result_id: RESULT, rubric_version: "judge-rubric-v1", passed: false, notes: "missing evidence", created_at: now}), get(`/eval-runs/${RUN}`, runDetail(RUN)));
    renderUi(<LlmopsEvalDetail evalRunId={RUN}/>);
    const form = await screen.findByRole("form", {name: /label case-017/i});
    const submit = within(form).getByRole("button", {name: "Submit label"});
    expect(submit).toBeDisabled();
    for (const name of ["factual consistency", "root cause quality", "relevance", "completeness", "actionability", "appropriate uncertainty"]) {
      await userEvent.selectOptions(within(form).getByLabelText(new RegExp(name, "i")), "4");
    }
    await userEvent.click(within(form).getByLabelText("Fail"));
    await userEvent.type(within(form).getByLabelText(/notes/i), "missing evidence");
    await userEvent.click(submit);
    await waitFor(() => expect(calls.find((c) => c.method === "POST")).toBeDefined());
    expect(calls.find((c) => c.method === "POST")).toMatchObject({url: `${BASE}/eval-results/${RESULT}/human-label`, body: {rubric_version: "judge-rubric-v1", passed: false, notes: "missing evidence",
      criterion_scores: {factual_consistency: 4, root_cause_quality: 4, relevance: 4, completeness: 4, actionability: 4, appropriate_uncertainty: 4}}});
    expect(await screen.findByText(/label saved for case-017/i)).toBeInTheDocument();
  });

  it("does not offer labelling to users without the reviewer permission", async () => {
    as("admin", ADMIN);
    mockApi(get(`/eval-runs/${RUN}`, runDetail(RUN)));
    renderUi(<LlmopsEvalDetail evalRunId={RUN}/>);
    expect(await screen.findByText(/labelling requires the reviewer permission/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: "Submit label"})).not.toBeInTheDocument();
  });

  it("shows a running run without results and a loading then error state", async () => {
    mockApi(get(`/eval-runs/${RUN}`, runDetail(RUN, {status: "running", gate_report: null})));
    renderUi(<LlmopsEvalDetail evalRunId={RUN}/>);
    expect(await screen.findByText(/this run is running/i)).toBeInTheDocument();
    expect(screen.queryByRole("table", {name: /quality gates/i})).not.toBeInTheDocument();
  });
});

describe("experiments", () => {
  const routes = (status: string, overrides: Record<string, unknown> = {}) => [
    get(`/experiments/${EXP}`, {...experiment(status), assignment_counts: {control: 20, candidate: 18}, analysis: analysis(overrides)}), get("/experiments", [experiment(status)]),
  ];

  it("shows sample size, intervals, p-values, warnings, assumptions, and the manual-promotion note", async () => {
    as("responder", RESPONDER);
    mockApi(...routes("running"));
    renderUi(<LlmopsExperiments/>);
    await userEvent.click(await screen.findByRole("button", {name: /view tighter-prompt/i}));
    expect(await screen.findByText(/minimum not yet reached/i)).toBeInTheDocument();
    expect(screen.getByText(/minimum sample size not reached/i)).toBeInTheDocument();
    expect(screen.getByText(/repeated looks inflate false positives/i)).toBeInTheDocument();
    const table = screen.getByRole("table", {name: /confidence intervals/i});
    expect(within(table).getByText("0.0200")).toBeInTheDocument();
    expect(within(table).getByText("[-4.00, 14.00] pp")).toBeInTheDocument();
    expect(screen.getByText("Normal approximation used.")).toBeInTheDocument();
    expect(screen.getByText(/promotion is manual/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /stop experiment/i})).not.toBeInTheDocument();
  });

  it("starts, pauses, and stops (with reason and safety flag) and shows a safety stop", async () => {
    const calls = mockApi(post("/stop", experiment("cancelled")), post("/start", experiment("running")), post("/pause", experiment("paused")), ...routes("running", {safety_stop: true}));
    renderUi(<LlmopsExperiments/>);
    await userEvent.click(await screen.findByRole("button", {name: /view tighter-prompt/i}));
    expect(await screen.findByRole("alert")).toHaveTextContent(/safety stop/i);
    await userEvent.click(screen.getByRole("button", {name: /pause experiment/i}));
    await waitFor(() => expect(calls.some((c) => c.url === `${BASE}/experiments/${EXP}/pause` && c.method === "POST")).toBe(true));
    const stop = screen.getByRole("button", {name: /stop experiment/i});
    expect(stop).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/stop reason/i), "guardrail regressed");
    await userEvent.click(screen.getByLabelText(/safety stop \(the candidate/i));
    await userEvent.click(stop);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/stop"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/stop"))?.body).toEqual({reason: "guardrail regressed", safety: true});
  });

  it("starts a draft experiment and validates the create form", async () => {
    const calls = mockApi(post("/start", experiment("running")), post("/experiments", experiment("draft"), 201), get(`/experiments/${EXP}`, {...experiment("draft"), assignment_counts: {}, analysis: null}), get("/experiments", [experiment("draft")]));
    renderUi(<LlmopsExperiments/>);
    await userEvent.click(await screen.findByRole("button", {name: /view tighter-prompt/i}));
    await userEvent.click(await screen.findByRole("button", {name: /start experiment/i}));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/start"))).toBe(true));
    const create = screen.getByRole("button", {name: "Create experiment"});
    expect(create).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Name"), "new experiment");
    const guardrails = screen.getByRole("group", {name: /guardrail metrics/i});
    await userEvent.click(within(guardrails).getByLabelText("safety_rate"));
    expect(create).toBeDisabled();
    await userEvent.click(within(guardrails).getByLabelText("schema_validity_rate"));
    await userEvent.click(create);
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === `${BASE}/experiments`)).toBeDefined());
    expect(calls.find((c) => c.method === "POST" && c.url === `${BASE}/experiments`)?.body).toMatchObject({name: "new experiment", primary_metric: "pass_rate", guardrail_metrics: ["schema_validity_rate"], allocation_percentage: 10, minimum_sample_size: 100});
  });
});

describe("usage and budgets", () => {
  it("sends filters as a query string and flags unavailable costs", async () => {
    const calls = mockApi(get("/usage", usageBody()), get("/costs", costsBody()), get("/budgets", [budget("ok")]));
    renderUi(<LlmopsUsage/>);
    expect(await screen.findByText(/cost unavailable for 2 request\(s\)/i)).toBeInTheDocument();
    expect(screen.getByText("Cost unavailable", {selector: "strong"}) ).toBeInTheDocument();
    expect(screen.getByRole("table", {name: "Spend by user"})).toBeInTheDocument();
    expect(screen.getByText("2026-10-31 (2 unpriced)")).toBeInTheDocument();
    expect(screen.getByText(/soft limit crossed/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Days"), "7");
    await userEvent.type(screen.getByLabelText("Workflow"), "investigation");
    await userEvent.type(screen.getByLabelText("Model"), "model-a");
    await userEvent.type(screen.getByLabelText("Provider"), "fake");
    await userEvent.click(screen.getByRole("button", {name: "Apply filters"}));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/costs?days=7&workflow=investigation&model=model-a&provider=fake"))).toBe(true));
    expect(calls.some((c) => c.url.includes("/usage?days=7&group_by=workflow&workflow=investigation&model=model-a&provider=fake"))).toBe(true);
  });

  it("shows ok, soft, and hard budget states", async () => {
    mockApi(get("/usage", usageBody()), get("/costs", costsBody()), get("/budgets", [budget("ok"), budget("soft_warning", {id: "15151515-1515-4515-8515-151515151515", workflow: "remediation"}), budget("hard_limit", {id: "16161616-1616-4616-8616-161616161616", workflow: "evaluation"})]));
    renderUi(<LlmopsUsage/>);
    expect(await screen.findByText("Within budget")).toBeInTheDocument();
    expect(screen.getByText("Soft limit reached")).toBeInTheDocument();
    expect(screen.getByText(/hard limit reached: llm calls blocked/i)).toBeInTheDocument();
    expect(screen.getAllByRole("progressbar")).toHaveLength(3);
  });

  it("requires a reason when raising a limit and sends the PATCH", async () => {
    const calls = mockApi(get("/usage", usageBody()), get("/costs", costsBody({daily: []})), get("/budgets", [budget("ok")]), post("/budgets/", budget("ok", {hard_limit_usd: 20})));
    renderUi(<LlmopsUsage/>);
    await userEvent.click(await screen.findByRole("button", {name: /edit budget for investigation/i}));
    const save = screen.getByRole("button", {name: "Save budget"});
    expect(save).toBeEnabled();
    const limit = screen.getByLabelText(/hard limit/i);
    await userEvent.clear(limit);
    await userEvent.type(limit, "20");
    expect(save).toBeDisabled();
    expect(screen.getByLabelText(/reason \(required/i)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/reason/i), "quarterly increase");
    await userEvent.click(save);
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")).toBeDefined());
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({url: `${BASE}/budgets/${BUDGET}`, body: {hard_limit_usd: 20, soft_fraction: 0.8, enabled: true, reason: "quarterly increase"}});
  });

  it("hides budget editing from users without the permission", async () => {
    as("responder", RESPONDER);
    mockApi(get("/usage", usageBody()), get("/costs", costsBody()), get("/budgets", [budget("ok")]));
    renderUi(<LlmopsUsage/>);
    await screen.findByText("Within budget");
    expect(screen.queryByRole("button", {name: /edit budget/i})).not.toBeInTheDocument();
  });
});

describe("providers", () => {
  it("distinguishes unknown from healthy and explains each breaker state", async () => {
    mockApi(get("/providers/health", [provider("closed"), provider("open", {model: "m2", consecutive_failures: 5}), provider("half_open", {model: "m3"}), provider("unknown", {model: "m4", consecutive_failures: null, recent_error_rate: null, detail: "breaker store unreachable"})]));
    renderUi(<LlmopsProviders/>);
    const table = await screen.findByRole("table", {name: /provider and model health/i});
    expect(within(table).getByText("Closed")).toBeInTheDocument();
    expect(within(table).getByText("Open")).toBeInTheDocument();
    expect(within(table).getByText("Half open")).toBeInTheDocument();
    const unknown = within(table).getByText("Unknown", {selector: ".breaker-unknown"}).closest("tr")!;
    expect(unknown).toHaveClass("row-unknown");
    expect(unknown).toHaveTextContent(/do not assume the provider is healthy/i);
    expect(unknown).toHaveTextContent("breaker store unreachable");
    expect(within(table).getByText("Closed").closest("tr")).not.toHaveClass("row-unknown");
    expect(within(table).getByText("Closed").className).not.toBe(within(table).getByText("Unknown", {selector: ".breaker-unknown"}).className);
  });

  it("shows an empty state", async () => {
    mockApi(get("/providers/health", []));
    renderUi(<LlmopsProviders/>);
    expect(await screen.findByText("No providers configured")).toBeInTheDocument();
  });
});

describe("navigation and layout", () => {
  it("hides LLMOps from roles without llmops.read and shows it otherwise", () => {
    as("viewer", ["incidents.read"]);
    const {unmount} = renderUi(<AppShell><p>child</p></AppShell>);
    expect(screen.queryByRole("link", {name: "LLMOps"})).not.toBeInTheDocument();
    unmount();
    as("responder", RESPONDER);
    renderUi(<AppShell><p>child</p></AppShell>);
    expect(screen.getByRole("link", {name: "LLMOps"})).toHaveAttribute("href", "/llmops");
  });

  it("stacks on narrow screens: wrapped scroll tables, tab navigation, and labelled sections", async () => {
    mockApi(get("/providers/health", [provider("closed")]));
    const {container} = renderUi(<LlmopsProviders/>);
    await screen.findByRole("table");
    for (const table of container.querySelectorAll("table")) expect(table.closest(".table-wrap")).not.toBeNull();
    expect(container.firstElementChild).toHaveClass("page-stack", "llmops");
    expect(screen.getByRole("navigation", {name: "LLMOps sections"})).toHaveClass("llmops-tabs");
    expect(screen.getByRole("link", {name: "Overview"})).toHaveAttribute("aria-current", "page");
    const css = readFileSync("src/app/globals.css", "utf8");
    expect(css).toMatch(/\.llmops-tabs a \{[^}]*min-height: 40px/);
    expect(css).toMatch(/@media \(max-width: 820px\) \{[^@]*\.bar-row \{ grid-template-columns: minmax\(0, 1fr\) auto/);
  });
});
