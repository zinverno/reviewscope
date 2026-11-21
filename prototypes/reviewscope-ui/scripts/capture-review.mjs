import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "playwright";
import { preview } from "vite";
import { profileScroll } from "./profile-scroll.mjs";

const dir = process.argv[2] ?? (await mkdtemp(join(tmpdir(), "reviewscope-ux-")));
if (process.argv[2]) await mkdir(dir, { recursive: true });
const server = await preview({ preview: { host: "127.0.0.1", port: 0 }, logLevel: "error" });
const url = server.resolvedUrls.local[0];
const viewport = { width: 1440, height: 900 };
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const shot = (page, name, options) =>
  page.screenshot({ path: join(dir, name + ".png"), ...options });
let browser, baseline;

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
  async function wide(label, address) {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    try {
      await page.goto(address, { waitUntil: "networkidle" });
      await shot(page, label + "-hero-1920");
      await page.locator(".atlas-plane").evaluate((el) => el.scrollIntoView({ block: "center" }));
      await page.waitForFunction(
        () => Number(document.querySelector(".point-field").dataset.stage) === 1,
      );
      await wait(350);
      await shot(page, label + "-map-1920");
      return await page.locator(".atlas-plane").evaluate((el) => {
        const canvas = document.querySelector(".point-field"),
          c = canvas.getBoundingClientRect();
        const map = el.getBoundingClientRect(),
          sx = canvas.width / c.width,
          sy = canvas.height / c.height;
        const { data, width } = canvas
          .getContext("2d")
          .getImageData(0, 0, canvas.width, canvas.height);
        let left = width,
          right = 0;
        const top = Math.max(0, Math.floor((map.top - c.top) * sy));
        const bottom = Math.min(canvas.height, Math.ceil((map.bottom - c.top) * sy));
        for (let y = top; y < bottom; y++)
          for (let x = 0; x < width; x++) {
            if (data[(y * width + x) * 4 + 3] > 75) {
              left = Math.min(left, x);
              right = Math.max(right, x);
            }
          }
        return { viewport: innerWidth, landWidthCssPx: Math.round((right - left + 1) / sx) };
      });
    } finally {
      await page.close();
    }
  }
  const report = {};
  if (process.argv[3]) {
    baseline = await preview({
      build: { outDir: resolve(process.argv[3]) },
      preview: { host: "127.0.0.1", port: 0 },
      logLevel: "error",
    });
    const before = baseline.resolvedUrls.local[0];
    report.before = {
      coast: await wide("before", before),
      scroll: await profileScroll(browser, before),
    };
  }
  report.after = {
    coast: await wide("after", url),
    scroll: await profileScroll(browser, url),
    reversals: await profileScroll(browser, url, true),
  };
  assert.ok(report.after.coast.landWidthCssPx / report.after.coast.viewport >= 0.85);
  await writeFile(join(dir, "frame-profile.json"), JSON.stringify(report, null, 2) + "\n");

  await record("01-wheel-and-reversals", async (page) => {
    const end = await page
      .locator("#features")
      .evaluate((el) => el.getBoundingClientRect().top + scrollY);
    await page.mouse.move(520, 650);
    await wait(850);
    async function wheel(delta, count, pause) {
      for (let i = 0; i < count; i++) {
        await page.mouse.wheel(0, delta);
        await wait(pause);
      }
    }
    await wheel(30, Math.floor((end - 40) / 30), 65);
    await wait(350);
    for (let i = 0; i < 4; i++) {
      await wheel(88, 1, 170);
      await wheel(-88, 1, 170);
    }
    await wheel(64, 4, 65);
    await page.waitForFunction(() => document.querySelector("canvas").dataset.stage === "1.000");
    await wait(500);
    for (let i = 0; i < 4; i++) {
      await wheel(-34, 1, 135);
      await wheel(34, 1, 135);
    }
    await wheel(-115, Math.ceil((end + 260) / 115), 55);
    await page.waitForFunction(() => scrollY === 0);
    await wait(750);
  });
  await record("03-stationary-map", async (page) => {
    await page.locator(".atlas-plane").evaluate((el) => el.scrollIntoView({ block: "center" }));
    await page.waitForFunction(() => document.querySelector("canvas").dataset.stage === "1.000");
    const top = await page.evaluate(() => scrollY);
    await wait(8100);
    assert.equal(await page.evaluate(() => scrollY), top);
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
  await mobile.goto(url + "#features");
  await mobile.getByRole("button", { name: "Моя сеть", exact: true }).tap();
  await mobile.getByRole("button", { name: "На карте: Мыс", exact: true }).tap();
  await mobile.getByRole("button", { name: "Октябрь", exact: true }).tap();
  await mobile
    .locator(".atlas-plane")
    .evaluate((el) => scrollBy(0, el.getBoundingClientRect().top - 60));
  await wait(300);
  await shot(mobile, "mobile-map");
  console.log(dir);
} finally {
  await browser?.close();
  await baseline?.close();
  await server.close();
}
