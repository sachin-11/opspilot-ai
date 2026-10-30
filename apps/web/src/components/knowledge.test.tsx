import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({useRouter: () => ({push}), usePathname: () => "/knowledge"}));
vi.mock("./providers", () => ({
  useOps: () => ({
    baseUrl: "http://api.test",
    workspace: {id: "11111111-1111-4111-8111-111111111111", name: "Demo", slug: "demo"},
    workspaces: [], connection: "live",
  }),
}));

import { KnowledgeCreate } from "./knowledge-create";
import { KnowledgeDetailView } from "./knowledge-detail";
import { KnowledgeList } from "./knowledge-list";
import { KnowledgeSearchPlayground } from "./knowledge-search";

const documentId = "22222222-2222-4222-8222-222222222222";
const versionId = "33333333-3333-4333-8333-333333333333";
const now = "2026-10-30T10:00:00Z";
const summary = {id: documentId, workspace_id: "11111111-1111-4111-8111-111111111111", service_id: null, title: "Checkout recovery", slug: "checkout-recovery", description: "Database recovery", document_type: "runbook", status: "ready", current_version_id: versionId, tags: ["checkout", "database"], archived_at: null, created_at: now, updated_at: now, service_name: null, current_version_number: 1, chunk_count: 1};
const detail = {...summary, versions: [{id: versionId, workspace_id: summary.workspace_id, document_id: documentId, version_number: 1, original_filename: "checkout.md", mime_type: "text/markdown", storage_key: null, content_hash: "a".repeat(64), extracted_text_hash: "b".repeat(64), byte_size: 128, page_count: null, embedding_provider: "fake", embedding_model: "hash-bow-v1", embedding_dimensions: 64, chunk_count: 1, status: "ready", failure_code: null, failure_message: null, created_at: now, processed_at: now}], jobs: [{id: "44444444-4444-4444-8444-444444444444", workspace_id: summary.workspace_id, document_id: documentId, document_version_id: versionId, status: "completed", current_stage: "ready", attempt: 1, chunks_processed: 1, total_chunks: 1, started_at: now, completed_at: now, failed_at: null, failure_code: null, failure_message: null, created_at: now, updated_at: now}], chunks: [{id: "55555555-5555-4555-8555-555555555555", chunk_index: 0, content: "Checkout database recovery steps", heading_path: ["Checkout"], page_number: null, token_count: 4, citation_label: "[Runbook: Checkout recovery, Checkout]"}]};
const service = {id: "66666666-6666-4666-8666-666666666666", workspace_id: summary.workspace_id, name: "Checkout", slug: "checkout", description: null, environment: "production", created_at: now, updated_at: now};

function wrapper({children}: {children: ReactNode}) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
function response(body: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}})); }

