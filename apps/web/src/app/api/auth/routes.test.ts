// @vitest-environment node
import { SignJWT } from "jose";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pkceChallenge, seal, SESSION_COOKIE, TX_COOKIE, type Session } from "@/lib/auth/security";
import { GET as login } from "./login/route";
import { GET as callback } from "./callback/route";
import { POST as logout } from "./logout/route";
import { GET as sessionInfo } from "./session/route";
import { DELETE as proxyDelete, GET as proxyGet, POST as proxyPost } from "../proxy/[...path]/route";

const SECRET = "s".repeat(40);
const LOCAL_KEY = "local-signing-key-for-tests";
const ORIGIN = "http://localhost:3300";

beforeEach(() => {
  vi.stubEnv("SESSION_SECRET", SECRET);
  vi.stubEnv("AUTH_MODE", "local");
  vi.stubEnv("APP_BASE_URL", ORIGIN);
  vi.stubEnv("API_INTERNAL_URL", "http://api:8000");
  vi.stubEnv("OIDC_AUTHORIZATION_URL", "http://localhost:8300/api/v1/auth/local/authorize");
  vi.stubEnv("OIDC_TOKEN_URL", "http://api:8000/api/v1/auth/local/token");
  vi.stubEnv("AUTH_LOCAL_SIGNING_KEY", LOCAL_KEY);
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

const live: Session = {sub: "u1", email: "a@example.test", name: "A", accessToken: "SECRET-ACCESS-TOKEN", expiresAt: Math.floor(Date.now() / 1000) + 600, csrf: "csrf-1"};
async function cookieFor(session: Session = live) { return `${SESSION_COOKIE}=${await seal(session, SECRET, 600)}`; }
const ctx = (path: string[]) => ({params: Promise.resolve({path})});

describe("login", () => {
  it("starts the authorization-code flow with PKCE S256, state, nonce, and an HttpOnly transaction cookie", async () => {
    const response = await login(new NextRequest(`${ORIGIN}/api/auth/login?returnTo=%2Fincidents&login_hint=admin`));
    const url = new URL(response.headers.get("location")!);
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("redirect_uri")).toBe(`${ORIGIN}/api/auth/callback`);
    expect(url.searchParams.get("state")!.length).toBeGreaterThan(20);
    expect(url.searchParams.get("nonce")!.length).toBeGreaterThan(20);
    const setCookie = response.headers.get("set-cookie")!;
    expect(setCookie).toContain(`${TX_COOKIE}=`);
    expect(setCookie.toLowerCase()).toContain("httponly");
    expect(setCookie.toLowerCase()).toContain("samesite=lax");
    expect(setCookie).not.toContain("code_verifier");
  });

  it("never turns an external returnTo into a redirect target", async () => {
    const response = await login(new NextRequest(`${ORIGIN}/api/auth/login?returnTo=https%3A%2F%2Fevil.example`));
    expect(response.headers.get("location")).not.toContain("evil.example");
  });
});

async function startedTransaction() {
  const response = await login(new NextRequest(`${ORIGIN}/api/auth/login?returnTo=%2Fremediations`));
  const url = new URL(response.headers.get("location")!);
  const tx = response.headers.get("set-cookie")!.split(";")[0]!;
  return {state: url.searchParams.get("state")!, nonce: url.searchParams.get("nonce")!, challenge: url.searchParams.get("code_challenge")!, tx};
}

async function idToken(nonce: string, extra: Record<string, unknown> = {}) {
  return new SignJWT({nonce, email: "a@example.test", name: "A", ...extra})
    .setProtectedHeader({alg: "HS256"}).setSubject("user-1").setIssuer("opspilot-local").setAudience("opspilot-api")
    .setIssuedAt().setExpirationTime("10m").sign(new TextEncoder().encode(LOCAL_KEY));
}

describe("callback", () => {
  it("rejects a state mismatch and a missing transaction without exchanging the code", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const {tx} = await startedTransaction();
    const bad = await callback(new NextRequest(`${ORIGIN}/api/auth/callback?code=c&state=wrong`, {headers: {cookie: tx}}));
    expect(bad.headers.get("location")).toContain("/auth-error?reason=state");
    const none = await callback(new NextRequest(`${ORIGIN}/api/auth/callback?code=c&state=s`));
    expect(none.headers.get("location")).toContain("/auth-error?reason=state");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("verifies PKCE, nonce, and the id_token, then sets a sealed HttpOnly session", async () => {
    const {state, nonce, challenge, tx} = await startedTransaction();
    let sentVerifier = "";
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      sentVerifier = new URLSearchParams(String(init.body)).get("code_verifier")!;
      return new Response(JSON.stringify({access_token: "SECRET-ACCESS-TOKEN", id_token: await idToken(nonce), expires_in: 900}), {status: 200});
    }));
    const response = await callback(new NextRequest(`${ORIGIN}/api/auth/callback?code=abc&state=${state}`, {headers: {cookie: tx}}));
    expect(await pkceChallenge(sentVerifier)).toBe(challenge);
    expect(response.headers.get("location")).toBe(`${ORIGIN}/remediations`);
    const cookie = response.headers.get("set-cookie")!;
    expect(cookie).toContain(`${SESSION_COOKIE}=`);
    expect(cookie.toLowerCase()).toContain("httponly");
    expect(cookie).not.toContain("SECRET-ACCESS-TOKEN");
    expect(response.headers.get("location")).not.toContain("token");
  });

  it("rejects a nonce mismatch, a forged id_token, and a failed exchange without echoing details", async () => {
    const {state, tx} = await startedTransaction();
    const request = () => new NextRequest(`${ORIGIN}/api/auth/callback?code=abc&state=${state}`, {headers: {cookie: tx}});
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({access_token: "a", id_token: await idToken("other-nonce")}), {status: 200})));
    expect((await callback(request())).headers.get("location")).toContain("reason=nonce");
    const forged = await new SignJWT({nonce: "n"}).setProtectedHeader({alg: "HS256"}).setSubject("x").setIssuer("opspilot-local").setAudience("opspilot-api").setExpirationTime("10m").sign(new TextEncoder().encode("attacker-key"));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({access_token: "a", id_token: forged}), {status: 200})));
    expect((await callback(request())).headers.get("location")).toContain("reason=validation");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", {status: 400})));
    expect((await callback(request())).headers.get("location")).toContain("reason=token_exchange");
  });

  it("routes provider errors to dedicated pages", async () => {
    expect((await callback(new NextRequest(`${ORIGIN}/api/auth/callback?error=access_denied`))).headers.get("location")).toContain("/access-denied");
    expect((await callback(new NextRequest(`${ORIGIN}/api/auth/callback?error=server_error`))).headers.get("location")).toContain("/auth-error");
  });
});

