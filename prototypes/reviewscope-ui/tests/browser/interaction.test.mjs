import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium, firefox } from "playwright";
import { createServer } from "vite";

let server, browser, origin;
before(async () => {
  server = await createServer({ server: { host: "127.0.0.1", port: 0 }, logLevel: "error" });
  await server.listen();
  origin = server.resolvedUrls.local[0];
  const engine = process.env.BROWSER === "firefox" ? firefox : chromium;
  browser = await engine.launch({ executablePath: process.env.BROWSER_PATH || undefined });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

test("drag moves the card and relation; release leaves the explicit review link usable", async () => {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  try {
    await page.goto(origin);
    const card = page.locator(".review-slip").nth(1),
      handle = card.getByRole("button", { name: "Переместить #02" });
    await handle.hover();
    const box = await handle.boundingBox(),
      path = page.locator(".threads path").first();
    const initial = await path.getAttribute("d");
    await page.mouse.move(box.x + 10, box.y + 10);
    await page.mouse.down();
    await page.mouse.move(box.x - 68, box.y + 44, { steps: 8 });
    await page.mouse.up();
    assert.equal(new URL(page.url()).hash, "");
    assert.equal(await handle.getAttribute("aria-expanded"), "false");
    assert.notEqual(await path.getAttribute("d"), initial);
    await handle.click();
    await page.getByRole("button", { name: "Влево", exact: true }).click();
    await page.getByRole("button", { name: "Готово" }).click();
    await card.locator(".slip-trigger").hover();
    await card.getByRole("link").click();
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#02/);
  } finally {
    await page.close();
  }
});
test("touch disclosure, explicit close and keyboard entry", async () => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  try {
    await page.goto(origin);
    const card = page.locator(".review-slip").last(),
      header = card.locator(".slip-trigger"),
      link = card.getByRole("link");
    assert.equal(await link.count(), 0);
    await header.tap();
    assert.equal(await header.getAttribute("aria-expanded"), "true");
    await link.waitFor();
    await header.tap();
    assert.equal(await header.getAttribute("aria-expanded"), "false");
    assert.equal(await link.count(), 0);
    await page.locator(".next-scene").focus();
    await header.focus();
    await link.waitFor();
    await page.keyboard.press("Enter");
    assert.equal(await link.count(), 0);
    await page.keyboard.press("Enter");
    await page.keyboard.press("Tab");
    assert.equal(await link.evaluate((e) => e === document.activeElement), true);
    await page.keyboard.press("Enter");
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#03/);
  } finally {
    await page.close();
  }
});
test("history restores the landing scene and research context per entry", async () => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  try {
    await page.goto(origin + "#how");
    await page.getByRole("button", { name: /Сравнить изменения/ }).click();
    await page.getByRole("link", { name: "Открыть изменения" }).scrollIntoViewIfNeeded();
    const before = await page.evaluate(() => scrollY);
    await page.getByRole("link", { name: "Открыть изменения" }).click();
    await page.goBack();
    await page.getByRole("link", { name: "Открыть изменения" }).waitFor();
    const returned = await page.evaluate(() => scrollY);
    assert.ok(Math.abs(before - returned) < 12, `left at ${before}, returned to ${returned}`);
    await page.goForward();
    await page.locator(".change-summary").waitFor();
    await page.getByRole("button", { name: "Октябрь" }).click();
    await page.locator(".source-row").first().click();
    await page.getByRole("textbox").fill("#");
    await page.getByRole("combobox", { name: /Сравнить/ }).selectOption("11");
    await page.getByRole("button", { name: "Открыть меню" }).click();
    await page.getByRole("link", { name: "Отчёт", exact: true }).click();
    await page.getByRole("button", { name: "Сентябрь" }).click();
    await page.goBack();
    await page.locator(".evidence").waitFor();
    assert.equal(await page.getByRole("textbox").inputValue(), "#");
    assert.equal(await page.getByRole("combobox", { name: /Сравнить/ }).inputValue(), "11");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#07/);
    assert.equal(
      await page.getByRole("button", { name: "Октябрь" }).getAttribute("aria-pressed"),
      "true",
    );
  } finally {
    await page.close();
  }
});
