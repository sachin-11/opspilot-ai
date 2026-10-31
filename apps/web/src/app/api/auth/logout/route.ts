import { NextResponse, type NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { csrfValid, CSRF_HEADER, openSession, SESSION_COOKIE } from "@/lib/auth/security";

/**
 * CSRF-protected logout: clears the sealed session cookie and, when the identity provider has an
 * end-session endpoint configured, tells the browser where to go to end that session too.
 * No token is put in the URL; the provider identifies the client by client_id and shows its own
 * confirmation before returning to the login page.
 */
export async function POST(request: NextRequest) {
  const config = getAuthConfig();
  const session = await openSession(request.cookies.get(SESSION_COOKIE)?.value, config.sessionSecret);
  if (session && !csrfValid(request.headers.get(CSRF_HEADER), session, request.headers.get("origin"), config.appBaseUrl)) {
    return NextResponse.json({error: {code: "csrf", message: "Invalid CSRF token"}}, {status: 403});
  }
  let redirectTo: string | null = null;
  if (config.endSessionUrl && config.mode === "oidc") {
    const url = new URL(config.endSessionUrl);
    url.searchParams.set("client_id", config.clientId);
    url.searchParams.set(config.endSessionRedirectParam, `${config.appBaseUrl}/login`);
    redirectTo = url.toString();
  }
  const response = NextResponse.json({ok: true, redirectTo}, {headers: {"Cache-Control": "no-store"}});
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