describe("session and logout", () => {
  it("returns session metadata without the token, 401 when absent or expired", async () => {
    const ok = await sessionInfo(new NextRequest(`${ORIGIN}/api/auth/session`, {headers: {cookie: await cookieFor()}}));
    const body = await ok.text();
    expect(ok.status).toBe(200);
    expect(body).toContain("csrfToken");
    expect(body).not.toContain("SECRET-ACCESS-TOKEN");
    expect((await sessionInfo(new NextRequest(`${ORIGIN}/api/auth/session`))).status).toBe(401);
    const stale = await cookieFor({...live, expiresAt: Math.floor(Date.now() / 1000) - 1});
    expect((await sessionInfo(new NextRequest(`${ORIGIN}/api/auth/session`, {headers: {cookie: stale}}))).status).toBe(401);
  });

  it("logs out only with a valid CSRF token and clears the cookie", async () => {
    const cookie = await cookieFor();
    const denied = await logout(new NextRequest(`${ORIGIN}/api/auth/logout`, {method: "POST", headers: {cookie, origin: ORIGIN}}));
    expect(denied.status).toBe(403);
    const ok = await logout(new NextRequest(`${ORIGIN}/api/auth/logout`, {method: "POST", headers: {cookie, origin: ORIGIN, "x-csrf-token": "csrf-1"}}));
    expect(ok.status).toBe(200);
    expect(ok.headers.get("set-cookie")).toContain(`${SESSION_COOKIE}=;`);
  });
});

