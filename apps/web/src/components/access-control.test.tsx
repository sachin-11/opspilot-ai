import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ops = vi.hoisted(() => ({
  value: {
    baseUrl: "http://api.test",
    workspace: {id: "11111111-1111-4111-8111-111111111111", name: "Acme"},
    workspaces: [{id: "11111111-1111-4111-8111-111111111111", name: "Acme"}],
    connection: "live",
    user: {id: "99999999-9999-4999-8999-999999999999", email: "me@example.test", display_name: "Me", identity_provider: "local", status: "active"},
    role: "owner",
    permissions: ["members.manage", "members.read", "api_keys.manage", "audit.read", "incidents.read", "remediations.read"],
    selectWorkspace: vi.fn(),
    refreshMemberships: vi.fn(async () => undefined),
  } as Record<string, unknown>,
}));
vi.mock("./providers", () => ({useOps: () => ops.value}));
vi.mock("next/navigation", () => ({usePathname: () => "/incidents"}));

import { ApiKeysAdmin } from "./api-keys-admin";
import { AppShell } from "./app-shell";
import { AccessDeniedView, AuthErrorView, LoginView, SessionExpiredView } from "./auth-pages";
import { AuditViewer } from "./audit-viewer";
import { MembersAdmin } from "./members-admin";
import { setCsrfToken, setSessionExpiredHandler } from "@/lib/api";

const WS = "11111111-1111-4111-8111-111111111111";
const now = "2026-10-31T10:00:00Z";
const member = (id: string, role: string, email: string, status = "active") => ({id, user_id: `${id.slice(0, 8)}-aaaa-4aaa-8aaa-aaaaaaaaaaaa`, email, display_name: email.split("@")[0], role, status, created_at: now});
const OWNER = member("aaaaaaaa-0000-4000-8000-000000000001", "owner", "owner@example.test");
const VIEWER = member("bbbbbbbb-0000-4000-8000-000000000002", "viewer", "viewer@example.test");

function wrapper({children}: {children: ReactNode}) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}});
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}}));
type Route = (url: string, init?: RequestInit) => Promise<Response> | undefined;
function mockFetch(...routes: Route[]) {
  const calls: Array<{url: string; method: string; body: unknown; headers: Record<string, string>}> = [];
  const fn = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined, headers: (init?.headers ?? {}) as Record<string, string>});
    for (const route of routes) { const result = route(url, init); if (result) return result; }
    return json({error: {message: "unmocked"}}, 500);
  });
  vi.stubGlobal("fetch", fn);
  return {calls, fn};
}

beforeEach(() => {
  setCsrfToken("csrf-xyz");
  setSessionExpiredHandler(() => undefined);
  ops.value.role = "owner";
  ops.value.permissions = ["members.manage", "members.read", "api_keys.manage", "audit.read", "incidents.read", "remediations.read"];
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("auth pages", () => {
  it("shows the local identity providers only when offered and keeps return URLs same-origin", () => {
    render(<LoginView returnTo="https://evil.example" localUsers={[{hint: "admin", label: "Admin"}]}/>);
    const link = screen.getByRole("link", {name: "Continue as Admin"});
    expect(link.getAttribute("href")).toContain("returnTo=%2F");
    expect(link.getAttribute("href")).not.toContain("evil");
    expect(screen.getByText(/not available in production/)).toBeInTheDocument();
  });

  it("offers SSO sign-in in production mode", () => {
    render(<LoginView returnTo="/remediations" localUsers={[]}/>);
    expect(screen.getByRole("link", {name: "Sign in with SSO"}).getAttribute("href")).toBe("/api/auth/login?returnTo=%2Fremediations");
  });

  it("renders distinct, accessible session-expired, access-denied and error states", () => {
    const {unmount} = render(<SessionExpiredView returnTo="/incidents"/>);
    expect(screen.getByRole("alert")).toHaveTextContent("Session expired");
    expect(screen.getByRole("link", {name: "Sign in again"}).getAttribute("href")).toBe("/login?returnTo=%2Fincidents");
    unmount();
    const denied = render(<AccessDeniedView/>);
    expect(screen.getByRole("alert")).toHaveTextContent("Access denied");
    denied.unmount();
    render(<AuthErrorView reason="state"/>);
    expect(screen.getByRole("alert")).toHaveTextContent(/state mismatch/);
  });
});

describe("role-aware navigation", () => {
  it("shows only what the permissions allow, the current user, and role", () => {
    ops.value.role = "viewer";
    ops.value.permissions = ["incidents.read", "remediations.read", "workspace.read"];
    render(<AppShell><p>content</p></AppShell>);
    const nav = screen.getByRole("navigation", {name: "Primary navigation"});
    expect(within(nav).getByRole("link", {name: "Incidents"})).toBeInTheDocument();
    expect(within(nav).queryByRole("link", {name: "Members"})).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", {name: "API keys"})).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", {name: "Audit log"})).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", {name: "Create incident"})).not.toBeInTheDocument();
    expect(screen.getByLabelText("Signed in user")).toHaveTextContent("Me · Viewer");
    expect(screen.getByRole("button", {name: "Sign out"})).toBeInTheDocument();
  });

  it("offers administration links to owners and a workspace selector with several workspaces", async () => {
    ops.value.workspaces = [{id: WS, name: "Acme"}, {id: "22222222-2222-4222-8222-222222222222", name: "Globex"}];
    render(<AppShell><p>content</p></AppShell>);
    expect(screen.getByRole("link", {name: "Members"})).toBeInTheDocument();
    expect(screen.getByRole("link", {name: "Audit log"})).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Workspace"), "22222222-2222-4222-8222-222222222222");
    expect(ops.value.selectWorkspace).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
    ops.value.workspaces = [{id: WS, name: "Acme"}];
  });

  it("signs out with the CSRF token and clears client state", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", {...window.location, set href(value: string) { assign(value); }});
    const {calls} = mockFetch((url) => url === "/api/auth/session" ? json({csrfToken: "csrf-xyz"}) : url === "/api/auth/logout" ? json({ok: true}) : undefined);
    render(<AppShell><p>x</p></AppShell>);
    await userEvent.click(screen.getByRole("button", {name: "Sign out"}));
    await waitFor(() => expect(calls.some((call) => call.url === "/api/auth/logout")).toBe(true));
    expect(calls.find((call) => call.url === "/api/auth/logout")!.headers["X-CSRF-Token"]).toBe("csrf-xyz");
  });
});

