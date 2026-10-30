import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({useRouter: () => ({push}), usePathname: () => "/incidents"}));
vi.mock("./providers", () => ({
  useOps: () => ({
    baseUrl: "http://api.test",
    workspace: {id: "11111111-1111-4111-8111-111111111111", name: "Demo", slug: "demo", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z"},
    workspaces: [], connection: "live",
  }),
}));

import { IncidentDetail } from "./incident-detail";
import { IncidentForm } from "./incident-form";
import { IncidentList } from "./incident-list";
import { ServiceSettings } from "./service-settings";

const incident = {
  id: "22222222-2222-4222-8222-222222222222", workspace_id: "11111111-1111-4111-8111-111111111111",
  service_id: "33333333-3333-4333-8333-333333333333", service_name: "Checkout API", service_environment: "production",
  title: "Checkout errors", description: "5xx elevated", severity: "sev2", status: "open", source: "manual", fingerprint: null,
  version: 1, opened_at: "2026-10-30T10:00:00Z", acknowledged_at: null, mitigated_at: null, resolved_at: null,
  created_at: "2026-10-30T10:00:00Z", updated_at: "2026-10-30T10:00:00Z",
};
const service = {id: "33333333-3333-4333-8333-333333333333", workspace_id: "11111111-1111-4111-8111-111111111111", name: "Checkout API", slug: "checkout-api", description: "Core checkout", environment: "production", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z"};

function wrapper({children}: {children: ReactNode}) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function response(body: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}})); }

beforeEach(() => { push.mockReset(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("Module 1 operations UI", () => {
  it("renders incidents from the API and exposes filters", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => String(input).includes("/services") ? response([service]) : response({items: [incident], next_cursor: null})));
    render(<IncidentList />, {wrapper});
    expect(screen.getByText("Loading incidents…")).toBeInTheDocument();
    expect(await screen.findByRole("link", {name: "Checkout errors"})).toBeInTheDocument();
    expect(screen.getByLabelText("Status")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "sev2");
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("severity=sev2"), expect.anything()));
  });

  it("shows a useful empty incident state", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => String(input).includes("/services") ? response([]) : response({items: [], next_cursor: null})));
    render(<IncidentList />, {wrapper});
    expect(await screen.findByText("No matching incidents")).toBeInTheDocument();
  });

  it("validates incident creation and routes after success", async () => {
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => init?.method === "POST" ? response(incident, 201) : response([service])));
    render(<IncidentForm />, {wrapper});
    const title = screen.getByLabelText("Title");
    await userEvent.click(screen.getByRole("button", {name: "Create incident"}));
    expect(title).toBeInvalid();
    await userEvent.type(title, "Checkout errors");
    await userEvent.click(screen.getByRole("button", {name: "Create incident"}));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/incidents/${incident.id}`));
  });

  it("renders incident detail, timeline and reports a stale-version conflict", async () => {
    const timeline = [{id: "44444444-4444-4444-8444-444444444444", workspace_id: incident.workspace_id, incident_id: incident.id, event_type: "incident_created", actor_type: "user", actor_id: null, summary: "Incident created", metadata: {}, occurred_at: incident.opened_at, created_at: incident.created_at}];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") return response({error: {message: "incident version is stale"}}, 409);
      if (String(input).includes("/investigations")) return response([]);
      return String(input).endsWith("/timeline") ? response(timeline) : response(incident);
    }));
    render(<IncidentDetail incidentId={incident.id} />, {wrapper});
    expect(await screen.findByRole("heading", {name: "Checkout errors"})).toBeInTheDocument();
    expect(await screen.findByText("Incident created")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "investigating"}));
    expect(await screen.findByText(/changed elsewhere/)).toBeInTheDocument();
  });

  it("creates a monitored service and refreshes the list", async () => {
    let created = false;
    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {created = true; return response(service, 201);}
      return response(created ? [service] : []);
    }));
    render(<ServiceSettings />, {wrapper});
    expect(await screen.findByText("No monitored services yet.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Name"), "Checkout API");
    await userEvent.click(screen.getByRole("button", {name: "Create service"}));
    expect(await screen.findByText("Service created.")).toBeInTheDocument();
    expect(await screen.findByText("Core checkout")).toBeInTheDocument();
  });
});
