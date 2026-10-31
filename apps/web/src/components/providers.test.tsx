import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/realtime", async (original) => ({...(await original<typeof import("@/lib/realtime")>()), reconnectDelay: () => 5}));

import { setSessionExpiredHandler } from "@/lib/api";
import { OpsProviders, useOps } from "./providers";

const WS = "11111111-1111-4111-8111-111111111111";
const now = "2026-10-31T10:00:00Z";

class FakeSocket {
  static instances: FakeSocket[] = [];
  static OPEN = 1;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: {data: string}) => void) | null = null;
  onclose: ((event: {code: number}) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { FakeSocket.instances.push(this); queueMicrotask(() => this.onopen?.()); }
  send() {}
  close() {}
  serverClose(code: number) { this.onclose?.({code}); }
}

function Probe() {
  const {connection, workspace, role, permissions} = useOps();
  return <div><span data-testid="state">{connection}</span><span data-testid="ws">{workspace?.name ?? "none"}</span><span data-testid="role">{role ?? "none"}</span><span data-testid="perms">{permissions.join(",")}</span></div>;
}

let tickets = 0;
let memberships: unknown[];
let fetchMock: ReturnType<typeof vi.fn>;
const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}}));
const membership = (role: string, permissions: string[]) => ({workspace: {id: WS, name: "Acme", slug: "acme", created_at: now, updated_at: now}, membership_id: "22222222-2222-4222-8222-222222222222", role, permissions});

beforeEach(() => {
  tickets = 0;
  FakeSocket.instances = [];
  memberships = [membership("admin", ["workspace.read", "members.manage"])];
  fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/auth/session") return json({csrfToken: "csrf-1"});
    if (url.endsWith("/auth/me")) return json({id: "33333333-3333-4333-8333-333333333333", email: "a@example.test", display_name: "A", identity_provider: "local", status: "active"});
    if (url.endsWith("/auth/workspaces")) return json(memberships);
    if (url.endsWith("/auth/ws-ticket")) { tickets += 1; return json({ticket: `ticket-${tickets}`, expires_in_seconds: 30, channel: "incidents"}); }
    return json({}, init ? 200 : 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => { vi.unstubAllGlobals(); window.localStorage.clear(); });

const mount = () => render(<OpsProviders baseUrl="/api/proxy" wsBaseUrl="http://localhost:8300"><Probe/></OpsProviders>);

describe("workspace and realtime provider", () => {
  it("loads role and permissions from the server, never from the client", async () => {
    mount();
    expect(await screen.findByTestId("role")).toHaveTextContent("admin");
    expect(screen.getByTestId("perms")).toHaveTextContent("workspace.read,members.manage");
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes("role="))).toBe(true);
  });

  it("connects with a fresh single-use ticket (never a token) and gets a new one on every reconnect", async () => {
    mount();
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    const first = FakeSocket.instances[0]!;
    expect(first.url).toBe(`ws://localhost:8300/api/v1/ws/workspaces/${WS}/incidents?ticket=ticket-1`);
    expect(first.url).not.toMatch(/token=|access|Bearer/i);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("live"));
    first.serverClose(1006);
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(2));
    expect(FakeSocket.instances[1]!.url).toContain("ticket=ticket-2");
    FakeSocket.instances[1]!.serverClose(4401); // expired/used ticket: again a new one
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(3));
    expect(FakeSocket.instances[2]!.url).toContain("ticket=ticket-3");
    expect(tickets).toBe(3);
  });

  it("drops access promptly when the socket reports a revoked membership", async () => {
    mount();
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("live"));
    memberships = []; // the server no longer lists this workspace
    FakeSocket.instances[0]!.serverClose(4403);
    await waitFor(() => expect(screen.getByTestId("ws")).toHaveTextContent("none"));
    expect(screen.getByTestId("role")).toHaveTextContent("none");
  });

  it("falls back to degraded (and retries with new tickets) when ticket issuance fails", async () => {
    let failures = 1;
    const original = fetchMock.getMockImplementation() as (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/auth/ws-ticket") && failures-- > 0) return json({error: {message: "slow down"}}, 429);
      return original(input, init);
    });
    mount();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("degraded"));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("live"));
  });

  it("shows a session-expired message instead of stale data when the memberships call is 401", async () => {
    const original = fetchMock.getMockImplementation() as (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) =>
      String(input).endsWith("/auth/workspaces") ? json({error: {message: "expired"}}, 401) : original(input, init));
    setSessionExpiredHandler(() => undefined); // the browser redirect is covered by the API client tests
    mount();
    expect(await screen.findByRole("alert", {}, {timeout: 4000})).toHaveTextContent(/Session expired/);
  });

  it("remembers the selected workspace between visits", async () => {
    memberships = [membership("viewer", ["workspace.read"]), {...membership("owner", ["workspace.read", "members.manage"]), workspace: {id: "44444444-4444-4444-8444-444444444444", name: "Globex", slug: "g", created_at: now, updated_at: now}}];
    window.localStorage.setItem("opspilot.workspace", "44444444-4444-4444-8444-444444444444");
    mount();
    expect(await screen.findByTestId("ws")).toHaveTextContent("Globex");
    expect(screen.getByTestId("role")).toHaveTextContent("owner");
  });
});
