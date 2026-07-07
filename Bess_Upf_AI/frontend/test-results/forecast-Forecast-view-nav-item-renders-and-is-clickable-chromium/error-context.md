# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: forecast.spec.ts >> Forecast view >> nav item renders and is clickable
- Location: e2e\forecast.spec.ts:17:3

# Error details

```
Test timeout of 30000ms exceeded.
```

```
Error: locator.click: Test timeout of 30000ms exceeded.
Call log:
  - waiting for getByText('Forecast').first()

```

# Page snapshot

```yaml
- generic [ref=e16]:
  - generic [ref=e17]:
    - img [ref=e19]
    - heading "BESS-UPF Intelligence" [level=1] [ref=e21]
    - paragraph [ref=e22]: 5G User Plane Function Monitor
  - generic [ref=e23]:
    - generic [ref=e24]:
      - text: Username
      - textbox "Username" [ref=e25]
    - generic [ref=e26]:
      - text: Password
      - textbox "Password" [ref=e27]
    - button "Sign in to Dashboard" [ref=e28] [cursor=pointer]
```

# Test source

```ts
  1  | import { test, expect } from "@playwright/test";
  2  | 
  3  | async function gotoView(page: import("@playwright/test").Page, navText: string) {
  4  |   await page.goto("/");
  5  |   await page.evaluate(() => {
  6  |     localStorage.setItem("upf_monitor_creds", JSON.stringify({ username: "testuser", password: "testpass" }));
  7  |     localStorage.setItem("upf_onboarded", "1");
  8  |   });
  9  |   await page.reload();
  10 |   await page.waitForTimeout(800);
  11 |   const nav = page.getByText(navText, { exact: false }).first();
> 12 |   await nav.click({ force: true });
     |             ^ Error: locator.click: Test timeout of 30000ms exceeded.
  13 |   await page.waitForTimeout(600);
  14 | }
  15 | 
  16 | test.describe("Forecast view", () => {
  17 |   test("nav item renders and is clickable", async ({ page }) => {
  18 |     await gotoView(page, "Forecast");
  19 |     const content = await page.content();
  20 |     expect(content.toLowerCase()).toContain("forecast");
  21 |   });
  22 | 
  23 |   test("Chronos-2 prediction interval label present", async ({ page }) => {
  24 |     await gotoView(page, "Forecast");
  25 |     const content = await page.content();
  26 |     const hasChronos = /chronos|interval|forecast|trend/i.test(content);
  27 |     expect(hasChronos).toBe(true);
  28 |   });
  29 | 
  30 |   test("no unexpected JS errors in forecast view", async ({ page }) => {
  31 |     const errors: string[] = [];
  32 |     page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
  33 |     page.on("pageerror", err => errors.push(err.message));
  34 |     await gotoView(page, "Forecast");
  35 |     await page.waitForLoadState("networkidle");
  36 |     const unexpected = errors.filter(e =>
  37 |       !e.includes("Failed to fetch") && !e.includes("ERR_CONNECTION") &&
  38 |       !e.includes("net::ERR") && !e.includes("401") && !e.includes("500") &&
  39 |       !e.includes("Failed to load resource") && !e.includes("ECONNREFUSED")
  40 |     );
  41 |     expect(unexpected).toHaveLength(0);
  42 |   });
  43 | });
  44 | 
```