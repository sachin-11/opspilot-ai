import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({useRouter: () => ({push: vi.fn()}), usePathname: () => "/repositories"}));
vi.mock("./providers", () => ({
  useOps: () => ({
    baseUrl: "http://api.test",
    workspace: {id: "11111111-1111-4111-8111-111111111111", name: "Demo", slug: "demo"},
    workspaces: [], connection: "live",
  }),
}));

import { invalidateRealtimeEvent } from "@/lib/realtime";
import { CodeEvidencePanel } from "./code-evidence-panel";
import { GitHubIntegration } from "./github-integration";
import { RepositoryDetailView } from "./repository-detail";
import { RepositoryList } from "./repository-list";
import { RepositorySearchPlayground } from "./repository-search";

const ws = "11111111-1111-4111-8111-111111111111";
const repoId = "22222222-2222-4222-8222-222222222222";
const installId = "33333333-3333-4333-8333-333333333333";
const snapId = "44444444-4444-4444-8444-444444444444";
const svcId = "66666666-6666-4666-8666-666666666666";
const mapId = "77777777-7777-4777-8777-777777777777";
const incId = "88888888-8888-4888-8888-888888888888";
const chunkId = "99999999-9999-4999-8999-999999999999";
const now = "2026-10-30T10:00:00Z";
const sha = "abcdef1234567890abcdef1234567890abcdef12";

const installation = {id: installId, workspace_id: ws, provider_installation_id: 4242, account_login: "acme-corp", account_type: "Organization", status: "active", repository_selection: "selected", permissions: {contents: "read", metadata: "read"}, rate_limit: {}, installed_at: now, suspended_at: null, removed_at: null, updated_at: now, repository_count: 3, last_synced_at: now};
const installations = {provider: "fixture", provider_configured: true, configuration_hint: null, installations: [installation]};
const snapshot = {id: snapId, commit_sha: sha, branch: "main", status: "ready", file_count: 12, chunk_count: 40, stats: {}, started_at: now, completed_at: now, failed_at: null, failure_code: null, failure_message: null, created_at: now};
const mapped = {mapping_id: mapId, service_id: svcId, service_name: "Checkout", environment: "production", branch_pattern: "main", path_prefix: "services/checkout/"};
const repo = {id: repoId, workspace_id: ws, github_installation_id: installId, provider_repository_id: 1, owner: "acme-corp", name: "shop", full_name: "acme-corp/shop", default_branch: "main", is_private: true, is_archived: false, status: "active", failure_code: null, failure_message: null, last_synced_at: now, updated_at: now, active_snapshot: snapshot, connected_services: [mapped]};
const detail = {...repo, recent_snapshots: [snapshot], commit_count: 5, deployment_count: 2};
const service = {id: svcId, workspace_id: ws, name: "Checkout", slug: "checkout", description: null, environment: "production", created_at: now, updated_at: now};
const commit = {id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", repository_id: repoId, repository_full_name: "acme-corp/shop", sha, author_name: "Dana", message: "Reduce pool size\n\nbody", committed_at: now, changed_files: 2, additions: 4, deletions: 1, provider_url: "https://example.test", files: []};
const deployment = {id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", repository_id: repoId, repository_full_name: "acme-corp/shop", provider_deployment_id: 9, commit_sha: sha, environment: "production", status: "success", description: "Release 42", deployed_at: now, completed_at: now};
const codeResult = {chunk_id: chunkId, repository_id: repoId, repository_full_name: "acme-corp/shop", snapshot_id: snapId, commit_sha: sha, branch: "main", path: "services/checkout/db.py", language: "python", symbol_name: "make_pool", symbol_type: "function", start_line: 41, end_line: 43, excerpt: "def make_pool():\n    size = 2\n    return Pool(size)", lexical_rank: 1, lexical_score: 0.4, vector_rank: 1, vector_score: 0.9, rrf_score: 0.03, rerank_score: 0.8, citation_label: "[Code: acme-corp/shop services/checkout/db.py:41-43 @abcdef1]"};
const searchResponse = {results: [codeResult], diagnostics: {variant: "hybrid_rrf_rerank", candidate_count: 20, latency_ms: 12.5, embedding_latency_ms: 2, rerank_latency_ms: 1, rerank_fallback_used: true, low_relevance: false}};
const suspects = {incident_id: incId, opened_at: now, service_id: svcId, has_repository_mapping: true, notice: "Ranked by timing and signals.", deployments: [deployment], code_excerpts: [codeResult], candidates: [{kind: "commit", repository_id: repoId, repository_full_name: "acme-corp/shop", commit_sha: sha, commit_id: commit.id, deployment_id: null, title: "Reduce pool size", score: 0.82, confidence: "high", matched_signals: ["touches db.py"], contradicting_signals: ["deployed to staging only"], minutes_before_incident: 14, changed_files: ["services/checkout/db.py"], environment: "production", citation_label: "[Commit: acme-corp/shop abcdef1]"}]};

let client: QueryClient;
function wrapper({children}: {children: ReactNode}) { return <QueryClientProvider client={client}>{children}</QueryClientProvider>; }
function response(body: unknown, status = 200) { return Promise.resolve(new Response(status === 204 ? null : JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}})); }
type Routes = Record<string, (init?: RequestInit) => Promise<Response>>;
function route(routes: Routes) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace("http://api.test/api/v1/workspaces/" + ws, "");
    const key = `${init?.method ?? "GET"} ${url.split("?")[0]}`;
    const handler = routes[key];
    if (!handler) return response({error: {code: "not_found", message: `unrouted ${key}`}}, 404);
    return handler(init);
  });
}
const base: Routes = {
  "GET /services": () => response([service]), "GET /repositories": () => response([repo]), [`GET /repositories/${repoId}`]: () => response(detail),
  [`GET /repositories/${repoId}/commits`]: () => response([commit]), [`GET /repositories/${repoId}/deployments`]: () => response([deployment]),
};

