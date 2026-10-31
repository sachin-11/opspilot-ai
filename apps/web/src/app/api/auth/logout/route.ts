import { NextResponse, type NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { csrfValid, CSRF_HEADER, openSession, SESSION_COOKIE } from "@/lib/auth/security";

/** CSRF-protected logout: clears the sealed session cookie. */
export async function POST(request: NextRequest) {
  const config = getAuthConfig();
  const session = await openSession(request.cookies.get(SESSION_COOKIE)?.value, config.sessionSecret);
  if (session && !csrfValid(request.headers.get(CSRF_HEADER), session, request.headers.get("origin"), config.appBaseUrl)) {
    return NextResponse.json({error: {code: "csrf", message: "Invalid CSRF token"}}, {status: 403});
  }
  const response = NextResponse.json({ok: true}, {headers: {"Cache-Control": "no-store"}});
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
