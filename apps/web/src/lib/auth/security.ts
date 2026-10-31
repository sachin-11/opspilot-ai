import { EncryptJWT, jwtDecrypt } from "jose";

export const SESSION_COOKIE = "opspilot_session";
export const TX_COOKIE = "opspilot_oidc_tx";
export const CSRF_HEADER = "x-csrf-token";

export type Session = {
  sub: string;
  email: string | null;
  name: string | null;
  accessToken: string;
  expiresAt: number; // epoch seconds
  csrf: string;
};

export type Transaction = {state: string; nonce: string; verifier: string; returnTo: string};

const encoder = new TextEncoder();

function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

export function randomToken(bytes = 32): string {
  return toBase64Url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(verifier));
  return toBase64Url(new Uint8Array(digest));
}

async function key(secret: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(secret)));
}

/** Sealed (encrypted + authenticated) cookie payloads: tokens never reach browser JavaScript. */
export async function seal(payload: Record<string, unknown>, secret: string, ttlSeconds: number): Promise<string> {
  return new EncryptJWT(payload)
    .setProtectedHeader({alg: "dir", enc: "A256GCM"})
    .setIssuedAt()
    .setExpirationTime(`${ttlSeconds}s`)
    .encrypt(await key(secret));
}

export async function open<T>(token: string | undefined, secret: string): Promise<T | null> {
  if (!token) return null;
  try {
    const {payload} = await jwtDecrypt(token, await key(secret));
    return payload as unknown as T;
  } catch {
    return null; // tampered, wrong key, or expired
  }
}

export async function openSession(token: string | undefined, secret: string): Promise<Session | null> {
  const session = await open<Session>(token, secret);
  if (!session || typeof session.accessToken !== "string" || session.expiresAt * 1000 <= Date.now()) return null;
  return session;
}

/** Only same-origin application paths are valid return URLs (no open redirects). */
export function safeReturnTo(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\") || /[\u0000-\u001f]/.test(value)) return "/";
  try {
    const parsed = new URL(value, "http://app.invalid");
    if (parsed.origin !== "http://app.invalid" || parsed.pathname.startsWith("/api/")) return "/";
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return "/";
  }
}

export function timingSafeEqual(a: string, b: string): boolean {
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  let diff = left.length ^ right.length;
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) diff |= (left[index] ?? 0) ^ (right[index] ?? 0);
  return diff === 0;
}

/** Cookie-authenticated mutations need a matching CSRF header and a same-origin Origin. */
export function csrfValid(headerValue: string | null, session: Session, originHeader: string | null, appOrigin: string): boolean {
  if (!headerValue || !timingSafeEqual(headerValue, session.csrf)) return false;
  return originHeader === null ? false : originHeader === appOrigin;
}

export function cookieOptions(secure: boolean, maxAgeSeconds: number) {
  return {httpOnly: true, secure, sameSite: "lax" as const, path: "/", maxAge: maxAgeSeconds};
}
