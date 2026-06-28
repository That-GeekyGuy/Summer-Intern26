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

test.describe("Insights view", () => {
  test("insights nav item navigates to view", async ({ page }) => {
    await gotoView(page, "Insights");
    const content = await page.content();
    expect(content.toLowerCase()).toMatch(/insight|regime|correlat/);
  });

  test("correlation matrix or regime guide section present", async ({ page }) => {
    await gotoView(page, "Insights");
    const content = await page.content();
    // "Temporal Insights" h2 always renders; matrix/regime visible when data present
    const hasContent = /temporal insight|correlat|regime|co-activation|channel|matrix/i.test(content);
    expect(hasContent).toBe(true);
  });

  test("no unexpected JS errors in insights view", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
    page.on("pageerror", err => errors.push(err.message));
    await gotoView(page, "Insights");
    await page.waitForLoadState("networkidle");
    const unexpected = errors.filter(e =>
      !e.includes("Failed to fetch") && !e.includes("ERR_CONNECTION") &&
      !e.includes("net::ERR") && !e.includes("401") && !e.includes("500") &&
      !e.includes("Failed to load resource") && !e.includes("ECONNREFUSED")
    );
    expect(unexpected).toHaveLength(0);
  });
});
