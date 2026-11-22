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
const viewport = { width: 1920, height: 1000 };
const commit = execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
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
async function wheelToProgress(page, target) {
  for (let i = 0; i < 900; i++) {
    const current = await progress(page);
    if (Math.abs(current - target) <= 0.008) break;
    const distance = Math.abs(current - target);
    const step = distance > 0.12 ? 36 : distance > 0.03 ? 16 : 8;
    await page.mouse.wheel(0, current < target ? step : -step);
    await wait(24);
  }
  await wait(200);
  assert.ok(Math.abs((await progress(page)) - target) < 0.012, `wheel target ${target}`);
}
// Measure the rendered silhouette (including its visible glow) in CSS pixels.
function bounds(page) {
  return page.locator(".point-field").evaluate((el) => {
    const { width, height, data } = el.getContext("2d").getImageData(0, 0, el.width, el.height);
    let left = width,
      right = 0,
      top = height,
      bottom = 0;
    for (let y = 0; y < height; y++)
      for (let x = 0; x < width; x++) {
        if (data[(y * width + x) * 4 + 3] < 7) continue;
        left = Math.min(left, x);
        right = Math.max(right, x);
        top = Math.min(top, y);
        bottom = Math.max(bottom, y);
      }
    const scale = el.clientWidth / width;
    return {
      viewport: [innerWidth, innerHeight],
      dpr: devicePixelRatio,
      bounds: [left, top, right, bottom].map((v) => Math.round(v * scale)),
      width: Math.round((right - left + 1) * scale),
    };
  });
}

async function dimSample(page) {
  return page.evaluate(async () => {
    const canvas = document.querySelector(".point-field");
    const ctx = canvas.getContext("2d");
    const { x, y, width, height } = document
      .querySelector(".pin[aria-pressed=true]")
      .getBoundingClientRect();
    const rect = canvas.getBoundingClientRect();
    const px = Math.round(((x + width / 2 - rect.x) * canvas.width) / rect.width);
    const py = Math.round(((y + height / 2 - rect.y) * canvas.height) / rect.height);
    const alpha = () => ctx.getImageData(px, py, 1, 1).data[3];
    const start = performance.now();
    let min = 255,
      max = 0;
    while (performance.now() - start < 6800) {
      await new Promise(requestAnimationFrame);
      const a = alpha();
      min = Math.min(min, a);
      max = Math.max(max, a);
    }
    while (alpha() > min + 2 && performance.now() - start < 12500)
      await new Promise(requestAnimationFrame);
    return { min, max, atCapture: alpha(), centre: [px, py] };
  });
}

