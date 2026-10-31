// Captures the screenshots used in docs/project-article.md from a running stack that already has
// demo data (see README "Demo data"). Not part of the test suite.
//
//   cd apps/web
//   E2E_BROWSER_CHANNEL=chrome node scripts/capture-screenshots.mjs
//
// It signs in as the development account (owner@example.com) in the workspace that holds the demo
// data and writes PNGs to ../../docs/images. Run it against a development stack only.
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3300";
const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "docs", "images");
const EMAIL = process.env.SHOT_USER_EMAIL ?? "owner@example.com";
const PASSWORD = process.env.SHOT_USER_PASSWORD ?? "owner-local-password";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ channel: process.env.E2E_BROWSER_CHANNEL || undefined });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const page = await context.newPage();

async function settle(ms = 1800) {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await page.waitForTimeout(ms);
}

async function shot(name) {
  await settle();
  await page.screenshot({ path: join(OUT, `${name}.png`) });
  console.log("saved", name);
}

async function scrollToHeading(text, offset = 90) {
  const heading = page.getByRole("heading", { name: text, exact: true }).first();
  await heading.scrollIntoViewIfNeeded();
  await page.evaluate((o) => window.scrollBy(0, -o), offset);
}

async function scrollToText(text, offset = 300) {
  await page.getByText(text, { exact: true }).first().scrollIntoViewIfNeeded();
  await page.evaluate((o) => window.scrollBy(0, -o), offset);
}

async function openIncident(titleStart) {
  await page.goto(`${BASE}/incidents`);
  await settle();
  await page.locator("a[href^='/incidents/']", { hasText: titleStart }).first().click();
  await page.getByRole("heading", { name: "Timeline" }).waitFor();
  await settle(2500);
}

// 01: the sign-in page, before anyone is signed in.
await page.goto(`${BASE}/login`);
await shot("01-sign-in");

await page.getByRole("link", { name: "Sign in with SSO" }).click();
await page.locator("#username").fill(EMAIL);
await page.locator("#password").fill(PASSWORD);
await page.locator("#kc-login").click();
await page.getByRole("heading", { name: "Incident overview" }).waitFor();

await shot("02-overview");

await page.goto(`${BASE}/incidents`);
await shot("03-incidents");

await openIncident("Checkout API 5xx");
await page.evaluate(() => window.scrollTo(0, 0));
await shot("04-incident-detail");

// The timeline continues below the fold: show its later events (status changes, notes, mitigation).
await page.evaluate(() => window.scrollTo(0, 560));
await shot("05-incident-timeline");

// Investigation reports are long, so capture each part in a tall window.
async function tall(height, fn) {
  await page.setViewportSize({ width: 1440, height });
  await fn();
  await page.setViewportSize({ width: 1440, height: 900 });
}
await openIncident("Checkout API 5xx");
await tall(1100, async () => {
  await scrollToHeading("Incident investigation report", 20);
  await shot("06-investigation-steps");
});
await tall(1500, async () => {
  await scrollToHeading("Investigation report", 20);
  await shot("07-investigation-report");
});
await page.getByRole("heading", { name: "Human review", exact: true }).evaluate((el) => el.scrollIntoView({ block: "start" }));
await page.evaluate(() => window.scrollBy(0, -60));
await shot("08-human-review-gate");

// After review the outcome lands on the incident timeline.
await openIncident("Notification queue lag");
await tall(1000, async () => {
  await scrollToText("Investigation rejected during human review", 420);
  await shot("09-investigation-rejected");
});

await openIncident("Payments worker database");
await tall(1000, async () => {
  await scrollToText("Investigation approved and completed", 420);
  await shot("10-incident-resolved-lifecycle");
});

for (const [name, path] of [
  ["11-pipeline-overview", "/observability"],
  ["12-pipeline-sources", "/observability/sources"],
  ["13-pipeline-signals", "/observability/signals"],
  ["14-pipeline-dead-letters", "/observability/dead-letters"],
  ["15-audit-log", "/settings/audit"],
  ["16-members-and-roles", "/settings/members"],
  ["17-services", "/settings/services"],
]) {
  await page.goto(`${BASE}${path}`);
  await shot(name);
}

await browser.close();
