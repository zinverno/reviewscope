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

test("a review swaps faces in place; dragging its quote updates the edge without following the link", async () => {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  try {
    await page.goto(origin);
    const link = page.getByRole("link", { name: "Открыть отзыв #02" }),
      card = link.locator("..");
    const rect = () => card.boundingBox();
    const initial = await rect();
    await link.hover();
    await page.waitForFunction(
      () => document.querySelector("#face-02-back").ariaHidden === "false",
    );
    assert.equal((await rect()).height, initial.height);
    assert.equal(await page.locator("#face-02-front").getAttribute("aria-hidden"), "true");
    const path = page.locator(".threads path").first(),
      before = await path.getAttribute("d");
    const drag = async () => {
      const b = await page.locator("#face-02-back").boundingBox();
      await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
      await page.mouse.down();
      await page.mouse.move(b.x + b.width / 2 - 85, b.y + b.height / 2 + 24, { steps: 8 });
    };
    await drag();
    await page.keyboard.press("Escape");
    await page.mouse.up();
    assert.ok(Math.abs((await rect()).x - initial.x) < 1);
    assert.equal(await page.locator("#face-02-back").getAttribute("aria-hidden"), "true");
    await page.mouse.move(0, 0);
    await link.hover();
    await drag();
    await page.mouse.up();
    assert.ok((await rect()).x < initial.x - 20);
    assert.notEqual(await path.getAttribute("d"), before);
    assert.equal(new URL(page.url()).hash, "");
    await link.click();
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#02/);
  } finally {
    await page.close();
  }
});
test("touch uses two taps; move mode and its arrow controls do not navigate", async () => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  try {
    await page.goto(origin);
    const link = page.getByRole("link", { name: "Открыть отзыв #03" });
    await link.tap();
    assert.equal(new URL(page.url()).hash, "");
    assert.equal(await page.locator("#face-03-back").getAttribute("aria-hidden"), "false");
    await link.tap();
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#03/);
    await page.goBack();
    await page.getByRole("button", { name: "Перемещение", exact: true }).tap();
    await link.locator(".slip-arrow").tap();
    assert.equal(new URL(page.url()).hash, "");
    const x = (await link.locator("..").boundingBox()).x;
    await page.getByRole("button", { name: "Вправо", exact: true }).tap();
    assert.ok((await link.locator("..").boundingBox()).x > x);
    await page.getByRole("button", { name: "Готово", exact: true }).tap();
    await link.focus();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#face-03-back").getAttribute("aria-hidden"), "true");
    await page.locator(".next-scene").focus();
    await link.focus();
    assert.equal(await page.locator("#face-03-back").getAttribute("aria-hidden"), "false");
    await page.keyboard.press("Enter");
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#03/);
  } finally {
    await page.close();
  }
});
test("the same coast sample is traversed in both directions and survives resize", async () => {
  const page = await browser.newPage({ viewport: { width: 1200, height: 1050 } });
  try {
    await page.addInitScript(() => {
      const ctx = CanvasRenderingContext2D.prototype,
        arc = ctx.arc,
        clear = ctx.clearRect,
        hit = ctx.isPointInPath;
      window.fieldProbe = { sample: [], checks: 0 };
      ctx.isPointInPath = function (...args) {
        window.fieldProbe.checks++;
        return hit.apply(this, args);
      };
      ctx.clearRect = function (...args) {
        window.fieldProbe.sample = [];
        return clear.apply(this, args);
      };
      ctx.arc = function (x, y, ...args) {
        if (window.fieldProbe.sample.length < 15) window.fieldProbe.sample.push([x, y]);
        return arc.call(this, x, y, ...args);
      };
    });
    await page.goto(origin);
    async function sample(progress) {
      await page.evaluate((p) => {
        const atlas = document.querySelector("#features"),
          end = atlas.getBoundingClientRect().top + scrollY,
          start = Math.max(0, end - innerHeight);
        scrollTo(0, start + (end - start) * p);
      }, progress);
      await page.waitForFunction(
        (p) => Math.abs(Number(document.querySelector("canvas").dataset.stage) - p) < 0.005,
        progress,
      );
      return page.evaluate(() => window.fieldProbe.sample);
    }
    const start = await sample(0),
      mid = await sample(0.5),
      end = await sample(1),
      back = await sample(0.5);
    const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
    assert.ok(distance(start[0], mid[0]) > 12 && distance(end[0], mid[0]) > 12);
    for (let i = 0; i < mid.length; i++) assert.ok(distance(mid[i], back[i]) < 8);
    const checks = await page.evaluate(() => window.fieldProbe.checks);
    assert.ok(checks > 0);
    await page.setViewportSize({ width: 1000, height: 900 });
    await sample(1);
    await page.getByRole("button", { name: "Моя сеть", exact: true }).click();
    assert.equal(await page.evaluate(() => window.fieldProbe.checks), checks);
    assert.match(await page.locator(".atlas-proof").innerText(), /3\/10/);
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