describe("API proxy", () => {
  it("answers 401 without a session and never contacts the API", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await proxyGet(new NextRequest(`${ORIGIN}/api/proxy/api/v1/auth/me`), ctx(["api", "v1", "auth", "me"]));
    expect(response.status).toBe(401);
    expect(response.headers.get("x-session-expired")).toBe("1");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("attaches the bearer token server-side and does not leak it back", async () => {
    let seen: Headers | undefined;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => { seen = new Headers(init.headers); return new Response(JSON.stringify({ok: true}), {status: 200}); }));
    const response = await proxyGet(new NextRequest(`${ORIGIN}/api/proxy/api/v1/workspaces`, {headers: {cookie: await cookieFor(), authorization: "Bearer browser-supplied", "x-api-key": "k"}}), ctx(["api", "v1", "workspaces"]));
    expect(response.status).toBe(200);
    expect(seen?.get("authorization")).toBe("Bearer SECRET-ACCESS-TOKEN");
    expect(seen?.get("x-api-key")).toBeNull();
    expect(seen?.get("cookie")).toBeNull();
    expect(await response.text()).not.toContain("SECRET-ACCESS-TOKEN");
  });

  it("requires CSRF and same-origin for mutations", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", {status: 202}));
    vi.stubGlobal("fetch", fetchMock);
    const cookie = await cookieFor();
    const make = (headers: Record<string, string>) => new NextRequest(`${ORIGIN}/api/proxy/api/v1/x`, {method: "POST", headers: {cookie, ...headers}, body: "{}"});
    expect((await proxyPost(make({}), ctx(["api", "v1", "x"]))).status).toBe(403);
    expect((await proxyPost(make({origin: "http://evil.example", "x-csrf-token": "csrf-1"}), ctx(["api", "v1", "x"]))).status).toBe(403);
    expect((await proxyDelete(make({"x-csrf-token": "csrf-1"}), ctx(["api", "v1", "x"]))).status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
    expect((await proxyPost(make({origin: ORIGIN, "x-csrf-token": "csrf-1"}), ctx(["api", "v1", "x"]))).status).toBe(202);
  });

  it("only forwards /api/v1 paths and hides the local IdP", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const cookie = await cookieFor();
    for (const path of [["health", "other"], ["api", "v1", "auth", "local", "token"], ["api", "v1", "..", "secret"]]) {
      const response = await proxyGet(new NextRequest(`${ORIGIN}/api/proxy/${path.join("/")}`, {headers: {cookie}}), ctx(path));
      expect(response.status).toBe(404);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards only the public GET health endpoints, without a session or a token", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({status: "degraded"}), {status: 503, headers: {"content-type": "application/json"}}));
    vi.stubGlobal("fetch", fetchMock);
    const response = await proxyGet(new NextRequest(`${ORIGIN}/api/proxy/health/ready`), ctx(["health", "ready"]));
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({status: "degraded"});
    expect(response.headers.get("cache-control")).toBe("no-store");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://api:8000/health/ready");
    expect(init.headers).toBeUndefined();
    // Mutating methods and other health-like paths stay blocked.
    const post = await proxyPost(new NextRequest(`${ORIGIN}/api/proxy/health/ready`, {method: "POST", body: "{}"}), ctx(["health", "ready"]));
    expect(post.status).toBe(404);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("passes through 403, 404, and 429 distinctly with Retry-After", async () => {
    const cookie = await cookieFor();
    for (const [status, extra] of [[403, {}], [404, {}], [429, {"retry-after": "7"}]] as const) {
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({error: {code: "x", message: "m"}}), {status, headers: extra})));
      const response = await proxyGet(new NextRequest(`${ORIGIN}/api/proxy/api/v1/y`, {headers: {cookie}}), ctx(["api", "v1", "y"]));
      expect(response.status).toBe(status);
      if (status === 429) expect(response.headers.get("retry-after")).toBe("7");
    }
  });
});
