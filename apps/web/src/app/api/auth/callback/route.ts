import { createRemoteJWKSet, jwtVerify } from "jose";
import { NextResponse, type NextRequest } from "next/server";
import { z } from "zod";
import { getAuthConfig, type AuthConfig } from "@/lib/auth/config";
import { cookieOptions, open, randomToken, seal, SESSION_COOKIE, TX_COOKIE, timingSafeEqual, type Transaction } from "@/lib/auth/security";

const tokenResponse = z.object({access_token: z.string().min(1), id_token: z.string().min(1), expires_in: z.number().positive().optional()});

function fail(config: AuthConfig, reason: string, path = "/auth-error") {
  const response = NextResponse.redirect(new URL(`${path}?reason=${encodeURIComponent(reason)}`, config.appBaseUrl));
  response.cookies.delete(TX_COOKIE);
  response.headers.set("Cache-Control", "no-store");
  return response;
}

async function verifyIdToken(config: AuthConfig, idToken: string) {
  if (config.mode === "local") {
    return jwtVerify(idToken, new TextEncoder().encode(config.localSigningKey ?? ""), {algorithms: ["HS256"], issuer: "opspilot-local", audience: "opspilot-api"});
  }
  const jwks = createRemoteJWKSet(new URL(config.jwksUrl ?? ""));
  return jwtVerify(idToken, jwks, {algorithms: ["RS256"], issuer: config.issuer ?? "", audience: config.clientId});
}

export async function GET(request: NextRequest) {
  const config = getAuthConfig();
  const params = request.nextUrl.searchParams;
  if (params.get("error")) return fail(config, params.get("error") === "access_denied" ? "denied" : "provider", params.get("error") === "access_denied" ? "/access-denied" : "/auth-error");
  const tx = await open<Transaction>(request.cookies.get(TX_COOKIE)?.value, config.sessionSecret);
  const code = params.get("code");
  const state = params.get("state");
  if (!tx || !code || !state || !timingSafeEqual(state, tx.state)) return fail(config, "state");
  let tokens: z.infer<typeof tokenResponse>;
  try {
    const exchange = await fetch(config.tokenUrl, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: new URLSearchParams({grant_type: "authorization_code", code, redirect_uri: config.redirectUri, client_id: config.clientId, code_verifier: tx.verifier}),
      cache: "no-store",
    });
    if (!exchange.ok) return fail(config, "token_exchange");
    tokens = tokenResponse.parse(await exchange.json());
    const {payload} = await verifyIdToken(config, tokens.id_token);
    if (typeof payload.nonce !== "string" || !timingSafeEqual(payload.nonce, tx.nonce) || typeof payload.sub !== "string") return fail(config, "nonce");
    const ttl = Math.min(tokens.expires_in ?? 900, 3600);
    const session = {
      sub: payload.sub,
      email: typeof payload.email === "string" ? payload.email : null,
      name: typeof payload.name === "string" ? payload.name : null,
      accessToken: tokens.access_token,
      expiresAt: Math.floor(Date.now() / 1000) + ttl,
      csrf: randomToken(),
    };
    const response = NextResponse.redirect(new URL(tx.returnTo, config.appBaseUrl));
    response.cookies.set(SESSION_COOKIE, await seal(session, config.sessionSecret, ttl), cookieOptions(config.secureCookies, ttl));
    response.cookies.delete(TX_COOKIE);
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch {
    return fail(config, "validation"); // details (and any token) are never reflected
  }
}
