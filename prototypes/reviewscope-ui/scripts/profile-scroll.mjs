/** Measure real wheel-driven morph frames without recording, using only this page's context. */
export async function profileScroll(browser, url, reversals = false) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  try {
    await page.addInitScript(() => {
      const raf = window.requestAnimationFrame.bind(window);
      window.requestAnimationFrame = (cb) =>
        raf((t) => {
          window.displayFrame = t;
          const start = performance.now(), count = window.draws?.length;
          cb(t);
          if (window.draws && window.draws.length > count) window.paintTimes.push(performance.now() - start);
        });
      const context = CanvasRenderingContext2D.prototype;
      for (const op of ["clearRect", "putImageData"]) {
        const native = context[op];
        context[op] = function (...args) {
          if (this.canvas.matches?.(".point-field"))
            window.draws?.push([window.displayFrame, scrollY]);
          return native.apply(this, args);
        };
      }
    });
    await page.goto(url, { waitUntil: "networkidle" });
    await page.mouse.move(500, 600);
    const initial = reversals ? 730 : 80;
    await page.mouse.wheel(0, initial);
    await page.waitForFunction((y) => scrollY >= y - 1, initial);
    await page.evaluate(() => {
      window.framesSeen = [];
      window.paintTimes = [];
      window.draws = [];
      requestAnimationFrame(function sample(t) {
        if (!window.framesSeen) return;
        window.framesSeen.push([t, scrollY]);
        requestAnimationFrame(sample);
      });
    });
    for (const direction of reversals ? [1, -1, 1, -1, 1, -1, 1, -1] : [1, -1]) {
      for (let i = 0; i < (reversals ? 12 : 48); i++) await page.mouse.wheel(0, 12 * direction);
    }
    await page.waitForTimeout(90);
    const [frames, paints, costs] = await page.evaluate(() => {
      const result = [window.framesSeen, window.draws, window.paintTimes.sort((a,b) => a-b)];
      window.framesSeen = window.draws = null;
      return result;
    });
    const changes = frames.filter((v, i) => i && v[1] !== frames[i - 1][1]);
    const [start, finish] = [changes[0][0], changes.at(-1)[0]];
    const active = frames.filter(([t]) => t >= start && t <= finish);
    const gaps = active
      .slice(1)
      .map(([t], i) => t - active[i][0])
      .sort((a, b) => a - b);
    const painted = changes.filter(([t, y]) =>
      paints.some(([frame, py]) => py === y && frame === t),
    );
    return {
      browser: browser.browserType().name(),
      version: browser.version(),
      viewport: "1920x1080",
      dpr: await page.evaluate(() => devicePixelRatio),
      durationMs: Math.round(finish - start),
      paintCallbackMedianMs: +costs[Math.floor(costs.length * 0.5)].toFixed(2),
      paintCallbackP95Ms: +costs[Math.floor(costs.length * 0.95)].toFixed(2),
      input: reversals
        ? "96 wheel events, 8 short runs near the atlas, 12 CSS px each"
        : "96 wheel events, one down/up pass, 12 CSS px each",
      recording: false,
      rafHz: +((1000 * (active.length - 1)) / (finish - start)).toFixed(1),
      changedScrollFrames: changes.length,
      paintedScrollFrames: painted.length,
      missingScrollFrames: changes.length - painted.length,
      geometryHz: +((1000 * (painted.length - 1)) / (finish - start)).toFixed(1),
      gapMedianMs: +gaps[Math.floor(gaps.length * 0.5)].toFixed(1),
      gapP95Ms: +gaps[Math.floor(gaps.length * 0.95)].toFixed(1),
      gapMaxMs: +gaps.at(-1).toFixed(1),
      gapsOver50ms: gaps.filter((t) => t > 50).length,
    };
  } finally {
    await page.close();
  }
}
