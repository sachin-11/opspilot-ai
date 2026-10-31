import { expect, type Page } from "@playwright/test";

// Throwaway development account from infra/keycloak/opspilot-realm.json (not a real credential).
export const E2E_EMAIL = process.env.E2E_USER_EMAIL ?? "e2e@example.com";
export const E2E_PASSWORD = process.env.E2E_USER_PASSWORD ?? "e2e-local-password";

/** Signs in through the real OIDC flow and makes sure the account has a workspace to work in. */
export async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
  await page.getByRole("link", { name: "Sign in with SSO" }).click();
  await page.locator("#username").fill(E2E_EMAIL);
  await page.locator("#password").fill(E2E_PASSWORD);
  await page.locator("#kc-login").click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();

  // A brand-new account has no workspace yet; on later runs it already has one.
  const createWorkspace = page.getByRole("button", { name: "Create workspace" });
  if (await createWorkspace.isVisible()) {
    await page.getByLabel("Workspace name").fill(`E2E ${Date.now()}`);
    await createWorkspace.click();
  }
  await expect(page.getByRole("heading", { name: "Incident overview" })).toBeVisible();
}
