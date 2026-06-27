import { test, expect } from "@playwright/test";

test.describe("Login", () => {
  test("renders login form", async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator("input[type='password'], input[placeholder*='password' i]").first()
    ).toBeVisible({ timeout: 10000 });
  });

  test("no unexpected JS errors on load", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
    page.on("pageerror", err => errors.push(err.message));
    await page.goto("/");
    await page.waitForLoadState("networkidle");
    const unexpected = errors.filter(e =>
      !e.includes("Failed to fetch") &&
      !e.includes("ERR_CONNECTION_REFUSED") &&
      !e.includes("net::ERR")
    );
    expect(unexpected, `JS errors: ${unexpected.join(", ")}`).toHaveLength(0);
  });

  test("page has a title", async ({ page }) => {
    await page.goto("/");
    expect((await page.title()).length).toBeGreaterThan(0);
  });
});
