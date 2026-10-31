import { NextResponse, type NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { openSession, safeReturnTo, SESSION_COOKIE } from "@/lib/auth/security";

const PUBLIC_PAGES = new Set(["/login", "/session-expired", "/access-denied", "/auth-error"]);

/** Server-side route protection: no valid sealed session, no application page. */
export async function proxy(request: NextRequest) {
  const {pathname, search} = request.nextUrl;
  if (PUBLIC_PAGES.has(pathname) || pathname.startsWith("/api/")) return NextResponse.next();
  const config = getAuthConfig();
  const cookie = request.cookies.get(SESSION_COOKIE)?.value;
  if (await openSession(cookie, config.sessionSecret)) return NextResponse.next();
  const target = cookie ? "/session-expired" : "/login";
  const url = new URL(target, config.appBaseUrl);
  url.searchParams.set("returnTo", safeReturnTo(`${pathname}${search}`));
  const response = NextResponse.redirect(url);
  if (cookie) response.cookies.delete(SESSION_COOKIE);
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
