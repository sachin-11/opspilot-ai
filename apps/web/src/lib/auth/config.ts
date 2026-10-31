import { z } from "zod";

/** Server-only authentication configuration. Fails fast on anything unsafe or missing. */
const schema = z.object({
  AUTH_MODE: z.enum(["local", "oidc"]).default("local"),
  APP_BASE_URL: z.string().url().default("http://localhost:3300"),
  API_INTERNAL_URL: z.string().url(),
  SESSION_SECRET: z.string().min(32, "SESSION_SECRET must be at least 32 characters"),
  OIDC_CLIENT_ID: z.string().min(1).default("opspilot-web"),
  OIDC_AUTHORIZATION_URL: z.string().url(),
  OIDC_TOKEN_URL: z.string().url(),
  OIDC_ISSUER: z.string().optional(),
  OIDC_JWKS_URL: z.string().url().optional(),
  OIDC_SCOPE: z.string().default("openid email profile"),
  // Local development provider only; never used in oidc mode.
  AUTH_LOCAL_SIGNING_KEY: z.string().optional(),
});

export type AuthConfig = {
  mode: "local" | "oidc";
  appBaseUrl: string;
  apiInternalUrl: string;
  sessionSecret: string;
  clientId: string;
  authorizationUrl: string;
  tokenUrl: string;
  issuer: string | null;
  jwksUrl: string | null;
  scope: string;
  localSigningKey: string | null;
  secureCookies: boolean;
  redirectUri: string;
};

export function getAuthConfig(env: Record<string, string | undefined> = process.env): AuthConfig {
  // Compose passes unset optional variables as empty strings; those count as unset.
  const present = Object.fromEntries(Object.entries(env).filter(([, value]) => value !== undefined && value !== ""));
  const withFallback = {...present, API_INTERNAL_URL: present.API_INTERNAL_URL ?? present.NEXT_PUBLIC_API_BASE_URL};
  const parsed = schema.safeParse(withFallback);
  if (!parsed.success) {
    throw new Error(`Invalid authentication configuration: ${parsed.error.issues.map((issue) => `${issue.path.join(".")}: ${issue.message}`).join("; ")}`);
  }
  const value = parsed.data;
  const base = new URL(value.APP_BASE_URL);
  const secure = base.protocol === "https:";
  if (!secure && !["localhost", "127.0.0.1"].includes(base.hostname)) {
    throw new Error("APP_BASE_URL must use https outside localhost");
  }
  if (value.AUTH_MODE === "oidc" && (!value.OIDC_ISSUER || !value.OIDC_JWKS_URL)) {
    throw new Error("OIDC_ISSUER and OIDC_JWKS_URL are required for oidc mode");
  }
  if (value.AUTH_MODE === "local" && (!value.AUTH_LOCAL_SIGNING_KEY || env.NODE_ENV === "production" && secure)) {
    throw new Error("local authentication is only available for plain-http development with AUTH_LOCAL_SIGNING_KEY");
  }
  return {
    mode: value.AUTH_MODE,
    appBaseUrl: base.origin,
    apiInternalUrl: value.API_INTERNAL_URL.replace(/\/+$/, ""),
    sessionSecret: value.SESSION_SECRET,
    clientId: value.OIDC_CLIENT_ID,
    authorizationUrl: value.OIDC_AUTHORIZATION_URL,
    tokenUrl: value.OIDC_TOKEN_URL,
    issuer: value.OIDC_ISSUER ?? null,
    jwksUrl: value.OIDC_JWKS_URL ?? null,
    scope: value.OIDC_SCOPE,
    localSigningKey: value.AUTH_LOCAL_SIGNING_KEY ?? null,
    secureCookies: secure,
    redirectUri: `${base.origin}/api/auth/callback`,
  };
}