beforeEach(() => { client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}}); vi.spyOn(window, "confirm").mockReturnValue(true); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("GitHub integration page", () => {
  it("shows installations, provider mode, permissions and read-only note", async () => {
    vi.stubGlobal("fetch", route({"GET /integrations/github/installations": () => response(installations)}));
    render(<GitHubIntegration/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "acme-corp"})).toBeInTheDocument();
    expect(screen.getByText("fixture")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/Read-only access/)).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /create|commit|branch|pull request/i})).toBeNull();
  });

  it("shows configuration instructions when the provider is not configured", async () => {
    vi.stubGlobal("fetch", route({"GET /integrations/github/installations": () => response({provider: "github", provider_configured: false, configuration_hint: "Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH.", installations: []})}));
    render(<GitHubIntegration/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "GitHub is not configured"})).toBeInTheDocument();
    expect(screen.getByText(/GITHUB_APP_ID/)).toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Sync installation"})).toBeDisabled();
  });

  it("warns for suspended and removed installations", async () => {
    vi.stubGlobal("fetch", route({"GET /integrations/github/installations": () => response({...installations, installations: [{...installation, status: "suspended", suspended_at: now}, {...installation, id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", account_login: "gone", status: "removed", removed_at: now}]})}));
    render(<GitHubIntegration/>, {wrapper});
    expect(await screen.findByText(/is suspended on GitHub/)).toBeInTheDocument();
    expect(screen.getByText(/was removed from GitHub/)).toBeInTheDocument();
  });

  it("validates and queues an installation sync", async () => {
    const fetchMock = route({"GET /integrations/github/installations": () => response(installations), "POST /integrations/github/installations/sync": () => response({status: "queued"}, 202)});
    vi.stubGlobal("fetch", fetchMock);
    render(<GitHubIntegration/>, {wrapper});
    await screen.findByRole("heading", {name: "acme-corp"});
    await userEvent.type(screen.getByLabelText("Installation id"), "abc");
    await userEvent.click(screen.getByRole("button", {name: "Sync installation"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("numeric");
    await userEvent.clear(screen.getByLabelText("Installation id"));
    await userEvent.type(screen.getByLabelText("Installation id"), "4242");
    await userEvent.click(screen.getByRole("button", {name: "Sync installation"}));
    expect(await screen.findByText(/sync queued/i)).toBeInTheDocument();
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({provider_installation_id: 4242});
  });

  it("renders a provider-unavailable state for 503", async () => {
    vi.stubGlobal("fetch", route({"GET /integrations/github/installations": () => response({error: {code: "provider_unavailable", message: "down", correlation_id: "x"}}, 503)}));
    render(<GitHubIntegration/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Provider unavailable"})).toBeInTheDocument();
  });
});

describe("repository list and detail", () => {
  it("lists repositories with privacy, branch, sha, counts and services, and filters by status", async () => {
    const fetchMock = route(base);
    vi.stubGlobal("fetch", fetchMock);
    render(<RepositoryList/>, {wrapper});
    expect(await screen.findByRole("link", {name: "acme-corp/shop"})).toHaveAttribute("href", `/repositories/${repoId}`);
    expect(screen.getByText(/Private/)).toBeInTheDocument();
    expect(screen.getByText("abcdef1")).toBeInTheDocument();
    expect(screen.getByText("12 files · 40 chunks")).toBeInTheDocument();
    expect(screen.getByText("Services").nextElementSibling).toHaveTextContent("Checkout");
    await userEvent.selectOptions(screen.getByLabelText("Sync status"), "failed");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("status=failed"), expect.anything()));
  });

  it("shows detail with commits, deployments, snapshots and failure details without stack traces", async () => {
    vi.stubGlobal("fetch", route({...base, [`GET /repositories/${repoId}`]: () => response({...detail, status: "failed", failure_code: "github_rate_limited", failure_message: "Rate limit reached"})}));
    render(<RepositoryDetailView repositoryId={repoId}/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "acme-corp/shop"})).toBeInTheDocument();
    expect(await screen.findByText("Reduce pool size")).toBeInTheDocument();
    expect(screen.getByText(/Release 42/)).toBeInTheDocument();
    expect(screen.getByText(/github_rate_limited: Rate limit reached/)).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "Snapshot history"})).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /create (pr|pull|commit|branch)/i})).toBeNull();
  });

  it("queues sync and reindex with pending protection and error state", async () => {
    let release: (value: Response) => void = () => undefined;
    let reindexCalls = 0;
    const fetchMock = route({...base,
      [`POST /repositories/${repoId}/sync`]: () => new Promise<Response>((resolve) => { release = resolve; }),
      [`POST /repositories/${repoId}/reindex`]: () => { reindexCalls += 1; return response({error: {code: "x", message: "Reindex already running"}}, 409); },
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<RepositoryDetailView repositoryId={repoId}/>, {wrapper});
    await screen.findByRole("heading", {name: "acme-corp/shop"});
    await userEvent.click(screen.getByRole("button", {name: "Sync"}));
    expect(await screen.findByRole("button", {name: "Queuing sync…"})).toBeDisabled();
    expect(screen.getByRole("button", {name: "Reindex"})).toBeDisabled();
    await userEvent.click(screen.getByRole("button", {name: "Queuing sync…"}));
    release(await response({status: "queued"}, 202));
    expect(await screen.findByText("Sync queued.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Reindex"}));
    expect(await screen.findByText("Reindex already running")).toBeInTheDocument();
    expect(reindexCalls).toBe(1);
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST" && String(call[0]).endsWith("/sync"))).toHaveLength(1);
  });

  it("creates a mapping with a monorepo path prefix and removes one after confirmation", async () => {
    let created: unknown = null; let deleted = false;
    const fetchMock = route({...base,
      [`POST /services/${svcId}/repositories`]: (init) => { created = JSON.parse(String(init?.body)); return response({id: mapId, workspace_id: ws, service_id: svcId, repository_id: repoId, repository_full_name: "acme-corp/shop", environment: "staging", branch_pattern: "release/*", path_prefix: "services/api/", created_at: now, updated_at: now}, 201); },
      [`DELETE /services/${svcId}/repositories/${mapId}`]: () => { deleted = true; return response(null, 204); },
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<RepositoryDetailView repositoryId={repoId}/>, {wrapper});
    await screen.findByText("services/checkout/");
    await userEvent.selectOptions(await screen.findByLabelText("Service"), svcId);
    await userEvent.selectOptions(screen.getByLabelText("Environment"), "staging");
    await userEvent.clear(screen.getByLabelText("Branch pattern"));
    await userEvent.type(screen.getByLabelText("Branch pattern"), "release/*");
    await userEvent.type(screen.getByLabelText("Monorepo path prefix (optional)"), "../etc");
    await userEvent.click(screen.getByRole("button", {name: "Create mapping"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("relative repository path");
    await userEvent.clear(screen.getByLabelText("Monorepo path prefix (optional)"));
    await userEvent.type(screen.getByLabelText("Monorepo path prefix (optional)"), "services/api/");
    await userEvent.click(screen.getByRole("button", {name: "Create mapping"}));
    await waitFor(() => expect(created).toEqual({repository_id: repoId, environment: "staging", branch_pattern: "release/*", path_prefix: "services/api/"}));
    await userEvent.click(screen.getByRole("button", {name: "Remove mapping for Checkout"}));
    await waitFor(() => expect(deleted).toBe(true));
    expect(window.confirm).toHaveBeenCalledTimes(1);
  });

  it("updates live indexing progress from WebSocket events", async () => {
    vi.stubGlobal("fetch", route(base));
    render(<RepositoryDetailView repositoryId={repoId}/>, {wrapper});
    await screen.findByRole("heading", {name: "acme-corp/shop"});
    expect(screen.queryByRole("status", {name: "Live indexing progress"})).toBeNull();
    await act(async () => {
      await invalidateRealtimeEvent(client, ws, {event_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd", type: "repository.indexing_progress", workspace_id: ws, incident_id: null, occurred_at: now, data: {repository_id: repoId, stage: "embedding", files_processed: 6, total_files: 12, chunks_processed: 20}});
    });
    const progress = await screen.findByRole("status", {name: "Live indexing progress"});
    expect(progress).toHaveTextContent("Indexing: embedding");
    expect(within(progress).getByText(/6\/12 files · 20 chunks/)).toBeInTheDocument();
    await act(async () => {
      await invalidateRealtimeEvent(client, ws, {event_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd", type: "repository.indexing_completed", workspace_id: ws, incident_id: null, occurred_at: now, data: {repository_id: repoId}});
    });
    await waitFor(() => expect(screen.queryByRole("status", {name: "Live indexing progress"})).toBeNull());
  });

  it("shows a provider-unavailable state on repository 502", async () => {
    vi.stubGlobal("fetch", route({...base, [`GET /repositories/${repoId}`]: () => response({error: {code: "bad_gateway", message: "upstream", correlation_id: "x"}}, 502)}));
    render(<RepositoryDetailView repositoryId={repoId}/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Provider unavailable"})).toBeInTheDocument();
  });
});

describe("code search playground", () => {
  it("renders citations, real line numbers, scores, and reranker fallback", async () => {
    const fetchMock = route({...base, "POST /repositories/search": () => response(searchResponse)});
    vi.stubGlobal("fetch", fetchMock);
    render(<RepositorySearchPlayground/>, {wrapper});
    await userEvent.type(screen.getByLabelText("Query"), "checkout pool");
    await userEvent.selectOptions(await screen.findByLabelText("Service"), svcId);
    await userEvent.type(screen.getByLabelText("Path prefix"), "services/checkout/");
    await userEvent.selectOptions(screen.getByLabelText("Variant"), "hybrid_rrf_rerank");
    await userEvent.click(screen.getByRole("button", {name: "Search code"}));
    expect(await screen.findByText(codeResult.citation_label)).toBeInTheDocument();
    expect(screen.getByText("services/checkout/db.py:41-43")).toBeInTheDocument();
    expect(screen.getByText("41")).toBeInTheDocument();
    expect(screen.getByText("43")).toBeInTheDocument();
    expect(screen.getByText("Fallback to RRF")).toBeInTheDocument();
    expect(screen.getByText("0.0300")).toBeInTheDocument();
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({query: "checkout pool", service_id: svcId, path_prefix: "services/checkout/", variant: "hybrid_rrf_rerank", top_k: 5});
  });

  it("shows empty and low-relevance states and errors", async () => {
    const diagnostics = {variant: "hybrid_rrf", candidate_count: 0, latency_ms: 3, embedding_latency_ms: null, rerank_latency_ms: null, rerank_fallback_used: false, low_relevance: true};
    vi.stubGlobal("fetch", route({...base, "POST /repositories/search": () => response({results: [], diagnostics})}));
    render(<RepositorySearchPlayground/>, {wrapper});
    await userEvent.click(screen.getByRole("button", {name: "Search code"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a search query");
    await userEvent.type(screen.getByLabelText("Query"), "nothing");
    await userEvent.click(screen.getByRole("button", {name: "Search code"}));
    expect(await screen.findByRole("heading", {name: "Low relevance"})).toBeInTheDocument();
    expect(screen.getByRole("heading", {name: "No code results"})).toBeInTheDocument();
  });

  it("shows provider failure for search", async () => {
    vi.stubGlobal("fetch", route({...base, "POST /repositories/search": () => response({error: {code: "x", message: "down"}}, 503)}));
    render(<RepositorySearchPlayground/>, {wrapper});
    await userEvent.type(screen.getByLabelText("Query"), "anything");
    await userEvent.click(screen.getByRole("button", {name: "Search code"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Provider unavailable");
  });
});

describe("incident code evidence panel", () => {
  it("shows ranked suspects, signals, excerpts, and the causation warning", async () => {
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/suspect-changes`]: () => response(suspects)}));
    render(<CodeEvidencePanel incidentId={incId}/>, {wrapper});
    expect(screen.getByText(/Correlation does not prove causation/)).toBeInTheDocument();
    expect(await screen.findByText("Reduce pool size")).toBeInTheDocument();
    expect(screen.getByText("high confidence")).toBeInTheDocument();
    expect(screen.getByText("touches db.py")).toBeInTheDocument();
    expect(screen.getByText("deployed to staging only")).toBeInTheDocument();
    expect(screen.getByText(/14 min before incident/)).toBeInTheDocument();
    expect(screen.getByText("[Commit: acme-corp/shop abcdef1]")).toBeInTheDocument();
    expect(screen.getByText(codeResult.citation_label)).toBeInTheDocument();
    expect(screen.getByText(/Release 42|production · success/)).toBeInTheDocument();
  });

  it("shows empty states and keeps the warning visible", async () => {
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/suspect-changes`]: () => response({...suspects, has_repository_mapping: false, candidates: [], deployments: [], code_excerpts: []})}));
    render(<CodeEvidencePanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "No repository mapping"})).toBeInTheDocument();
    expect(screen.getByText(/Correlation does not prove causation/)).toBeInTheDocument();
  });

  it("shows no-suspects and provider failure states", async () => {
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/suspect-changes`]: () => response({...suspects, candidates: [], code_excerpts: []})}));
    const {unmount} = render(<CodeEvidencePanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByText("No suspect changes found in the incident window.")).toBeInTheDocument();
    unmount();
    client = new QueryClient({defaultOptions: {queries: {retry: false}}});
    vi.stubGlobal("fetch", route({[`GET /incidents/${incId}/suspect-changes`]: () => response({error: {code: "x", message: "down"}}, 503)}));
    render(<CodeEvidencePanel incidentId={incId}/>, {wrapper});
    expect(await screen.findByRole("heading", {name: "Provider unavailable"})).toBeInTheDocument();
    expect(screen.getByText(/Correlation does not prove causation/)).toBeInTheDocument();
  });
});

describe("mobile layout", () => {
  const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");
  const mobile = css.slice(css.indexOf("@media (max-width: 820px)"));

  it("collapses grids and keeps code scrollable inside its own container at narrow widths", () => {
    expect(mobile).toContain(".repository-card dl");
    expect(mobile).toContain(".mapping-list li");
    expect(mobile).toMatch(/\.form-grid[^{]*\{[^}]*grid-template-columns: 1fr/);
    expect(css).toMatch(/\.code-excerpt \{[^}]*overflow-x: auto/);
    expect(css).toMatch(/\.code-excerpt \{[^}]*max-width: 100%/);
  });

  it("renders core screens without fixed-width inline styles", async () => {
    vi.stubGlobal("fetch", route(base));
    Object.defineProperty(window, "innerWidth", {configurable: true, value: 375});
    const {container} = render(<RepositoryList/>, {wrapper});
    await screen.findByRole("link", {name: "acme-corp/shop"});
    expect(container.querySelectorAll("[style*='width']")).toHaveLength(0);
  });
});
