import type { NextConfig } from "next";

const dev = process.env.NODE_ENV !== "production";
const apiOrigin = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8300";
const wsOrigin = apiOrigin.replace(/^http/, "ws");
const secureSite = (process.env.APP_BASE_URL ?? "").startsWith("https://");

// Practical CSP: Next.js needs inline bootstrap scripts, so script-src allows 'unsafe-inline'
// (and 'unsafe-eval' only in development). Everything else is locked to the application origin;
// the browser may open a WebSocket to the API origin only.
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  `connect-src 'self' ${wsOrigin}${dev ? " ws://localhost:3300" : ""}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const securityHeaders = [
  {key: "Content-Security-Policy", value: csp},
  {key: "X-Content-Type-Options", value: "nosniff"},
  {key: "X-Frame-Options", value: "DENY"},
  {key: "Referrer-Policy", value: "strict-origin-when-cross-origin"},
  {key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()"},
  ...(secureSite ? [{key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains"}] : []),
];

const nextConfig: NextConfig = {
  // The Docker image sets NEXT_OUTPUT=standalone; local builds (esp. on Windows,
  // where standalone tracing needs symlink rights) use the default output.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    return [{source: "/:path*", headers: securityHeaders}];
  },
};

export default nextConfig;
