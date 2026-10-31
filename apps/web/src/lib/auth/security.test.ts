import { describe, expect, it } from "vitest";
import { getAuthConfig } from "./config";
import { csrfValid, open, openSession, pkceChallenge, safeReturnTo, seal, timingSafeEqual, type Session } from "./security";

const SECRET = "x".repeat(40);
const session: Session = {sub: "user-1", email: "a@example.test", name: "A", accessToken: "access-token-value", expiresAt: Math.floor(Date.now() / 1000) + 600, csrf: "csrf-token-value"};

describe("sealed cookies", () => {
  it("round-trips and never exposes the token in the cookie text", async () => {
    const sealed = await seal(session, SECRET, 600);
    expect(sealed).not.toContain("access-token-value");
    expect(sealed.split(".")).toHaveLength(5); // compact JWE: encrypted, not merely signed
    expect(await openSession(sealed, SECRET)).toMatchObject({sub: "user-1", accessToken: "access-token-value"});
  });

  it("rejects tampered, wrong-key, expired, and empty cookies", async () => {
    const sealed = await seal(session, SECRET, 600);
    expect(await open(sealed.slice(0, -4) + "AAAA", SECRET)).toBeNull();
    expect(await open(sealed, "y".repeat(40))).toBeNull();
    expect(await open(undefined, SECRET)).toBeNull();
    expect(await open("garbage", SECRET)).toBeNull();
    const stale = await seal({...session, expiresAt: Math.floor(Date.now() / 1000) - 5}, SECRET, 600);
    expect(await openSession(stale, SECRET)).toBeNull();
  });
});

describe("PKCE", () => {
  it("matches the RFC 7636 test vector", async () => {
    expect(await pkceChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });
});

describe("safe return URLs", () => {
  it.each([
    ["/incidents?x=1", "/incidents?x=1"],
    ["/", "/"],
    [null, "/"],
    ["https://evil.example/x", "/"],
    ["//evil.example/x", "/"],
    ["/\\evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["/api/auth/logout", "/"],
    ["/ok\nSet-Cookie: a=b", "/"],
  ])("%s -> %s", (input, expected) => {
    expect(safeReturnTo(input)).toBe(expected);
  });
});

describe("CSRF", () => {
  it("requires a matching token and a same-origin Origin header", () => {
    expect(csrfValid("csrf-token-value", session, "http://localhost:3300", "http://localhost:3300")).toBe(true);
    expect(csrfValid("wrong", session, "http://localhost:3300", "http://localhost:3300")).toBe(false);
    expect(csrfValid(null, session, "http://localhost:3300", "http://localhost:3300")).toBe(false);
    expect(csrfValid("csrf-token-value", session, "http://evil.example", "http://localhost:3300")).toBe(false);
    expect(csrfValid("csrf-token-value", session, null, "http://localhost:3300")).toBe(false);
    expect(timingSafeEqual("a", "ab")).toBe(false);
  });
});

describe("auth configuration", () => {
  const base = {
    API_INTERNAL_URL: "http://api:8000", SESSION_SECRET: SECRET, OIDC_AUTHORIZATION_URL: "http://localhost:8300/api/v1/auth/local/authorize",
    OIDC_TOKEN_URL: "http://api:8000/api/v1/auth/local/token", AUTH_LOCAL_SIGNING_KEY: "local-key", APP_BASE_URL: "http://localhost:3300",
  };
  it("accepts local development configuration and uses insecure cookies only on plain localhost", () => {
    const config = getAuthConfig({...base});
    expect(config).toMatchObject({mode: "local", secureCookies: false, redirectUri: "http://localhost:3300/api/auth/callback"});
  });
  it("treats empty-string variables (as passed by Compose) as unset", () => {
    expect(getAuthConfig({...base, OIDC_ISSUER: "", OIDC_JWKS_URL: ""}).mode).toBe("local");
  });
  it("fails fast on a weak secret, non-https remote origins, and missing OIDC settings", () => {
    expect(() => getAuthConfig({...base, SESSION_SECRET: "short"})).toThrow(/SESSION_SECRET/);
    expect(() => getAuthConfig({...base, APP_BASE_URL: "http://app.example.com"})).toThrow(/https/);
    expect(() => getAuthConfig({...base, AUTH_MODE: "oidc"})).toThrow(/OIDC_ISSUER/);
    expect(() => getAuthConfig({...base, AUTH_LOCAL_SIGNING_KEY: undefined})).toThrow(/local authentication/);
  });
  it("uses Secure cookies on https and refuses local mode in a production https deployment", () => {
    const oidc = {...base, AUTH_MODE: "oidc", APP_BASE_URL: "https://app.example.com", OIDC_ISSUER: "https://idp.example.com", OIDC_JWKS_URL: "https://idp.example.com/.well-known/jwks.json", AUTH_LOCAL_SIGNING_KEY: undefined};
    expect(getAuthConfig(oidc).secureCookies).toBe(true);
    expect(() => getAuthConfig({...base, APP_BASE_URL: "https://app.example.com", NODE_ENV: "production"})).toThrow(/local authentication/);
  });
});
