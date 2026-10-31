import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
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

import { DiffViewer, parseDiff } from "./diff-viewer";
import { RemediationDetailView } from "./remediation-detail";
import { RemediationLauncher } from "./remediation-launcher";
import { RemediationList } from "./remediation-list";

const workspaceId = "11111111-1111-4111-8111-111111111111";
const remediationId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const revisionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const evidenceId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const investigationId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const incidentId = "22222222-2222-4222-8222-222222222222";
const now = "2026-10-31T10:00:00Z";
const patchHash = "f".repeat(64);
const baseSha = "a1b2c3d4".repeat(5);
const PATCH = [
  "--- a/src/calc/ops.py", "+++ b/src/calc/ops.py", "@@ -1,12 +1,12 @@",
  " line1", " line2", " line3", " line4", " line5", " line6", " line7", "-    return a - b", "+    return a + b",
  " line9", " line10", " line11", " line12",
  "--- /dev/null", "+++ b/tests/test_new.py", "@@ -0,0 +1,2 @@", "+def test_new():", "+    assert True",
].join("\n");

function remediation(overrides: Record<string, unknown> = {}) {
  return {
    id: remediationId, workspace_id: workspaceId, incident_id: incidentId, investigation_id: investigationId,
    repository_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", retry_of_id: null, base_commit_sha: baseSha, status: "awaiting_approval",
    title: "Fix add() operands", risk_level: "medium", files_changed: 2, lines_added: 3, lines_deleted: 1, patch_hash: patchHash,
    policy_version: "2026-10.1", policy_decision: "allowed_with_warnings", verification_state: "passed", revision_count: 1, version: 7,
    created_at: now, updated_at: now, completed_at: null, failed_at: null, failure_code: null, failure_message: null, stale_reason: null,
    problem_statement: "add() subtracts", proposed_solution: "Use addition", affected_components: ["src/calc"],
    generator_provider: "fake", generator_model: "deterministic-v1", generator_prompt_version: "remediation-v1",
    structured_output: {}, total_input_tokens: 321, total_output_tokens: 123, estimated_cost_usd: 0.0123,
    current_revision_id: revisionId, repository_full_name: "acme/calc", root_cause: "Wrong operator in add()",
    evidence: [{id: evidenceId, title: "ops.py add() subtracts", excerpt: "return a - b", source_type: "code_chunk"}],
    revisions: [{id: revisionId, revision_number: 1, base_commit_sha: baseSha, patch_text: PATCH, patch_hash: patchHash, change_summary: "operator fix", files_changed: 2, lines_added: 3, lines_deleted: 1, created_at: now}],
    policy_evaluations: [{id: "11111111-aaaa-4aaa-8aaa-111111111111", revision_id: revisionId, policy_version: "2026-10.1", decision: "allowed_with_warnings", risk_level: "medium", violations: [], warnings: [{code: "missing_targeted_test", message: "patch adds or updates no test"}], evaluated_at: now}],
    approvals: [], steps: [{id: "22222222-aaaa-4aaa-8aaa-222222222222", node_name: "policy_check", display_name: "Deterministic policy check", status: "completed", attempt: 1, summary: "Policy 2026-10.1: allowed", error_code: null, started_at: now, completed_at: now}],
    pull_request: null,
    approval_binding: {workspace_id: workspaceId, repository_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", base_commit_sha: baseSha, revision_id: revisionId, patch_hash: patchHash, policy_version: "2026-10.1", verification_hash: "9".repeat(64), expires_in_seconds: 3600},
    write_enabled: true, audit_events: [],
    ...overrides,
  };
}

function run(kind: string, status: string, checks: Array<[string, string, string]>) {
  return {
    id: kind === "baseline" ? "33333333-aaaa-4aaa-8aaa-333333333333" : "44444444-aaaa-4aaa-8aaa-444444444444", revision_id: revisionId, run_kind: kind, status,
    sandbox_provider: "docker", base_commit_sha: baseSha, patch_hash: patchHash, baseline_run_id: null, cpu_limit: 1, memory_limit_mb: 512, disk_limit_mb: 256,
    timeout_seconds: 120, network_enabled: false, exit_code: 0, output_truncated: false, result_hash: null, cleanup_verified: true, started_at: now,
    completed_at: now, failure_code: null, failure_message: null, created_at: now,
    checks: checks.map(([commandId, name, checkStatus], index) => ({
      id: `5555555${index}-aaaa-4aaa-8aaa-555555555555`, sandbox_run_id: "33333333-aaaa-4aaa-8aaa-333333333333", check_type: "unit_test", command_id: commandId,
      display_name: name, status: checkStatus, exit_code: 0, duration_ms: 5, stdout_excerpt: null, stderr_excerpt: null, output_truncated: false, failing_tests: [],
    })),
  };
}

function verification(overrides: Record<string, unknown> = {}) {
  return {
    remediation_id: remediationId,
    baseline: run("baseline", "failed", [["python.pytest", "pytest (full suite)", "failed"]]),
    patched: run("patched", "passed", [["patch.apply", "apply patch", "passed"], ["python.pytest", "pytest (full suite)", "passed"]]),
    comparison: {state: "passed", required_executed: true, new_failures: [], existing_failures: [], fixed: ["tests/test_ops.py::test_add"], skipped: [], notes: []},
    verification_hash: "9".repeat(64), ...overrides,
  };
}

function wrapper({children}: {children: ReactNode}) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

type Handler = (url: string, init?: RequestInit) => unknown | Promise<Response>;
function json(body: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}})); }

