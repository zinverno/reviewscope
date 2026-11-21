import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { preview } from "vite";

const dir = process.argv[2] ?? (await mkdtemp(join(tmpdir(), "reviewscope-ux-")));
if (process.argv[2]) await mkdir(dir);
const server = await preview({ preview: { host: "127.0.0.1", port: 0 }, logLevel: "error" });
const url = server.resolvedUrls.local[0];
const viewport = { width: 1440, height: 900 };
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const shot = (page, name, options) =>
  page.screenshot({ path: join(dir, name + ".png"), ...options });
let browser;

async function record(name, play) {
  const context = await browser.newContext({
    locale: "ru-RU",
    viewport,
    recordVideo: { dir, size: viewport },
  });
  const page = await context.newPage();
  const video = page.video();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(url, { waitUntil: "networkidle" });
    await play(page);
    assert.deepEqual(errors, []);
  } finally {
    await context.close();
  }
  await video.saveAs(join(dir, name + ".webm"));
  await video.delete();
}

try {
  browser = await chromium.launch();
  await record("01-scroll-down-and-back", async (page) => {
    const [start, end] = await page.evaluate(() => {
      const end = document.querySelector("#features").getBoundingClientRect().top + scrollY;
      return [Math.max(0, end - innerHeight), end];
    });
    await wait(1500);
    for (const progress of [0.25, 0.5, 0.75, 1, 0.75, 0.5, 0.25, 0]) {
      const top = start + (end - start) * progress;
      await page.evaluate((y) => scrollTo({ top: y, behavior: "smooth" }), top);
      await page.waitForFunction(
        (p) => Math.abs(Number(document.querySelector("canvas").dataset.stage) - p) < 0.004,
        progress,
      );
      await wait(1850);
      if (progress === 1) {
        await shot(page, "map");
        const map = await page.locator(".atlas-plane").boundingBox();
        await shot(page, "coast", {
          clip: {
            x: map.x + map.width * 0.045,
            y: map.y + map.height * 0.06,
            width: map.width * 0.3,
            height: map.height * 0.35,
          },
        });
      }
    }
  });

  await record("02-faces-and-drag", async (page) => {
    const link = page.getByRole("link", { name: "Открыть отзыв #02" });
    await shot(page, "front");
    await wait(1500);
    await link.hover();
    await wait(1400);
    await shot(page, "back");
    const quote = await page.locator("#face-02-back").boundingBox();
    let x = quote.x + quote.width / 2,
      y = quote.y + quote.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    for (const [dx, dy] of [
      [-155, -35],
      [-24, 83],
      [107, -32],
    ]) {
      for (let i = 1; i <= 48; i++) {
        await page.mouse.move(x + (dx * i) / 48, y + (dy * i) / 48);
        await wait(32);
      }
      x += dx;
      y += dy;
      await wait(450);
    }
    await page.mouse.up();
    assert.equal(new URL(page.url()).hash, "");
    await wait(1500);
    await link.click();
    await page.locator(".evidence").waitFor();
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#02/);
    await wait(1500);
  });

  const desktop = await browser.newPage({ viewport });
  await desktop.goto(url + "#features", { waitUntil: "networkidle" });
  await desktop.getByRole("button", { name: "Сравнение", exact: true }).click();
  await desktop.getByRole("button", { name: "На карте: Луч", exact: true }).click();
  await desktop.locator(".atlas-data").scrollIntoViewIfNeeded();
  await wait(350);
  await shot(desktop, "compare");
  await desktop.locator("#how").scrollIntoViewIfNeeded();
  await desktop.getByRole("button", { name: /Проверить смысл/ }).click();
  await wait(300);
  await shot(desktop, "method");

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  await mobile.goto(url);
  await mobile.getByRole("link", { name: "Открыть отзыв #01" }).tap();
  await wait(550);
  await shot(mobile, "mobile");
  console.log(dir);
} finally {
  await browser?.close();
  await server.close();
}