beforeEach(() => { push.mockReset(); vi.spyOn(window, "confirm").mockReturnValue(true); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("Module 3 knowledge UI", () => {
  it("renders the knowledge list and applies a document filter", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response([summary])));
    render(<KnowledgeList />, {wrapper});
    expect(await screen.findByRole("link", {name: "Checkout recovery"})).toBeInTheDocument();
    expect(screen.getByText("checkout · database")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Document type"), "runbook");
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("document_type=runbook"), expect.anything()));
  });

  it("validates upload mode and creates direct Markdown without duplicate submission", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST" ? response({document_id: documentId, version_id: versionId, job_id: "77777777-7777-4777-8777-777777777777", status: "queued"}, 202) : response([service]));
    vi.stubGlobal("fetch", fetchMock);
    render(<KnowledgeCreate />, {wrapper});
    await userEvent.type(screen.getByLabelText("Title"), "Checkout recovery");
    await userEvent.type(screen.getByLabelText("Slug"), "checkout-recovery");
    await userEvent.click(screen.getByRole("button", {name: "Upload and process"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("meaningful text");
    await userEvent.type(screen.getByLabelText("Markdown or plain text"), "# Recovery\nCheck database connection saturation.");
    await userEvent.click(screen.getByRole("button", {name: "Upload and process"}));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/knowledge/${documentId}`));
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(1);
  });

  it("shows detail, progress, version history, citations, reindex and archive confirmations", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST" && url.endsWith("/archive")) return response({document_id: documentId, status: "archived"});
      if (init?.method === "POST" && url.endsWith("/reindex")) return response({...detail.jobs[0], id: "77777777-7777-4777-8777-777777777777", status: "queued", current_stage: "queued", chunks_processed: 0, total_chunks: null, started_at: null, completed_at: null});
      return response(detail);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<KnowledgeDetailView documentId={documentId} />, {wrapper});
    expect(await screen.findByRole("heading", {name: "Checkout recovery"})).toBeInTheDocument();
    expect(screen.getByText("1/1 chunks")).toBeInTheDocument();
    expect(screen.getByText("v1 · current")).toBeInTheDocument();
    expect(screen.getByText("[Runbook: Checkout recovery, Checkout]")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Reindex"}));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/reindex"), expect.objectContaining({method: "POST"})));
    await userEvent.click(screen.getByRole("button", {name: "Archive"}));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/knowledge"));
    expect(window.confirm).toHaveBeenCalledTimes(2);
  });

  it("renders search filters, citation scores, and reranker fallback", async () => {
    const result = {results: [{chunk_id: "55555555-5555-4555-8555-555555555555", document_id: documentId, document_version_id: versionId, document_title: "Checkout recovery", document_type: "runbook", service_id: service.id, service_name: "Checkout", heading_path: ["Checkout"], page_number: null, excerpt: "Inspect database connections", lexical_rank: 1, lexical_score: 0.4, vector_rank: 1, vector_score: 0.9, rrf_score: 0.03, rerank_score: 0.8, citation_label: "[Runbook: Checkout recovery, Checkout]"}], diagnostics: {variant: "hybrid_rrf_rerank", candidate_count: 1, latency_ms: 12.5, embedding_latency_ms: 2, rerank_latency_ms: 1, rerank_fallback_used: true, low_relevance: false}};
    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST" ? response(result) : response([service])));
    render(<KnowledgeSearchPlayground />, {wrapper});
    await userEvent.type(screen.getByLabelText("Query"), "checkout database exhaustion");
    await userEvent.selectOptions(screen.getByLabelText("Service"), service.id);
    await userEvent.selectOptions(screen.getByLabelText("Variant"), "hybrid_rrf_rerank");
    await userEvent.click(screen.getByRole("button", {name: "Search knowledge"}));
    expect(await screen.findByText("[Runbook: Checkout recovery, Checkout]")).toBeInTheDocument();
    expect(screen.getByText("Fallback to RRF")).toBeInTheDocument();
    expect(screen.getByText("0.0300")).toBeInTheDocument();
  });

  it("shows processing failure and low-relevance states", async () => {
    const failed = {...detail, status: "failed", jobs: [{...detail.jobs[0], status: "failed", current_stage: "embedding", failure_message: "Embedding provider unavailable"}], chunks: []};
    vi.stubGlobal("fetch", vi.fn(() => response(failed)));
    render(<KnowledgeDetailView documentId={documentId} />, {wrapper});
    expect(await screen.findByText("Embedding provider unavailable")).toBeInTheDocument();
  });

  it("shows an explicit low-relevance search state", async () => {
    const empty = {results: [], diagnostics: {variant: "hybrid_rrf", candidate_count: 0, latency_ms: 3.2, embedding_latency_ms: 1, rerank_latency_ms: null, rerank_fallback_used: false, low_relevance: true}};
    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST" ? response(empty) : response([])));
    render(<KnowledgeSearchPlayground />, {wrapper});
    await userEvent.type(screen.getByLabelText("Query"), "unknown operational issue");
    await userEvent.click(screen.getByRole("button", {name: "Search knowledge"}));
    expect(await screen.findByRole("heading", {name: "No sufficiently relevant result"})).toBeInTheDocument();
  });
});
