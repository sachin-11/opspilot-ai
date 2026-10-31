import { NextResponse, type NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { openSession, SESSION_COOKIE } from "@/lib/auth/security";

/** Session metadata for the UI. Never returns the access token. */
export async function GET(request: NextRequest) {
  const config = getAuthConfig();
  const session = await openSession(request.cookies.get(SESSION_COOKIE)?.value, config.sessionSecret);
  const headers = {"Cache-Control": "no-store"};
  if (!session) return NextResponse.json({authenticated: false}, {status: 401, headers});
  return NextResponse.json({authenticated: true, user: {sub: session.sub, email: session.email, name: session.name}, csrfToken: session.csrf, expiresAt: session.expiresAt}, {headers});
}
