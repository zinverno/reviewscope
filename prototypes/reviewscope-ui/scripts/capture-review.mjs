import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
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
const commit =
  process.env.GITHUB_SHA || execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
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

function stage(page) {
  return page.evaluate(() => Number(document.querySelector(".point-field").dataset.stage));
}
async function wheelTo(page, target) {
  for (let i = 0; i < 500; i++) {
    if ((await stage(page)) >= target - 0.004) break;
    await page.mouse.wheel(0, 16);
    await wait(20);
  }
  await wait(260);
}
function visibleField(page) {
  return page.evaluate(() => {
    const el = document.querySelector(".point-field"),
      rect = el.getBoundingClientRect(),
      scale = el.width / rect.width,
      data = el.getContext("2d").getImageData(0, 0, el.width, el.height).data;
    let visible = 0;
    for (let y = 0; y < el.height; y += 2)
      for (let x = 0; x < el.width; x += 2) {
        if (data[4 * (y * el.width + x) + 3] < 60) continue;
        const px = rect.left + x / scale,
          py = rect.top + y / scale;
        if (px >= 0 && px <= innerWidth && py >= 0 && py <= innerHeight) visible++;
      }
    return visible;
  });
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
  const report = { commit };
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

  const framePage = await browser.newPage({ viewport });
  await framePage.goto(url, { waitUntil: "networkidle" });
  await wait(450);
  report.morphVisibility = { start: await visibleField(framePage) };
  await shot(framePage, "morph-00-start");
  for (const [name, target] of [
    ["25", 0.25],
    ["50", 0.5],
    ["75", 0.75],
    ["100", 1],
  ]) {
    await wheelTo(framePage, target);
    report.morphVisibility[name] = await visibleField(framePage);
    await shot(framePage, `morph-${name}`);
  }
  await framePage.close();

  await writeFile(join(dir, "frame-profile.json"), JSON.stringify(report, null, 2) + "\n");

  await record("01-hero-idle", async (page) => {
    await wait(8300);
    assert.equal(await page.locator(".point-field").getAttribute("data-stage"), "0.000");
    await shot(page, "hero");
  });

  await record("02-visible-morph", async (page) => {
    for (const target of [0.25, 0.5, 0.75]) {
      await wheelTo(page, target);
      await wait(600);
    }
    for (let i = 0; i < 8; i++) {
      await page.mouse.wheel(0, -42);
      await wait(115);
      await page.mouse.wheel(0, 42);
      await wait(115);
    }
    await wheelTo(page, 1);
    await wait(500);
    for (let i = 0; i < 8; i++) {
      await page.mouse.wheel(0, -38);
      await wait(110);
      await page.mouse.wheel(0, 38);
      await wait(110);
    }
  });

  await record("03-map-light-and-selection", async (page) => {
    await page.locator(".atlas-plane").evaluate((el) => el.scrollIntoView({ block: "center" }));
    await page.waitForFunction(() => document.querySelector("canvas").dataset.stage === "1.000");
    const top = await page.evaluate(() => scrollY);
    await wait(8200);
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
    await page.getByRole("button", { name: "Моя сеть", exact: true }).click();
    await page
      .getByRole("button", { name: "На карте: Линия", exact: true })
      .evaluate((el) => el.focus());
    await page.keyboard.press("Enter");
    await wait(800);
    await page.getByRole("button", { name: "Сравнение", exact: true }).click();
    await page.getByRole("button", { name: "На карте: Луч", exact: true }).click();
    await wait(1400);
  });

  await record("04-faces-and-drag", async (page) => {
    const link = page.getByRole("link", { name: "Открыть отзыв #02" });
    await shot(page, "front");
    await wait(1200);
    await link.hover();
    await wait(1100);
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
      await wait(400);
    }
    await page.mouse.up();
    assert.equal(new URL(page.url()).hash, "");
    await wait(1200);
    await link.click();
    await page.locator(".evidence").waitFor();
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#02/);
    await wait(1200);
  });

  const detail = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await detail.goto(url + "#features", { waitUntil: "networkidle" });
  await detail.waitForFunction(() => document.querySelector("canvas").dataset.stage === "1.000");
  const arc = await detail.locator('.pin[data-place="arc"]').boundingBox();
  await shot(detail, "company-particle", {
    clip: {
      x: Math.max(0, arc.x - 80),
      y: Math.max(0, arc.y - 80),
      width: 240,
      height: 210,
    },
  });
  await detail.getByRole("button", { name: "Сравнение", exact: true }).click();
  await detail.getByRole("button", { name: "На карте: Луч", exact: true }).click();
  await detail.locator(".atlas-data").scrollIntoViewIfNeeded();
  await wait(350);
  await shot(detail, "compare");
  await detail.locator("#how").scrollIntoViewIfNeeded();
  await detail.getByRole("button", { name: /Проверить смысл/ }).click();
  await wait(300);
  await shot(detail, "method");
  await detail.close();

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
  await mobile.close();

  await writeFile(
    join(dir, "verification.md"),
    [
      "# ReviewScope PR-03 UX v5 verification",
      "",
      `- commit: \`${commit}\``,
      `- preview URL: \`${url}\``,
      "- browsers used for automated captures: Chromium headless (production preview)",
      "- videos: `01-hero-idle.webm`, `02-visible-morph.webm`, `03-map-light-and-selection.webm`, `04-faces-and-drag.webm`",
      "- five frames of one native scroll: `morph-00-start.png` … `morph-100.png`",
      "- per-frame visible particle sample counts: see `frame-profile.json`",
      "- browser/CI checks are recorded in the PR run; manual Firefox acceptance remains with the owner",
      "",
    ].join("\n"),
  );
  console.log(dir);
} finally {
  await browser?.close();
  await baseline?.close();
  await server.close();
}
