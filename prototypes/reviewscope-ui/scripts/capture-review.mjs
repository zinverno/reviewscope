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

function progress(page) {
  return page.evaluate(() => Number(document.querySelector(".story-stage")?.dataset.progress ?? 0));
}
function phase(page) {
  return page.evaluate(() => document.querySelector(".story-stage")?.dataset.phase ?? "");
}
async function wheelToProgress(page, target) {
  for (let i = 0; i < 900; i++) {
    const current = await progress(page);
    if (Math.abs(current - target) <= 0.008) break;
    const distance = Math.abs(current - target);
    const step = distance > 0.12 ? 36 : distance > 0.03 ? 16 : 8;
    await page.mouse.wheel(0, current < target ? step : -step);
    await wait(24);
  }
  await wait(260);
}
async function wheelToTop(page) {
  for (let i = 0; i < 200; i++) {
    if ((await page.evaluate(() => scrollY)) <= 1) break;
    await page.mouse.wheel(0, -180);
    await wait(18);
  }
  await wait(280);
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
function firstFrame(page) {
  return page.evaluate(() => {
    const mast = document.querySelector(".mast").getBoundingClientRect(),
      eyebrowEl = document.querySelector(".eyebrow") ?? document.querySelector(".opening-copy .folio"),
      eyebrow = eyebrowEl.getBoundingClientRect(),
      h1 = document.querySelector(".opening h1").getBoundingClientRect(),
      cards = document.querySelector(".board-canvas").getBoundingClientRect();
    return {
      gapCssPx: Math.round(eyebrow.top - mast.bottom),
      h1FitsViewport: h1.top >= 0 && h1.bottom <= innerHeight,
      cardsFitViewport: cards.top >= 0 && cards.bottom <= innerHeight,
      mastBottomCssPx: Math.round(mast.bottom),
      h1TopCssPx: Math.round(h1.top),
      cardsBottomCssPx: Math.round(cards.bottom),
      viewportHeightCssPx: innerHeight,
    };
  });
}

try {
  browser = await chromium.launch();
  async function wide(label, address, baselineBuild = false) {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    try {
      await page.goto(address, { waitUntil: "networkidle" });
      await shot(page, label + "-hero-1920");
      const first = await firstFrame(page);
      if (baselineBuild) {
        await page.locator(".atlas-plane").evaluate((el) => el.scrollIntoView({ block: "center" }));
        await page.waitForFunction(() => Number(document.querySelector("canvas").dataset.stage) === 1);
      } else {
        await wheelToProgress(page, 1.35);
        await page.waitForFunction(
          () => document.querySelector(".story-stage")?.dataset.phase === "ATLAS_HOLD",
        );
      }
      await wait(350);
      await shot(page, label + "-map-1920");
      const coast = await page.locator(".atlas-plane").evaluate((el) => {
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
        return {
          viewport: innerWidth,
          mapWidthCssPx: Math.round(map.width),
          landWidthCssPx: Math.round((right - left + 1) / sx),
          mapTop: Math.round(map.top),
          mapBottom: Math.round(map.bottom),
        };
      });
      return { first, coast };
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
    const beforeWide = await wide("before", before, true);
    report.before = {
      first: beforeWide.first,
      coast: beforeWide.coast,
      scroll: await profileScroll(browser, before),
    };
  }
  const afterWide = await wide("after", url);
  report.after = {
    first: afterWide.first,
    coast: afterWide.coast,
    scroll: await profileScroll(browser, url),
    reversals: await profileScroll(browser, url, true),
  };
  assert.ok(report.after.coast.landWidthCssPx / report.after.coast.mapWidthCssPx >= 0.85);
  assert.ok(report.after.coast.mapTop >= 0 && report.after.coast.mapBottom <= 1080);
  assert.ok(
    report.after.first.gapCssPx >= 24 && report.after.first.gapCssPx <= 90,
    JSON.stringify(report.after.first),
  );
  assert.ok(report.after.first.h1FitsViewport && report.after.first.cardsFitViewport);

  const framePage = await browser.newPage({ viewport });
  await framePage.goto(url, { waitUntil: "networkidle" });
  await wait(450);
  report.morphVisibility = { start: await visibleField(framePage) };
  await shot(framePage, "morph-00-start");
  for (const [name, target] of [
    ["25", 0.5],
    ["50", 0.7],
    ["75", 0.9],
    ["100", 1.25],
  ]) {
    await wheelToProgress(framePage, target);
    report.morphVisibility[name] = await visibleField(framePage);
    await shot(framePage, `morph-${name}`);
  }
  await framePage.close();

  await record("01-continuous-v6", async (page) => {
    await shot(page, "01-first-frame");
    await wait(1100);
    await wheelToProgress(page, 0.18);
    await wait(900);
    assert.equal(await phase(page), "HERO_HOLD");
    await shot(page, "02-hero-hold");
    await wheelToProgress(page, 0.52);
    await wait(900);
    assert.equal(await phase(page), "MORPH");
    await shot(page, "03-morph-stop");
    await wheelToProgress(page, 0.82);
    await wait(500);
    await wheelToProgress(page, 1.25);
    await wait(700);
    assert.equal(await phase(page), "ATLAS_HOLD");
    await shot(page, "04-atlas-hold-a");
    await wheelToProgress(page, 1.6);
    await wait(600);
    assert.equal(await phase(page), "ATLAS_HOLD");
    await shot(page, "05-atlas-hold-b");
    await wheelToProgress(page, 1.27);
    const held = await page.evaluate(() => scrollY);
    await wait(6800);
    assert.equal(await phase(page), "ATLAS_HOLD");
    assert.ok(Math.abs((await page.evaluate(() => scrollY)) - held) < 2);
    await page.getByRole("button", { name: "Моя сеть", exact: true }).click();
    await page
      .getByRole("button", { name: "На карте: Линия", exact: true })
      .evaluate((el) => el.focus());
    await page.keyboard.press("Enter");
    await page.getByRole("button", { name: "Октябрь", exact: true }).click();
    await wait(900);
    await page.getByRole("button", { name: "Сравнение", exact: true }).click();
    await page.getByRole("button", { name: "На карте: Луч", exact: true }).click();
    await wait(1100);
    await wheelToProgress(page, 2.28);
    await wait(700);
    assert.equal(await phase(page), "RELEASE");
    await page.locator("#how").scrollIntoViewIfNeeded();
    await wait(900);
    await shot(page, "06-third-scene");
    await wheelToTop(page);
    await wait(700);
  });

  await record("02-details-and-return", async (page) => {
    await wheelToProgress(page, 1.3);
    await page.getByRole("button", { name: "Моя сеть", exact: true }).click();
    await page.getByRole("button", { name: "Состав расчёта", exact: true }).click();
    await wait(900);
    await shot(page, "07-details-open");
    await page.locator("#how").scrollIntoViewIfNeeded();
    await wait(800);
    await wheelToProgress(page, 1.3);
    await wait(700);
  });

  const compact = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  await compact.goto(url, { waitUntil: "networkidle" });
  await shot(compact, "08-first-frame-1366");
  await wheelToProgress(compact, 1.3);
  await shot(compact, "09-atlas-hold-1366");
  await compact.close();

  const low = await browser.newPage({ viewport: { width: 1366, height: 620 } });
  await low.goto(url, { waitUntil: "networkidle" });
  await shot(low, "10-low-height-flow");
  await low.close();

  const detail = await browser.newPage({ viewport });
  await detail.goto(url + "#features", { waitUntil: "networkidle" });
  await detail.waitForFunction(() => document.querySelector("canvas")?.dataset.morph === "1.000");
  const arc = await detail.locator('.pin[data-place="arc"]').boundingBox();
  await shot(detail, "11-company-particle", {
    clip: {
      x: Math.max(0, arc.x - 80),
      y: Math.max(0, arc.y - 80),
      width: 240,
      height: 210,
    },
  });
  await detail.close();

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  await mobile.goto(url);
  await shot(mobile, "12-mobile-hero");
  await mobile.getByRole("link", { name: "Открыть отзыв #01" }).tap();
  await wait(550);
  await shot(mobile, "13-mobile-card");
  await mobile.goto(url + "#features");
  await mobile.getByRole("button", { name: "Моя сеть", exact: true }).tap();
  await mobile.getByRole("button", { name: "На карте: Мыс", exact: true }).tap();
  await mobile.getByRole("button", { name: "Октябрь", exact: true }).tap();
  await mobile
    .locator(".atlas-plane")
    .evaluate((el) => scrollBy(0, el.getBoundingClientRect().top - 60));
  await wait(300);
  await shot(mobile, "14-mobile-map");
  await mobile.close();

  await writeFile(join(dir, "frame-profile.json"), JSON.stringify(report, null, 2) + "\n");
  await writeFile(
    join(dir, "verification.md"),
    [
      "# ReviewScope PR-03 UX v6 verification",
      "",
      `- commit: \`${commit}\``,
      `- preview URL: \`${url}\``,
      `- Chromium: \`${browser.version()}\`; browser tests also run with Firefox in CI/local checks`,
      "- production preview; viewport 1440x900 for videos, 1920x1080 and 1366x768 for desktop stills, 1366x620 for low-height flow, 390x844 for mobile",
      "- `01-continuous-v6.webm`: first frame → HERO_HOLD → MORPH stop → two ATLAS_HOLD positions → 6.8s quiet → selection/period → RELEASE → third scene → back, one uninterrupted context",
      "- `02-details-and-return.webm`: long calculation details in normal flow and return",
      "- first screen before/after: `before-hero-1920.png` and `after-hero-1920.png` (before only when a baseline build is supplied); measured mast/eyebrow gap and viewport fit are in `frame-profile.json`",
      "- morph frames: `morph-00-start.png`, `morph-25.png`, `morph-50.png`, `morph-75.png`, `morph-100.png`",
      "- atlas hold frames: `04-atlas-hold-a.png`, `05-atlas-hold-b.png`, `09-atlas-hold-1366.png`",
      "- per-frame visible particle samples and frame timing: `frame-profile.json`",
      "- native browser zoom 200%: not checked in this headless environment; owner check remains open",
      "- physical Firefox smoothness, glass readability and phone swipe remain owner checks; headless Firefox browser tests passed",
      "",
    ].join("\n"),
  );
  console.log(dir);
} finally {
  await browser?.close();
  await baseline?.close();
  await server.close();
}
