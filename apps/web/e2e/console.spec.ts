import { expect, test } from "@playwright/test";
import { signIn } from "./helpers";

test.describe("unauthenticated visitors", () => {
  test("are sent to the login page and cannot reach the API through the proxy", async ({ page, request }) => {
    for (const path of ["/", "/incidents", "/settings/members"]) {
      await page.goto(path);
      await expect(page).toHaveURL(/\/login/);
    }
    const workspaces = await request.get("/api/proxy/api/v1/auth/workspaces");
    expect(workspaces.status()).toBe(401);
    // Only the public health endpoints are forwarded without a session.
    const health = await request.get("/api/proxy/health/ready");
    expect(health.status()).toBe(200);
    expect((await health.json()).status).toBe("ready");
    expect((await request.get("/api/proxy/health/other")).status()).toBe(404);
  });
});

test.describe("signed-in operator", () => {
  test("signs in with OIDC, declares an incident, and signs out", async ({ page }) => {
    await signIn(page);
    // The status panel reads readiness through the BFF proxy.
    await expect(page.getByText("All systems ready")).toBeVisible();

    const title = `E2E incident ${Date.now()}`;
    await page.goto("/incidents/new");
    await page.getByLabel("Title").fill(title);
    await page.getByLabel("Severity").selectOption("sev2");
    await page.getByRole("button", { name: "Create incident" }).click();
    await expect(page).toHaveURL(/\/incidents\/[0-9a-f-]{36}$/);
    await expect(page.getByRole("heading", { name: title })).toBeVisible();

    await page.goto("/incidents");
    await expect(page.getByText(title)).toBeVisible();

    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/incidents");
    await expect(page).toHaveURL(/\/login/);
  });

  test("keeps Sign out reachable and the nav scrollable on a short window", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 600 });
    await signIn(page);
    const signOut = page.getByRole("button", { name: "Sign out" });
    await expect(signOut).toBeVisible();
    const box = await signOut.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.y + box!.height).toBeLessThanOrEqual(600);

    const nav = page.getByRole("navigation", { name: "Primary navigation" });
    const scrollable = await nav.evaluate((element) => element.scrollHeight > element.clientHeight);
    expect(scrollable).toBe(true);
    await nav.evaluate((element) => element.scrollTo(0, element.scrollHeight));
    await expect(signOut).toBeVisible();
  });

  test("does not overflow horizontally on a phone-sized screen", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await signIn(page);
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});
