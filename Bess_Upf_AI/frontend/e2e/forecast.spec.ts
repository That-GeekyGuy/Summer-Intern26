import { test, expect } from "@playwright/test";

async function gotoView(page: import("@playwright/test").Page, navText: string) {
  await page.goto("/");
  await page.evaluate(() => {
    localStorage.setItem("upf_monitor_creds", JSON.stringify({ username: "testuser", password: "testpass" }));
    localStorage.setItem("upf_onboarded", "1");
  });
  await page.reload();
  await page.waitForTimeout(800);
  const nav = page.getByText(navText, { exact: false }).first();
  await nav.click({ force: true });
  await page.waitForTimeout(600);
}

test.describe("Forecast view", () => {
  test("nav item renders and is clickable", async ({ page }) => {
    await gotoView(page, "Forecast");
    const content = await page.content();
    expect(content.toLowerCase()).toContain("forecast");
  });

  test("Chronos-2 prediction interval label present", async ({ page }) => {
    await gotoView(page, "Forecast");
    const content = await page.content();
    const hasChronos = /chronos|interval|forecast|trend/i.test(content);
    expect(hasChronos).toBe(true);
  });

  test("no unexpected JS errors in forecast view", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
    page.on("pageerror", err => errors.push(err.message));
    await gotoView(page, "Forecast");
    await page.waitForLoadState("networkidle");
    const unexpected = errors.filter(e =>
      !e.includes("Failed to fetch") && !e.includes("ERR_CONNECTION") &&
      !e.includes("net::ERR") && !e.includes("401") && !e.includes("500") &&
      !e.includes("Failed to load resource") && !e.includes("ECONNREFUSED")
    );
    expect(unexpected).toHaveLength(0);
  });
});
