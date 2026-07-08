# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: shell.spec.ts >> App Shell >> topbar/header element renders
- Location: e2e\shell.spec.ts:25:3

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: locator('header, [role=\'banner\'], nav').first()
Expected: visible
Timeout: 5000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 5000ms
  - waiting for locator('header, [role=\'banner\'], nav').first()

```

```yaml
- img
- heading "BESS-UPF Intelligence" [level=1]
- paragraph: 5G User Plane Function Monitor
- text: Username
- textbox "Username"
- text: Password
- textbox "Password"
- button "Sign in to Dashboard"
```

# Test source

```ts
  1  | import { test, expect } from "@playwright/test";
  2  | 
  3  | async function injectCredentials(page: import("@playwright/test").Page) {
  4  |   await page.goto("/");
  5  |   await page.evaluate(() => {
  6  |     // Key must match CREDS_KEY in src/api/client.ts
  7  |     localStorage.setItem("upf_monitor_creds", JSON.stringify({
  8  |       username: "testuser",
  9  |       password: "testpass"
  10 |     }));
  11 |   });
  12 |   await page.reload();
  13 |   await page.waitForTimeout(800);
  14 | }
  15 | 
  16 | test.describe("App Shell", () => {
  17 |   test("navigation keywords present in DOM", async ({ page }) => {
  18 |     await injectCredentials(page);
  19 |     const content = await page.content();
  20 |     const found = ["overview", "anomal", "forecast", "chat", "insight"]
  21 |       .some(kw => content.toLowerCase().includes(kw));
  22 |     expect(found, "No navigation keywords found in DOM after login").toBe(true);
  23 |   });
  24 | 
  25 |   test("topbar/header element renders", async ({ page }) => {
  26 |     await injectCredentials(page);
  27 |     const header = page.locator("header, [role='banner'], nav").first();
> 28 |     await expect(header).toBeVisible({ timeout: 5000 });
     |                          ^ Error: expect(locator).toBeVisible() failed
  29 |   });
  30 | 
  31 |   test("no unexpected console errors in shell", async ({ page }) => {
  32 |     const errors: string[] = [];
  33 |     page.on("console", msg => { if (msg.type() === "error") errors.push(msg.text()); });
  34 |     page.on("pageerror", err => errors.push(err.message));
  35 |     await injectCredentials(page);
  36 |     await page.waitForLoadState("networkidle");
  37 |     const unexpected = errors.filter(e =>
  38 |       !e.includes("Failed to fetch") &&
  39 |       !e.includes("ERR_CONNECTION_REFUSED") &&
  40 |       !e.includes("net::ERR") &&
  41 |       !e.includes("401") &&
  42 |       !e.includes("500") &&
  43 |       !e.includes("Internal Server Error") &&
  44 |       !e.includes("ECONNREFUSED") &&
  45 |       !e.includes("Failed to load resource")
  46 |     );
  47 |     expect(unexpected, `Unexpected JS errors: ${unexpected.join(", ")}`).toHaveLength(0);
  48 |   });
  49 | });
  50 | 
```