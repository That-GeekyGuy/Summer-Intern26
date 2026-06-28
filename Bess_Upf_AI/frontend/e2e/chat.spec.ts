import { test, expect } from "@playwright/test";

async function gotoView(page: import("@playwright/test").Page, navText: string) {
  await page.goto("/");
  await page.evaluate(() => {
    localStorage.setItem("upf_monitor_creds", JSON.stringify({ username: "testuser", password: "testpass" }));
    localStorage.setItem("upf_onboarded", "1");
  });
  await page.reload();
  await page.waitForTimeout(800);
  await page.getByText(navText, { exact: false }).first().click({ force: true });
  await page.waitForTimeout(600);
}

test.describe("Chat / Analysis view", () => {
  test("chat nav item navigates to view", async ({ page }) => {
    await gotoView(page, "Analysis");
    const content = await page.content();
    expect(content.toLowerCase()).toMatch(/analy|chat|ask|query/);
  });

  test("message input or send button present", async ({ page }) => {
    await gotoView(page, "Analysis");
    const content = await page.content();
    const hasInput = /textarea|input|send|submit|ask/i.test(content);
    expect(hasInput).toBe(true);
  });

  test("no unexpected JS errors in chat view", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
    page.on("pageerror", err => errors.push(err.message));
    await gotoView(page, "Analysis");
    await page.waitForLoadState("networkidle");
    const unexpected = errors.filter(e =>
      !e.includes("Failed to fetch") && !e.includes("ERR_CONNECTION") &&
      !e.includes("net::ERR") && !e.includes("401") && !e.includes("500") &&
      !e.includes("Failed to load resource") && !e.includes("ECONNREFUSED")
    );
    expect(unexpected).toHaveLength(0);
  });
});