describe("member management", () => {
  const route: Route = (url, init) => {
    if (url.endsWith("/members") && !init?.method) return json([OWNER, VIEWER]);
    if (url.endsWith("/invitations") && !init?.method) return json([]);
    return undefined;
  };

  it("lists members, protects the final owner in the UI, and keeps role changes confirmed", async () => {
    const {calls} = mockFetch((url, init) => init?.method === "PATCH" ? json({...VIEWER, role: "responder"}) : undefined, route);
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<MembersAdmin/>, {wrapper});
    expect(await screen.findByText("owner@example.test", {selector: "small"})).toBeInTheDocument();
    const ownerRole = screen.getByLabelText("Role for owner@example.test");
    expect(ownerRole).toBeDisabled();
    const rows = screen.getAllByRole("row");
    expect(within(rows.find((row) => row.textContent?.includes("owner@example.test"))!).getByRole("button", {name: "Remove"})).toBeDisabled();
    expect(screen.getByText(/Final owner/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Role for viewer@example.test"), "responder");
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Viewer to Responder"));
    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH")!;
    expect(patch.url).toContain(`/members/${VIEWER.id}`);
    expect(patch.body).toEqual({role: "responder"});
    expect(patch.headers["X-CSRF-Token"]).toBe("csrf-xyz");
    expect(await screen.findByText("Member updated.")).toBeInTheDocument();
  });

  it("does nothing when the confirmation is declined", async () => {
    const {calls} = mockFetch(route);
    vi.stubGlobal("confirm", vi.fn(() => false));
    render(<MembersAdmin/>, {wrapper});
    await screen.findByLabelText("Role for viewer@example.test");
    await userEvent.selectOptions(screen.getByLabelText("Role for viewer@example.test"), "admin");
    await userEvent.click(within(screen.getAllByRole("row").find((row) => row.textContent?.includes("viewer@example.test"))!).getByRole("button", {name: "Remove"}));
    expect(calls.some((call) => ["PATCH", "DELETE"].includes(call.method))).toBe(false);
  });

  it("removes a member after confirmation and surfaces a backend 403 even though the button was visible", async () => {
    mockFetch((url, init) => init?.method === "DELETE" ? json({error: {code: "forbidden", message: "missing permission members.manage"}}, 403) : undefined, route);
    vi.stubGlobal("confirm", vi.fn(() => true));
    render(<MembersAdmin/>, {wrapper});
    await screen.findByLabelText("Role for viewer@example.test");
    await userEvent.click(within(screen.getAllByRole("row").find((row) => row.textContent?.includes("viewer@example.test"))!).getByRole("button", {name: "Remove"}));
    expect(await screen.findByText(/do not have permission.*role may have changed/)).toBeInTheDocument();
    expect(ops.value.refreshMemberships).toHaveBeenCalled();
  });

  it("creates an invitation and shows the one-time token only until dismissed", async () => {
    const created = {id: "cccccccc-0000-4000-8000-000000000003", email: "new@example.test", role: "analyst", expires_at: now, accepted_at: null, revoked_at: null, created_at: now, token: "ONE-TIME-INVITE-TOKEN-1234567890"};
    const {calls} = mockFetch((url, init) => init?.method === "POST" && url.endsWith("/invitations") ? json(created, 201) : undefined, route);
    render(<MembersAdmin/>, {wrapper});
    await screen.findByLabelText("Email");
    await userEvent.type(screen.getByLabelText("Email"), "new@example.test");
    await userEvent.selectOptions(screen.getByLabelText("Role"), "analyst");
    await userEvent.click(screen.getByRole("button", {name: "Create invitation"}));
    expect(await screen.findByText("ONE-TIME-INVITE-TOKEN-1234567890")).toBeInTheDocument();
    expect(screen.getByText(/shown only once/)).toBeInTheDocument();
    expect(calls.find((call) => call.method === "POST")!.body).toEqual({email: "new@example.test", role: "analyst"});
    await userEvent.click(screen.getByRole("button", {name: "I have copied it"}));
    expect(screen.queryByText("ONE-TIME-INVITE-TOKEN-1234567890")).not.toBeInTheDocument();
  });

  it("hides administration controls from users without members.manage (UX only)", async () => {
    ops.value.permissions = ["members.read"];
    mockFetch(route);
    render(<MembersAdmin/>, {wrapper});
    await screen.findByText("owner@example.test", {selector: "small"});
    expect(screen.queryByRole("button", {name: "Create invitation"})).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Role for owner@example.test")).not.toBeInTheDocument();
  });

  it("renders a forbidden state when the API refuses to list members", async () => {
    mockFetch(() => json({error: {code: "forbidden", message: "missing permission members.read"}}, 403));
    render(<MembersAdmin/>, {wrapper});
    expect(await screen.findByRole("alert")).toHaveTextContent(/do not have permission/);
  });
});

describe("API key management", () => {
  const KEY = {id: "dddddddd-0000-4000-8000-000000000004", name: "ci", key_prefix: "ospk_abcd1234", scopes: ["incidents:read"], status: "active", created_by_user_id: "99999999-9999-4999-8999-999999999999", expires_at: null, last_used_at: null, revoked_at: null, created_at: now};

  it("reveals a new key exactly once and sends only the chosen scopes", async () => {
    const {calls} = mockFetch((url, init) => init?.method === "POST" ? json({...KEY, key: "ospk_abcd1234_FULL-SECRET-VALUE"}, 201) : url.endsWith("/api-keys") ? json([KEY]) : undefined);
    render(<ApiKeysAdmin/>, {wrapper});
    await screen.findByText("ospk_abcd1234");
    await userEvent.type(screen.getByLabelText("Name"), "pipeline");
    await userEvent.click(screen.getByLabelText("events:write"));
    await userEvent.click(screen.getByRole("button", {name: "Create API key"}));
    expect(await screen.findByText("ospk_abcd1234_FULL-SECRET-VALUE")).toBeInTheDocument();
    expect(calls.find((call) => call.method === "POST")!.body).toEqual({name: "pipeline", scopes: ["events:write"]});
    expect(screen.queryByText(/remediation|publish|approve/i, {selector: "label"})).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "I have copied it"}));
    expect(screen.queryByText("ospk_abcd1234_FULL-SECRET-VALUE")).not.toBeInTheDocument();
  });

  it("requires at least one scope", async () => {
    const {calls} = mockFetch((url) => url.endsWith("/api-keys") ? json([KEY]) : undefined);
    render(<ApiKeysAdmin/>, {wrapper});
    await screen.findByText("ospk_abcd1234");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("button", {name: "Create API key"}));
    expect(await screen.findByText("Select at least one scope.")).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("confirms revocation and rotation, and shows the rotated key once", async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    const {calls} = mockFetch((url, init) => {
      if (init?.method === "POST" && url.endsWith("/revoke")) return json({...KEY, status: "revoked"});
      if (init?.method === "POST" && url.endsWith("/rotate")) return json({...KEY, id: "eeeeeeee-0000-4000-8000-000000000005", key: "ospk_new00000_ROTATED-SECRET"});
      return url.endsWith("/api-keys") ? json([KEY]) : undefined;
    });
    render(<ApiKeysAdmin/>, {wrapper});
    await screen.findByText("ospk_abcd1234");
    await userEvent.click(screen.getByRole("button", {name: "Rotate"}));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("stops working immediately"));
    expect(await screen.findByText("ospk_new00000_ROTATED-SECRET")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Revoke"}));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("cannot be undone"));
    await waitFor(() => expect(calls.some((call) => call.url.endsWith("/revoke"))).toBe(true));
    expect(await screen.findByText(/revoked\. It stops working immediately/)).toBeInTheDocument();
  });

  it("explains a rate-limited request", async () => {
    mockFetch((url, init) => init?.method === "POST" ? json({error: {code: "rate_limited", message: "too many requests"}}, 429) : url.endsWith("/api-keys") ? json([KEY]) : undefined);
    render(<ApiKeysAdmin/>, {wrapper});
    await screen.findByText("ospk_abcd1234");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByLabelText("incidents:read"));
    await userEvent.click(screen.getByRole("button", {name: "Create API key"}));
    expect(await screen.findByText(/Too many requests/)).toBeInTheDocument();
  });
});

