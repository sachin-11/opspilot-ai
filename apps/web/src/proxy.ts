import { NextResponse, type NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { openSession, PUBLIC_PAGE_HEADER, safeReturnTo, SESSION_COOKIE } from "@/lib/auth/security";

const PUBLIC_PAGES = new Set(["/login", "/session-expired", "/access-denied", "/auth-error"]);

/** Server-side route protection: no valid sealed session, no application page. */
export async function proxy(request: NextRequest) {
  const {pathname, search} = request.nextUrl;
  // Tell the root layout whether this is a public auth page: those must render without the
  // application shell even when a (possibly unusable) session cookie is present.
  const headers = new Headers(request.headers);
  headers.delete(PUBLIC_PAGE_HEADER);
  if (PUBLIC_PAGES.has(pathname)) headers.set(PUBLIC_PAGE_HEADER, "1");
  if (PUBLIC_PAGES.has(pathname) || pathname.startsWith("/api/")) return NextResponse.next({request: {headers}});
  const config = getAuthConfig();
  const cookie = request.cookies.get(SESSION_COOKIE)?.value;
  if (await openSession(cookie, config.sessionSecret)) return NextResponse.next({request: {headers}});
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