function mockApi(detail: Record<string, unknown>, extra?: Handler, verificationBody: unknown = verification()) {
  const calls: Array<{url: string; method: string; body: unknown}> = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined});
    const custom = extra?.(url, init);
    if (custom !== undefined) return custom as Promise<Response>;
    if (url.includes("/verification")) return json(verificationBody);
    if (url.includes(`/remediations/${remediationId}`) && (init?.method ?? "GET") === "GET") return json(detail);
    return json({remediation_id: remediationId, status: "approved", version: 8}, 202);
  });
  vi.stubGlobal("fetch", fetchMock);
  return {calls, fetchMock};
}

afterEach(() => vi.unstubAllGlobals());

describe("diff viewer", () => {
  it("parses files with line numbers and added/deleted counts", () => {
    const files = parseDiff(PATCH);
    expect(files.map((file) => [file.path, file.change, file.added, file.deleted])).toEqual([["src/calc/ops.py", "modified", 1, 1], ["tests/test_new.py", "added", 2, 0]]);
    const change = files[0]!.lines.find((line) => line.kind === "add")!;
    expect(change.newNo).toBe(8);
    expect(files[0]!.lines.find((line) => line.kind === "del")!.oldNo).toBe(8);
  });

  it("renders file-by-file with styling, collapsible context, blocked warning, redaction, and a mobile fallback", async () => {
    const secretPatch = ["--- a/.env", "+++ b/.env", "@@ -1,1 +1,2 @@", " KEEP=1", "+TOKEN=[REDACTED]"].join("\n");
    render(<DiffViewer patch={`${PATCH}\n${secretPatch}`} blocked={[{path: ".env", reasons: ["blocked_path_secrets"]}]}/>);
    expect(screen.getAllByRole("group", {name: /unified diff/i})).toHaveLength(1);
    expect(screen.getByLabelText("Diff of src/calc/ops.py")).toBeInTheDocument();
    expect(screen.getByLabelText("Diff of tests/test_new.py")).toBeInTheDocument();
    const table = screen.getByLabelText("Diff of src/calc/ops.py");
    expect(table.querySelector("tr.diff-add")).toHaveTextContent("return a + b");
    expect(table.querySelector("tr.diff-del")).toHaveTextContent("return a - b");
    const collapsed = within(table).getAllByRole("button", {name: /unchanged lines/});
    expect(collapsed.length).toBeGreaterThan(0);
    const before = table.querySelectorAll("tr").length;
    await userEvent.click(collapsed[0]!);
    expect(table.querySelectorAll("tr").length).toBeGreaterThan(before - 1);
    expect(screen.getByText("Blocked file")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("blocked_path_secrets");
    expect(document.querySelectorAll("mark.diff-redacted").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Plain diff of src/calc/ops.py")).toBeInTheDocument();
    expect(screen.getByText(/cannot be edited here/i)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("explains when no diff can be parsed", () => {
    render(<DiffViewer patch="not a diff"/>);
    expect(screen.getByRole("status")).toHaveTextContent("No parsable diff");
  });
});

describe("remediation list and lifecycle", () => {
  it("lists remediations with status badges and filters by status", async () => {
    const summaries = [remediation(), remediation({id: "99999999-9999-4999-8999-999999999999", status: "policy_blocked", title: "Blocked fix", failure_code: null})];
    const fetchMock = vi.fn((input: RequestInfo | URL) => json(summaries.filter((item) => !String(input).includes("status=policy_blocked") || item.status === "policy_blocked")));
    vi.stubGlobal("fetch", fetchMock);
    render(<RemediationList/>, {wrapper});
    expect(await screen.findByRole("link", {name: "Fix add() operands"})).toHaveAttribute("href", `/remediations/${remediationId}`);
    expect(screen.getByText("Awaiting approval", {selector: "span.status-badge"})).toBeInTheDocument();
    expect(screen.getByText("Blocked by policy", {selector: "span.status-badge"})).toBeInTheDocument();
    expect(screen.getByText(/does not merge|never merges/i, {selector: "p"})).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Status"), "policy_blocked");
    await waitFor(() => expect(screen.queryByRole("link", {name: "Fix add() operands"})).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("status=policy_blocked"), expect.anything());
  });

  it("shows an empty state", async () => {
    vi.stubGlobal("fetch", vi.fn(() => json([])));
    render(<RemediationList/>, {wrapper});
    expect(await screen.findByText("No remediations")).toBeInTheDocument();
  });
});

describe("remediation detail", () => {
  it("shows repository, base commit, evidence, policy, revisions, usage and the no-auto-merge statement", async () => {
    mockApi(remediation());
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Fix add() operands"})).toBeInTheDocument();
    expect(screen.getByText(/acme\/calc/)).toBeInTheDocument();
    expect(screen.getAllByText("Wrong operator in add()").length).toBeGreaterThan(0);
    expect(screen.getByText("ops.py add() subtracts")).toBeInTheDocument();
    expect(screen.getByText("Policy decision")).toBeInTheDocument();
    expect(screen.getAllByText("patch adds or updates no test", {exact: false}).length).toBeGreaterThan(0);
    expect(screen.getByText("Revision 1")).toBeInTheDocument();
    expect(screen.getByText("321")).toBeInTheDocument();
    expect(screen.getByText("$0.0123")).toBeInTheDocument();
    expect(screen.getAllByText(/never merges, approves, or deploys/i).length).toBeGreaterThan(0);
  });

  it("renders baseline vs patched results with fixed, existing, and new failures separated", async () => {
    const body = verification({
      baseline: run("baseline", "failed", [["python.pytest", "pytest (full suite)", "failed"]]),
      patched: run("patched", "failed", [["patch.apply", "apply patch", "passed"], ["python.pytest", "pytest (full suite)", "failed"]]),
      comparison: {state: "failed", required_executed: true, new_failures: [{command_id: "python.pytest", tests: ["tests/test_new.py::test_boom"]}], existing_failures: [{command_id: "python.pytest", tests: ["tests/test_legacy.py::test_old"]}], fixed: ["tests/test_ops.py::test_add"], skipped: [{command_id: "python.mypy", reason: "markers absent"}], notes: []},
    });
    mockApi(remediation({status: "verification_failed", verification_state: "failed"}), undefined, body);
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    const table = await screen.findByRole("table", {name: "Baseline versus patched checks"});
    expect(within(table).getByText("pytest (full suite)").closest("tr")).toHaveTextContent("failedfailed");
    expect(screen.getByText("New failures introduced by the patch")).toBeInTheDocument();
    expect(screen.getByText("tests/test_new.py::test_boom")).toBeInTheDocument();
    expect(screen.getByText("Existing failures (present before the patch)")).toBeInTheDocument();
    expect(screen.getByText("tests/test_legacy.py::test_old")).toBeInTheDocument();
    expect(screen.getByText("Fixed by the patch")).toBeInTheDocument();
    expect(screen.getByText("Checks that did not run")).toBeInTheDocument();
    expect(screen.getByText(/Verification failed\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /approve/i})).not.toBeInTheDocument();
    expect(screen.getByText(/Sandbox resource limits/)).toBeInTheDocument();
  });

  it("labels partial verification instead of calling it passed", async () => {
    const body = verification({comparison: {state: "partial", required_executed: true, new_failures: [], existing_failures: [{command_id: "python.pytest", tests: ["t::old"]}], fixed: [], skipped: [], notes: ["Failures present before the patch remain and are not caused by it."]}});
    mockApi(remediation({verification_state: "partial"}), undefined, body);
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText("Partial verification")).toBeInTheDocument();
    expect(screen.getByText(/Verification is partial/)).toBeInTheDocument();
  });

  it("shows the policy-blocked state with violations and blocked-file warning, and no review panel", async () => {
    mockApi(remediation({
      status: "policy_blocked", policy_decision: "blocked", risk_level: "prohibited",
      policy_evaluations: [{id: "11111111-aaaa-4aaa-8aaa-111111111111", revision_id: revisionId, policy_version: "2026-10.1", decision: "blocked", risk_level: "prohibited", violations: [{code: "blocked_path_ci", message: "CI/CD configuration is out of scope", path: "src/calc/ops.py"}], warnings: [], evaluated_at: now}],
      approval_binding: null,
    }), undefined, {remediation_id: remediationId, baseline: null, patched: null, comparison: null, verification_hash: null});
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText("Blocked by deterministic policy.")).toBeInTheDocument();
    expect(screen.getByText("blocked_path_ci")).toBeInTheDocument();
    expect(screen.getByText("Blocked file")).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /approve/i})).not.toBeInTheDocument();
    expect(await screen.findByText("No sandbox verification has run yet.")).toBeInTheDocument();
  });

  it("explains abstention as a legitimate outcome", async () => {
    mockApi(remediation({status: "failed", failure_code: "abstained_no_safe_patch", failure_message: "No bounded fix is identifiable.", current_revision_id: null, revisions: [], policy_evaluations: [], approval_binding: null}));
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText("No safe automated patch")).toBeInTheDocument();
    expect(screen.getByText("No bounded fix is identifiable.")).toBeInTheDocument();
  });

  it("warns about stale base commits and invalidated approvals", async () => {
    mockApi(remediation({status: "stale", stale_reason: "default branch advanced"}));
    const {unmount} = render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText("Stale base commit.")).toBeInTheDocument();
    expect(screen.getByText(/never rebases automatically/)).toBeInTheDocument();
    expect(screen.getByRole("button", {name: /Regenerate against the latest base/})).toBeInTheDocument();
    unmount();
    mockApi(remediation({status: "awaiting_approval", audit_events: [{id: "1", action: "review_approved", result: "success", actor_type: "user", actor_id: null, target_type: "revision", correlation_id: null, created_at: "2026-10-31T10:00:00Z"}, {id: "2", action: "approval_invalidated", result: "invalidated", actor_type: "system", actor_id: null, target_type: "remediation", correlation_id: null, created_at: "2026-10-31T10:05:00Z"}]}));
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText("Approval invalidated.")).toBeInTheDocument();
  });
});

