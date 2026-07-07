# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: chat.spec.ts >> Chat / Analysis view >> chat nav item navigates to view
- Location: e2e\chat.spec.ts:16:3

# Error details

```
Test timeout of 30000ms exceeded.
```

```
Error: locator.click: Test timeout of 30000ms exceeded.
Call log:
  - waiting for getByText('Analysis').first()

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
> 11 |   await page.getByText(navText, { exact: false }).first().click({ force: true });
     |                                                           ^ Error: locator.click: Test timeout of 30000ms exceeded.
  12 |   await page.waitForTimeout(600);
  13 | }
  14 | 
  15 | test.describe("Chat / Analysis view", () => {
  16 |   test("chat nav item navigates to view", async ({ page }) => {
  17 |     await gotoView(page, "Analysis");
  18 |     const content = await page.content();
  19 |     expect(content.toLowerCase()).toMatch(/analy|chat|ask|query/);
  20 |   });
  21 | 
  22 |   test("message input or send button present", async ({ page }) => {
  23 |     await gotoView(page, "Analysis");
  24 |     const content = await page.content();
  25 |     const hasInput = /textarea|input|send|submit|ask/i.test(content);
  26 |     expect(hasInput).toBe(true);
  27 |   });
  28 | 
  29 |   test("no unexpected JS errors in chat view", async ({ page }) => {
  30 |     const errors: string[] = [];
  31 |     page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
  32 |     page.on("pageerror", err => errors.push(err.message));
  33 |     await gotoView(page, "Analysis");
  34 |     await page.waitForLoadState("networkidle");
  35 |     const unexpected = errors.filter(e =>
  36 |       !e.includes("Failed to fetch") && !e.includes("ERR_CONNECTION") &&
  37 |       !e.includes("net::ERR") && !e.includes("401") && !e.includes("500") &&
  38 |       !e.includes("Failed to load resource") && !e.includes("ECONNREFUSED")
  39 |     );
  40 |     expect(unexpected).toHaveLength(0);
  41 |   });
  42 | });
  43 | 
```