try {
  browser = await chromium.launch();
  const report = { commit, browser: browser.version(), silhouettes: [] };
  if (process.argv[3]) {
    baseline = await preview({
      build: { outDir: resolve(process.argv[3]) },
      preview: { host: "127.0.0.1", port: 0 },
      logLevel: "error",
    });
    const before = await browser.newPage({ viewport });
    for (const hash of ["home", "features"]) {
      await before.goto(baseline.resolvedUrls.local[0] + "#" + hash, { waitUntil: "networkidle" });
      await shot(before, "before-" + hash);
    }
    report.baselineSilhouettes = [await bounds(before)];
    await before.setViewportSize({width: 1366, height: 680});
    await before.reload({waitUntil: "networkidle"});
    report.baselineSilhouettes.push(await bounds(before));
    await shot(before, "before-atlas-1366");
    await before.close();
    report.baselineScroll = await profileScroll(browser, baseline.resolvedUrls.local[0]);
  }
  await record("01-story-down-and-back", async (page) => {
    await page.mouse.move(24, 560);
    await shot(page, "hero");
    for (const [p, name] of [
      [0.375, "transition-12-25"],
      [0.6, "transition-12-mid"],
      [0.825, "transition-12-75"],
      [1.28, "atlas"],
      [1.75, "transition-23-25"],
      [1.95, "transition-23-mid"],
      [2.15, "transition-23-75"],
      [2.44, "method"],
    ]) {
      await wheelToProgress(page, p);
      await shot(page, name);
      if (name.endsWith("mid")) {
        await wait(750);
        await wheelToProgress(page, p - 0.11);
      }
      if (name === "atlas") report.silhouettes.push(await bounds(page));
    }
    await wait(750);
    for (const p of [2.22, 2.29, 1.95, 1.57, 1.65, 1.27, 0.99, 1.07, 0.6, 0.22, 0.29, 0])
      await wheelToProgress(page, p);
  });
  await record("02-interactions", async (page) => {
    const link = page.getByRole("link", { name: "Открыть отзыв #02" });
    await link.hover();
    const box = await page.locator("#face-02-back").boundingBox();
    const x = box.x + box.width / 2,
      y = box.y + box.height / 2;
    await wait(500);
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x - 105, y + 34, { steps: 24 });
    await page.mouse.up();
    assert.equal(new URL(page.url()).hash, "");
    await wait(550);
    await link.click();
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#02/);
    await wait(650);
    await page.goBack();
    await wheelToProgress(page, 1.25);
    await page.locator('.pin[data-place="line"]').click();
    report.selectedPulse = await dimSample(page);
    assert.ok(
      report.selectedPulse.max - report.selectedPulse.min > 15 &&
        report.selectedPulse.atCapture <= report.selectedPulse.min + 3,
    );
    await shot(page, "atlas-company-dim");
    await page.getByRole("button", { name: "Моя сеть", exact: true }).click();
    await page.getByRole("button", { name: "Состав расчёта" }).click();
    await shot(page, "proof");
    await wait(550);
    await page.locator("dialog").hover();
    await page.mouse.wheel(0, 650);
    await wait(600);
    await page.keyboard.press("Escape");
    await wait(500);
  });

  for (const [width, height] of [
    [1920, 1080],
    [1440, 900],
    [1366, 768],
    [1366, 680],
    [1366, 620],
    [390, 844],
    [320, 700],
  ]) {
    const page = await browser.newPage({ viewport: { width, height }, hasTouch: width < 400 });
    await page.goto(url + "#features", { waitUntil: "networkidle" });
    if (height >= 680 && width >= 900) report.silhouettes.push(await bounds(page));
    if (width === 1366 && height === 680) await shot(page, "atlas-1366");
    if (width === 390) {
      await page.getByRole("button", { name: "Моя сеть", exact: true }).tap();
      await shot(page, "mobile");
      await page.getByRole("button", { name: "Состав расчёта" }).tap();
      await shot(page, "mobile-proof");
      await page.getByRole("button", { name: "Закрыть" }).tap();
    }
    if (width < 400) {
      await page.goto(url + "#how", {waitUntil: "networkidle"});
      await page.getByRole("button", {name: /Проверить смысл/}).tap();
      await shot(page, "method-mobile-" + width);
    }
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.close();
  }

  const steps = await browser.newPage({viewport: {width: 1000, height: 900}});
  await steps.goto(url + "#how", {waitUntil: "networkidle"});
  await shot(steps, "method-step-1");
  await steps.getByRole("button", {name: /Проверить смысл/}).click();
  await shot(steps, "method-step-2");
  await steps.close();
  for (const before of report.baselineSilhouettes ?? []) {
    const after = report.silhouettes.find(s => s.viewport.join() === before.viewport.join());
    assert.ok(Math.abs(before.width - after.width) <= 2, JSON.stringify({before, after}));
  }
  report.scroll = await profileScroll(browser, url);
  await writeFile(join(dir, "frame-profile.json"), JSON.stringify(report, null, 2) + "\n");
  const sizes = report.silhouettes.map(
    (s) => `| ${s.viewport.join("×")} | ${s.dpr} | ${s.bounds.join(", ")} | ${s.width} |`,
  );
  await writeFile(
    join(dir, "verification.md"),
    [
      "# ReviewScope v8 · NEEDS_OWNER_UX_APPROVAL",
      "",
      `Production build \`${commit}\`. Chromium ${report.browser}, Linux. Continuous wheel recording and interactions at 1920×1000, DPR 1. Playwright mouse.wheel, pauses and six short reversals during the round trip.`,
      "",
      "| Viewport | DPR | Left, top, right, bottom (CSS px) | Silhouette width |",
      "|---|---|---|---|",
      ...sizes,
      "",
      "Bounds measured on particle pixels with alpha ≥ 7/255, including the visible halo. Selected company alpha was sampled over 6.8 seconds before the dim screenshot.",
      "",
      "before-home.png and hero.png use the same viewport and zoom. The two sets of quarter frames share their endpoints (hero, atlas, method). method-step-1/2 show the actual panel resize at 1000×900.",
      "",
      "Local visual inspection of the matching build and device limits are recorded in the Draft PR handoff.",
      "",
      "See frame-profile.json for wheel rAF intervals. These timings do not measure compositor presentation on the owner's laptop.",
      "",
      "390×844 and 320×700 used emulated touch. The narrow proof screenshot shows the modal over the map. Local visual review and device limits are in the PR handoff.",
      "",
    ].join("\n"),
  );
  console.log(dir);
} finally {
  await browser?.close();
  await baseline?.close();
  await server.close();
}