describe("review interface", () => {
  async function renderReview(detail = remediation()) {
    const api = mockApi(detail);
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    await screen.findByRole("heading", {name: "Review this exact artifact"});
    return api;
  }

  it("shows the exact artifact, risk warnings, and that approval creates a branch and PR", async () => {
    await renderReview();
    expect(screen.getAllByText(patchHash).length).toBeGreaterThan(0);
    expect(screen.getAllByText(baseSha).length).toBeGreaterThan(0);
    expect(screen.getAllByText("2026-10.1").length).toBeGreaterThan(0);
    expect(screen.getByText(/Passed: every required check executed and passed/)).toBeInTheDocument();
    expect(screen.getAllByText(/2 files · \+3 \/ −1/).length).toBeGreaterThan(0);
    expect(screen.getByText("Risk warnings")).toBeInTheDocument();
    expect(screen.getByText(/Approving creates a GitHub fix branch and pull request/)).toBeInTheDocument();
    expect(screen.getAllByText(/never merges, approves, or deploys/i).length).toBeGreaterThan(1);
  });

  it("keeps approval disabled until the checkbox is checked and requires a comment", async () => {
    const {calls} = await renderReview();
    const approve = screen.getByRole("button", {name: "Approve and create pull request"});
    expect(approve).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(approve).toBeEnabled();
    await userEvent.click(approve);
    expect(await screen.findByText("A comment is required for every review action.")).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    await userEvent.click(screen.getByRole("button", {name: "Reject"}));
    expect(screen.getByText("A comment is required for every review action.")).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("submits approval bound to the exact revision and version", async () => {
    const {calls} = await renderReview();
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.type(screen.getByLabelText("Comment (required)"), "Reviewed the diff");
    await userEvent.click(screen.getByRole("button", {name: "Approve and create pull request"}));
    await waitFor(() => expect(calls.find((call) => call.method === "POST")).toBeTruthy());
    const post = calls.find((call) => call.method === "POST")!;
    expect(post.url).toContain(`/remediations/${remediationId}/review`);
    expect(post.body).toEqual({action: "approved", comment: "Reviewed the diff", revision_id: revisionId, version: 7});
  });

  it.each([["Request revision", "revision_requested"], ["Reject", "rejected"]])("submits %s with a comment", async (label, action) => {
    const {calls} = await renderReview();
    await userEvent.type(screen.getByLabelText("Comment (required)"), "Needs work");
    await userEvent.click(screen.getByRole("button", {name: label}));
    await waitFor(() => expect(calls.find((call) => call.method === "POST")).toBeTruthy());
    expect(calls.find((call) => call.method === "POST")!.body).toMatchObject({action, comment: "Needs work", revision_id: revisionId, version: 7});
  });

  it("handles a stale version or invalidated artifact by reloading and telling the reviewer", async () => {
    const {fetchMock} = mockApi(remediation(), (url, init) => init?.method === "POST" ? json({error: {code: "conflict", message: "remediation version is stale; reload and review again"}}, 409) : undefined);
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    await screen.findByRole("heading", {name: "Review this exact artifact"});
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.type(screen.getByLabelText("Comment (required)"), "ok");
    const reads = () => fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === undefined).length;
    const before = reads();
    await userEvent.click(screen.getByRole("button", {name: "Approve and create pull request"}));
    expect(await screen.findByText(/version is stale.*latest state has been loaded/i)).toBeInTheDocument();
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });
});

describe("publication", () => {
  it("blocks publication when GitHub write is disabled", async () => {
    mockApi(remediation({status: "approved", write_enabled: false, approval_binding: null}));
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText(/GitHub write access is disabled/)).toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Create branch and pull request"})).toBeDisabled();
  });

  it("queues publication for an approved remediation", async () => {
    const {calls} = mockApi(remediation({status: "approved", approval_binding: null}));
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Create branch and pull request"}));
    await waitFor(() => expect(calls.find((call) => call.method === "POST")).toBeTruthy());
    expect(calls.find((call) => call.method === "POST")!.url).toContain("/publish");
    expect(await screen.findByText("Publication queued.")).toBeInTheDocument();
  });

  it("shows publication progress without a cancel button", async () => {
    mockApi(remediation({status: "publishing", approval_binding: null}));
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText("Publishing…")).toBeInTheDocument();
    expect(screen.getByLabelText("Publication progress")).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /Cancel remediation/})).not.toBeInTheDocument();
  });

  it("links to the created pull request and repeats the no-auto-merge statement", async () => {
    mockApi(remediation({status: "pull_request_created", approval_binding: null, pull_request: {id: "66666666-aaaa-4aaa-8aaa-666666666666", remediation_id: remediationId, revision_id: revisionId, repository_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", base_commit_sha: baseSha, patch_hash: patchHash, branch_name: "opspilot/fix-aaaaaaaa-ffffffff", commit_sha: "c".repeat(40), provider_pull_request_id: "1", pull_request_number: 1007, pull_request_url: "https://github.com/acme/calc/pull/1007", status: "created", failure_code: null, failure_message: null, created_at: now, updated_at: now}}));
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    const link = await screen.findByRole("link", {name: /#1007/});
    expect(link).toHaveAttribute("href", "https://github.com/acme/calc/pull/1007");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(screen.getAllByText(/never merges, approves, or deploys/i).length).toBeGreaterThan(1);
  });

  it("shows a provider failure clearly and offers no merge action", async () => {
    mockApi(remediation({status: "approved", approval_binding: null, pull_request: {id: "66666666-aaaa-4aaa-8aaa-666666666666", remediation_id: remediationId, revision_id: revisionId, repository_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", base_commit_sha: baseSha, patch_hash: patchHash, branch_name: "opspilot/fix-aaaaaaaa-ffffffff", commit_sha: null, provider_pull_request_id: null, pull_request_number: null, pull_request_url: null, status: "failed", failure_code: "provider_unavailable", failure_message: "GitHub is unavailable (502)", created_at: now, updated_at: now}}));
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    expect(await screen.findByText("Publication failed")).toBeInTheDocument();
    expect(screen.getByText(/GitHub is unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /merge/i})).not.toBeInTheDocument();
  });

  it("reports provider outages during publish without claiming success", async () => {
    mockApi(remediation({status: "approved", approval_binding: null}), (_url, init) => init?.method === "POST" ? json({error: {code: "dependency_unavailable", message: "x"}}, 503) : undefined);
    render(<RemediationDetailView remediationId={remediationId}/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Create branch and pull request"}));
    expect(await screen.findByText(/repository provider could not be reached/)).toBeInTheDocument();
    expect(screen.queryByText("Publication queued.")).not.toBeInTheDocument();
  });
});

describe("incident launcher", () => {
  const completed = {id: investigationId, workspace_id: workspaceId, incident_id: incidentId, retry_of_id: null, status: "completed", trigger: "manual", prompt_version: "v1", primary_provider: "fake", primary_model: "m", fallback_provider: null, fallback_model: null, title: "t", executive_summary: null, root_cause: "rc", confidence: 0.9, total_input_tokens: 0, total_output_tokens: 0, estimated_cost_usd: 0, revision_count: 0, version: 3, started_at: now, review_requested_at: now, completed_at: now, failed_at: null, cancelled_at: null, failure_code: null, failure_message: null, created_at: now, updated_at: now};

  it("only appears for completed investigations and starts a remediation", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") return json({remediation_id: remediationId, status: "drafting"}, 202);
      if (url.includes("/remediations")) return json([]);
      return json([completed]);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<RemediationLauncher incidentId={incidentId}/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Propose fix"}));
    expect(await screen.findByText(/Remediation queued/)).toBeInTheDocument();
  });

  it("renders nothing without a completed investigation", async () => {
    vi.stubGlobal("fetch", vi.fn(() => json([{...completed, status: "running"}])));
    const {container} = render(<RemediationLauncher incidentId={incidentId}/>, {wrapper});
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("surfaces eligibility errors", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST" ? json({error: {code: "remediation_ineligible", message: "no repository is mapped to the affected service"}}, 422) : String(input).includes("/remediations") ? json([]) : json([completed])));
    render(<RemediationLauncher incidentId={incidentId}/>, {wrapper});
    await userEvent.click(await screen.findByRole("button", {name: "Propose fix"}));
    expect(await screen.findByText(/no repository is mapped/)).toBeInTheDocument();
  });
});
