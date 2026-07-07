import { test, expect } from "@playwright/test";

async function injectCredentials(page: import("@playwright/test").Page) {
  await page.goto("/");
  await page.evaluate(() => {
    // Key must match CREDS_KEY in src/api/client.ts
    localStorage.setItem("upf_monitor_creds", JSON.stringify({
      username: "testuser",
      password: "testpass"
    }));
  });
  await page.reload();
  await page.waitForTimeout(800);
}

test.describe("App Shell", () => {
  test("navigation keywords present in DOM", async ({ page }) => {
    await injectCredentials(page);
    const content = await page.content();
    const found = ["overview", "anomal", "forecast", "chat", "benchmark"]
      .some(kw => content.toLowerCase().includes(kw));
    expect(found, "No navigation keywords found in DOM after login").toBe(true);
  });

  test("topbar/header element renders", async ({ page }) => {
    await injectCredentials(page);
    const header = page.locator("header, [role='banner'], nav").first();
    await expect(header).toBeVisible({ timeout: 5000 });
  });

  test("no unexpected console errors in shell", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
    page.on("pageerror", err => errors.push(err.message));
    await injectCredentials(page);
    await page.waitForLoadState("networkidle");
    const unexpected = errors.filter(e =>
      !e.includes("Failed to fetch") &&
      !e.includes("ERR_CONNECTION_REFUSED") &&
      !e.includes("net::ERR") &&
      !e.includes("401") &&
      !e.includes("500") &&
      !e.includes("Internal Server Error") &&
      !e.includes("ECONNREFUSED") &&
      !e.includes("Failed to load resource")
    );
    expect(unexpected, `Unexpected JS errors: ${unexpected.join(", ")}`).toHaveLength(0);
  });
});