describe("audit viewer", () => {
  const event = (id: string, action: string, result: string, valid = true) => ({id, workspace_id: WS, actor_type: "user", actor_user_id: "99999999-9999-4999-8999-999999999999", actor_api_key_id: null, action, resource_type: "membership", resource_id: null, request_id: "req-1", result, reason_code: result === "denied" ? "permission_missing" : null, metadata: {}, integrity_valid: valid, occurred_at: now});

  it("shows events, flags integrity mismatches, filters, and loads more with the cursor", async () => {
    const {calls} = mockFetch((url) => {
      if (!url.includes("/audit-events")) return undefined;
      if (url.includes("cursor=next-1")) return json({items: [event("22222222-0000-4000-8000-000000000002", "api_key.create", "success")], next_cursor: null});
      if (url.includes("result=denied")) return json({items: [event("33333333-0000-4000-8000-000000000003", "authz.denied", "denied")], next_cursor: null});
      return json({items: [event("11111111-0000-4000-8000-000000000001", "membership.update", "success", false)], next_cursor: "next-1"});
    });
    render(<AuditViewer/>, {wrapper});
    expect(await screen.findByText("membership.update")).toBeInTheDocument();
    expect(screen.getByText("MISMATCH")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Load more"}));
    expect(await screen.findByText("api_key.create")).toBeInTheDocument();
    expect(calls.some((call) => call.url.includes("cursor=next-1"))).toBe(true);
    await userEvent.selectOptions(screen.getByLabelText("Result"), "denied");
    expect(await screen.findByText("authz.denied")).toBeInTheDocument();
    expect(screen.getByText(/permission_missing/)).toBeInTheDocument();
  });

  it("distinguishes a forbidden audit log from an empty one", async () => {
    mockFetch(() => json({error: {code: "forbidden", message: "missing permission audit.read"}}, 403));
    const {unmount} = render(<AuditViewer/>, {wrapper});
    expect(await screen.findByRole("alert")).toHaveTextContent(/do not have permission/);
    unmount();
    mockFetch(() => json({items: [], next_cursor: null}));
    render(<AuditViewer/>, {wrapper});
    expect(await screen.findByText("No audit events")).toBeInTheDocument();
  });
});

describe("session handling in the API client", () => {
  it("invokes the session-expired handler on a 401 and never retries silently", async () => {
    const expired = vi.fn();
    setSessionExpiredHandler(expired);
    const {calls} = mockFetch(() => json({error: {code: "session_expired", message: "Session expired"}}, 401));
    render(<MembersAdmin/>, {wrapper});
    await waitFor(() => expect(expired).toHaveBeenCalled());
    expect(calls.length).toBeLessThanOrEqual(2);
  });

  it("sends credentials same-origin only and never puts tokens in the URL", async () => {
    const {calls, fn} = mockFetch((url) => url.endsWith("/members") ? json([OWNER]) : json([]));
    render(<MembersAdmin/>, {wrapper});
    await screen.findByText("owner@example.test", {selector: "small"});
    expect(calls.every((call) => !/token|api_key|ticket/i.test(call.url))).toBe(true);
    expect(fn.mock.calls.every(([, init]) => (init as RequestInit).credentials === "same-origin")).toBe(true);
  });
});

describe("mobile layout", () => {
  it("keeps tables scrollable and wraps cells on narrow screens", () => {
    const css = readFileSync("src/app/globals.css", "utf8");
    expect(css).toMatch(/@media \(max-width: 640px\)[^}]*\.table-wrap td/s);
    expect(css).toMatch(/\.table-wrap\s*\{[^}]*overflow-x: auto/);
    expect(css).toMatch(/\.auth-card/);
  });
});
