import { NextResponse, type NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { csrfValid, CSRF_HEADER, openSession, SESSION_COOKIE } from "@/lib/auth/security";

// x-forwarded-for carries the edge-appended client address so the API can rate limit and audit the
// real caller. The API only believes it when this server is inside TRUSTED_PROXY_CIDRS.
const FORWARD_REQUEST = ["content-type", "accept", "idempotency-key", "x-request-id", "x-forwarded-for"];
const PUBLIC_HEALTH = new Set(["health/live", "health/ready"]);
const FORWARD_RESPONSE =["content-type", "retry-after", "x-request-id", "www-authenticate"];

/**
 * Backend-for-frontend proxy: the browser only holds a sealed HttpOnly cookie. The access token is
 * attached server-side, so it never appears in JavaScript, HTML, logs, or URLs.
 */
async function handle(request: NextRequest, context: {params: Promise<{path: string[]}>}) {
  const config = getAuthConfig();
  const {path} = await context.params;
  const target = path.join("/");
  if (request.method === "GET" && PUBLIC_HEALTH.has(target)) {
    // Health endpoints are public on the API and expose only up/down states; no session needed.
    const health = await fetch(`${config.apiInternalUrl}/${target}`, {cache: "no-store", redirect: "manual"}).catch(() => null);
    if (!health) return NextResponse.json({error: {code: "bad_gateway", message: "API unavailable"}}, {status: 502});
    return new NextResponse(health.body, {status: health.status, headers: {"Content-Type": health.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store"}});
  }
  if (!target.startsWith("api/v1/") || target.includes("..") || target.startsWith("api/v1/auth/local")) {
    return NextResponse.json({error: {code: "not_found", message: "Not found"}}, {status: 404});
  }
  const session = await openSession(request.cookies.get(SESSION_COOKIE)?.value, config.sessionSecret);
  if (!session) return NextResponse.json({error: {code: "session_expired", message: "Session expired"}}, {status: 401, headers: {"x-session-expired": "1"}});
  const mutating = !["GET", "HEAD"].includes(request.method);
  if (mutating && !csrfValid(request.headers.get(CSRF_HEADER), session, request.headers.get("origin"), config.appBaseUrl)) {
    return NextResponse.json({error: {code: "csrf", message: "Invalid CSRF token"}}, {status: 403});
  }
  const headers = new Headers();
  for (const name of FORWARD_REQUEST) { const value = request.headers.get(name); if (value) headers.set(name, value); }
  headers.set("authorization", `Bearer ${session.accessToken}`);
  const upstream = await fetch(`${config.apiInternalUrl}/${target}${request.nextUrl.search}`, {
    method: request.method,
    headers,
    body: mutating ? await request.arrayBuffer() : undefined,
    cache: "no-store",
    redirect: "manual",
  }).catch(() => null);
  if (!upstream) return NextResponse.json({error: {code: "bad_gateway", message: "API unavailable"}}, {status: 502});
  const out = new Headers({"Cache-Control": "no-store"});
  for (const name of FORWARD_RESPONSE) { const value = upstream.headers.get(name); if (value) out.set(name, value); }
  if (upstream.status === 401) out.set("x-session-expired", "1");
  return new NextResponse(upstream.status === 204 ? null : upstream.body, {status: upstream.status, headers: out});
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const DELETE = handle;
