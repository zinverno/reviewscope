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
test("the board grid morphs reversibly into the same land centres as the marker plane", async () => {
  const page = await browser.newPage({ viewport: { width: 1200, height: 1050 } });
  try {
    await page.addInitScript(() => {
      window.land = [];
      const ctx = CanvasRenderingContext2D.prototype,
        hit = ctx.isPointInPath;
      ctx.isPointInPath = function (path, x, y, ...args) {
        const inside = hit.call(this, path, x, y, ...args);
        if (inside && this.canvas.matches(".point-field")) window.land.push([x, y]);
        return inside;
      };
    });
    await page.goto(origin);
    const field = page.locator(".point-field");
    async function stage(p) {
      await page.evaluate((v) => {
        const end = document.querySelector("#features").getBoundingClientRect().top + scrollY;
        const start = Math.max(0, end - innerHeight);
        scrollTo(0, start + v * (end - start));
      }, p);
      await page.waitForFunction(
        (v) => Math.abs(Number(document.querySelector("canvas").dataset.stage) - v) < 0.004,
        p,
      );
    }
    // Sample alpha peaks from the actual rendered pixels, independently of the paint API.
    function pixels(el) {
      const ctx = el.getContext("2d"),
        { width: w, height: h } = el;
      const data = ctx.getImageData(0, 0, w, h).data,
        step = el.clientWidth / w;
      const peaks = [];
      for (let y = 0; y < h; y++)
        for (let x = 0; x < w; x++) {
          const off = 4 * (y * w + x) + 3;
          if (
            data[off] > 12 &&
            (x === 0 || data[off - 4] <= 12) &&
            (y === 0 || data[off - 4 * w] <= 12)
          )
            peaks.push([x * step, y * step]);
        }
      return peaks;
    }
    await stage(0);
    const grid = await field.evaluate(pixels);
    assert.ok(grid.length > 50);
    const xs = new Set(grid.map(([x]) => x)),
      ys = new Set(grid.map(([, y]) => y));
    assert.ok(xs.size * ys.size < grid.length * 12, "hero centres form rows and columns");
    const board = await page.locator(".board-canvas").boundingBox();
    assert.ok(Math.min(...xs) > board.x - board.width * 0.22, "quiet title area");
    await stage(0.5);
    const middle = await field.evaluate(pixels);
    assert.notDeepEqual(middle, grid);
    await stage(1);
    async function checkCoast() {
      return field.evaluate((el) => {
        const r = el.getBoundingClientRect(),
          map = document.querySelector(".atlas-plane").getBoundingClientRect();
        const ctx = el.getContext("2d"),
          { width: w, height: h, data } = ctx.getImageData(0, 0, el.width, el.height);
        const scale = w / r.width;
        return window.land.filter(([x, y]) => {
          const px = Math.floor((map.left - r.left + ((x - 32) * map.width) / 514) * scale);
          const py = Math.floor(((map.top - r.top + (y * map.height) / 292) * h) / r.height);
          return (data[4 * (py * w + px) + 3] ?? 0) < 66;
        }).length;
      });
    }
    const count = await page.evaluate(() => window.land.length);
    assert.ok(count > 100);
    assert.equal(await checkCoast(), 0);
    await page.mouse.wheel(0, 140);
    await page.waitForFunction(() => scrollY > document.querySelector("#features").offsetTop + 100);
    assert.equal(await checkCoast(), 0);
    await stage(0.5);
    const back = await field.evaluate(pixels);
    // Halo brightness can move the antialiased edge by a pixel.
    for (const [x, y] of middle.filter((_, i) => i % 11 === 0)) {
      assert.ok(back.some(([u, v]) => Math.abs(x - u) <= 2 && Math.abs(y - v) <= 2));
    }
    await page.setViewportSize({ width: 1000, height: 900 });
    await stage(1);
    assert.equal(await checkCoast(), 0);
    assert.equal(await page.evaluate(() => window.land.length), count);
    await page.goto(origin + "#features");
    await page.waitForFunction(() => document.querySelector("canvas").dataset.stage === "1.000");
    assert.equal(await checkCoast(), 0);
  } finally {
    await page.close();
  }
});

test("map markers select the evidence, with a network total and both comparison samples", async () => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  try {
    await page.goto(origin + "#features");
    await page.waitForFunction(() => document.querySelector(".point-field").dataset.stage === "1.000");
    const pin = (name) => page.getByRole("button", { name: "На карте: " + name, exact: true });
    const button = (name) => page.getByRole("button", { name, exact: true });
    const panels = page.locator(".atlas-proof");
    assert.equal(await page.getByRole("heading", { name: "Выберите точку" }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "Арка", exact: true }).count(), 0);
    await button("Моя сеть").click();
    assert.match(await panels.innerText(), /3\/10/);
    await pin("Арка").focus();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Enter");
    assert.equal(await pin("Линия").getAttribute("aria-pressed"), "true");
    assert.equal(await pin("Линия").evaluate((e) => e === document.activeElement), true);
    await page.keyboard.press("Tab");
    await page.keyboard.press("Space");
    await button("Октябрь").tap();
    assert.match(await panels.innerText(), /Мыс/i);
    assert.match(await panels.innerText(), /Нет данных/);
    await panels.locator("summary").click();
    assert.equal(await panels.locator("li").count(), 3);
    await button("← К итогу всей сети").tap();
    await button("Сентябрь").tap();
    assert.match(await panels.innerText(), /3\/10/);
    await button("Сравнение").tap();
    await pin("Арка").tap();
    await pin("Луч").tap();
    assert.equal(await panels.count(), 2);
    assert.match(await panels.first().innerText(), /Луч/i);
    assert.match(await panels.first().innerText(), /1\/4/);
    assert.match(await panels.last().innerText(), /2\/3/);
    for (const panel of await panels.all()) {
      await panel.locator("summary").click();
      assert.ok((await panel.locator("li").count()) >= 3);
    }
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
