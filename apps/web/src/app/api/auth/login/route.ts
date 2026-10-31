import { NextResponse, type NextRequest } from "next/server";
import { getAuthConfig } from "@/lib/auth/config";
import { cookieOptions, pkceChallenge, randomToken, safeReturnTo, seal, TX_COOKIE } from "@/lib/auth/security";

/** Starts the OIDC Authorization Code flow with PKCE, state, and nonce. */
export async function GET(request: NextRequest) {
  const config = getAuthConfig();
  const state = randomToken();
  const nonce = randomToken();
  const verifier = randomToken(48);
  const returnTo = safeReturnTo(request.nextUrl.searchParams.get("returnTo"));
  const url = new URL(config.authorizationUrl);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", config.clientId);
  url.searchParams.set("redirect_uri", config.redirectUri);
  url.searchParams.set("scope", config.scope);
  url.searchParams.set("state", state);
  url.searchParams.set("nonce", nonce);
  url.searchParams.set("code_challenge", await pkceChallenge(verifier));
  url.searchParams.set("code_challenge_method", "S256");
  const hint = request.nextUrl.searchParams.get("login_hint");
  if (config.mode === "local" && hint) url.searchParams.set("login_hint", hint.slice(0, 40));
  const response = NextResponse.redirect(url);
  response.cookies.set(TX_COOKIE, await seal({state, nonce, verifier, returnTo}, config.sessionSecret, 600), cookieOptions(config.secureCookies, 600));
  response.headers.set("Cache-Control", "no-store");
  return response;
}
