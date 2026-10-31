import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests drive a real browser against a running stack (they do not start one):
 *
 *   docker compose --profile idp up -d      # plus the OIDC settings from the README
 *   E2E_BROWSER_CHANNEL=chrome pnpm e2e     # use the installed Chrome instead of downloading one
 *
 * The suite signs in through Keycloak, so the stack must run with AUTH_PROVIDER=oidc.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3300",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: process.env.E2E_BROWSER_CHANNEL || undefined },
    },
  ],
});
