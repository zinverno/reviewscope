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
    await page.waitForTimeout(300);
    await link.tap();
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#03/);
    await page.goBack();
    await page.getByRole("button", { name: "Действия с карточкой #03" }).tap();
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
    await page.getByRole("button", { name: "Действия с карточкой #03" }).focus();
    await link.focus();
    assert.equal(await page.locator("#face-03-back").getAttribute("aria-hidden"), "false");
    await page.keyboard.press("Enter");
    await page.waitForSelector(".review-row[aria-pressed=true]");
    assert.match(await page.locator(".review-row[aria-pressed=true]").innerText(), /#03/);
  } finally {
    await page.close();
  }
});
test("scroll phases hold hero/atlas geometry and coast alignment stays stable", async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    await page.addInitScript(() => {
      window.land = [];
      const ctx = CanvasRenderingContext2D.prototype,
        hit = ctx.isPointInPath;
      const clear = ctx.clearRect, draw = ctx.drawImage;
      ctx.clearRect = function (...args) {
        if (this.canvas.matches(".point-field")) window.centres = [];
        return clear.apply(this, args);
      };
      ctx.drawImage = function (image, x, y, w, h, ...rest) {
        if (this.canvas.matches(".point-field")) window.centres.push([x + w / 2, y + h / 2]);
        return draw.call(this, image, x, y, w, h, ...rest);
      };
      ctx.isPointInPath = function (path, x, y, ...args) {
        const inside = hit.call(this, path, x, y, ...args);
        if (inside) window.land.push([x, y]);
        return inside;
      };
    });
    await page.goto(origin);
    const field = page.locator(".point-field"),
      scene = page.locator(".story-stage");
    async function toProgress(p) {
      await page.evaluate((value) => {
        const story = document.getElementById("story"),
          stage = document.querySelector(".story-stage");
        const top = story.getBoundingClientRect().top + scrollY;
        scrollTo(0, top + stage.clientHeight * value);
      }, p);
      await page.waitForFunction(
        (value) => Math.abs(Number(document.querySelector(".story-stage").dataset.progress) - value) < 0.01,
        p,
      );
    }
    function sameBox(a, b) {
      return (
        a &&
        b &&
        Math.abs(a.x - b.x) < 0.6 &&
        Math.abs(a.y - b.y) < 0.6 &&
        Math.abs(a.width - b.width) < 0.6 &&
        Math.abs(a.height - b.height) < 0.6
      );
    }
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

    await toProgress(0.1);
    assert.equal(await scene.getAttribute("data-phase"), "HERO_HOLD");
    const hero = await page.locator(".opening-grid").boundingBox();
    await page.waitForTimeout(600);
    assert.ok(sameBox(hero, await page.locator(".opening-grid").boundingBox()));
    const grid = await field.evaluate(pixels);
    assert.ok(grid.length > 50);
    const centres = () => page.evaluate(() => window.centres);
    const initialCentres = await centres();
    const board = await page.locator(".board-canvas").boundingBox();
    const core = initialCentres.filter(([x, y]) =>
      Math.abs(x - board.x - board.width / 2) < board.width / 4 &&
      Math.abs(y - board.y - board.height / 2) < board.height / 4);
    const rows = points => new Set(points.map(([, y]) => Math.round(y * 1000))).size;
    assert.ok(core.length > 400 && rows(core) < 40, "regular central rows");
    assert.ok(rows(initialCentres) > 4 * rows(core), "outer positions diverge from grid rows");
    assert.ok(Math.min(...initialCentres.map(([x]) => x)) > board.x - board.width * 0.22, "quiet title area");
    assert.equal(await page.getByRole("heading", { name: "За разными словами — общая проблема." }).count(), 1);

    const moving = () => page.locator(".heading-motion span").evaluateAll(faces =>
      faces.filter(f => Number(getComputedStyle(f).opacity) > 0.5)
        .map(f => [f.textContent, f.style.transform]));
    async function verifyReversal(p) {
      await toProgress(p);
      const letters = await moving(), particles = await centres();
      assert.ok(letters.length >= 20);
      assert.equal(await page.locator(".heading-motion").getAttribute("aria-hidden"), "true");
      assert.equal(await page.locator(".heading-motion").evaluate(el => getComputedStyle(el).pointerEvents), "none");
      assert.equal(await page.locator(".story-stage :is(h1,h2) .heading-motion, .heading-motion [tabindex]").count(), 0);
      await page.waitForTimeout(180);
      assert.deepEqual(await moving(), letters);
      assert.deepEqual(await centres(), particles);
      await toProgress(p + 0.08);
      assert.notDeepEqual(await moving(), letters);
      await toProgress(p);
      assert.deepEqual(await moving(), letters);
      assert.deepEqual(await centres(), particles);
    }
    for (const card of await page.locator(".review-slip").all()) {
      const box = await card.boundingBox();
      assert.ok(grid.filter(([x, y]) => x > box.x && x < box.x + box.width && y > box.y && y < box.y + box.height).length > 35);
    }

    await verifyReversal(0.6);
    await toProgress(0.1);
    assert.deepEqual(await centres(), initialCentres);
    await toProgress(0.5);
    assert.equal(await scene.getAttribute("data-phase"), "MORPH");
    const middle = await field.evaluate(pixels),
      middleTransform = await field.evaluate((el) => el.style.transform);
    assert.notDeepEqual(middle, grid);

    await toProgress(1.2);
    assert.equal(await scene.getAttribute("data-phase"), "ATLAS_HOLD");
    assert.equal(await page.locator(".atlas").getAttribute("inert"), null);
    const title = await page.locator("#atlas-title").boundingBox(),
      map = await page.locator(".atlas-plane").boundingBox();
    await page.waitForTimeout(850);
    assert.ok(sameBox(title, await page.locator("#atlas-title").boundingBox()));
    assert.ok(sameBox(map, await page.locator(".atlas-plane").boundingBox()));
    const silhouette = await field.evaluate(pixels);
    const coastX = silhouette.map(([x]) => x);
    assert.ok(Math.max(...coastX) - Math.min(...coastX) > 1440 * 0.85);
    const count = await page.evaluate(() => window.land.length);
    assert.ok(count > 100);
    assert.equal(await checkCoast(), 0);

    await toProgress(1.47);
    assert.equal(await scene.getAttribute("data-phase"), "ATLAS_HOLD");
    assert.ok(sameBox(title, await page.locator("#atlas-title").boundingBox()));
    assert.equal(await checkCoast(), 0);
    await toProgress(1.2);
    assert.ok(sameBox(title, await page.locator("#atlas-title").boundingBox()));
    assert.equal(await checkCoast(), 0);

    await toProgress(0.5);
    const back = await field.evaluate(pixels),
      backTransform = await field.evaluate((el) => el.style.transform);
    assert.equal(backTransform, middleTransform);
    assert.notDeepEqual(back, grid);

    await verifyReversal(1.95);
    const gathering = await field.evaluate(pixels);
    await page.goto(origin + "#how");
    await page.waitForFunction(() => !document.getElementById("how").inert && document.getElementById("features").inert);
    assert.equal(await page.locator(".atlas-details:visible").count(), 0);
    const method = await field.evaluate(pixels);
    assert.notDeepEqual(gathering, method);
    assert.equal(await page.getByRole("heading", {name: "От фразы — к проверяемому изменению."}).count(), 1);
    assert.deepEqual(await moving(), []);
    async function checkPerimeters() {
      await page.waitForFunction(() => {
        const remaining = new Set(window.centres);
        const valid = [...document.querySelectorAll(".step")].every(card => {
          const box = card.getBoundingClientRect(), style = getComputedStyle(card);
          const inset = parseFloat(style.borderTopWidth) / 2;
          const radius = parseFloat(style.borderTopLeftRadius) - inset;
          const halfX = box.width / 2 - inset, halfY = box.height / 2 - inset;
          let count = 0, corners = 0;
          for (const point of remaining) {
            const dx = Math.abs(point[0] - box.x - box.width / 2) - (halfX - radius);
            const dy = Math.abs(point[1] - box.y - box.height / 2) - (halfY - radius);
            const distance = Math.hypot(Math.max(0, dx), Math.max(0, dy)) + Math.min(0, Math.max(dx, dy)) - radius;
            if (Math.abs(distance) > 0.1) continue;
            remaining.delete(point);
            count++;
            if (dx > 0 && dy > 0) corners++;
          }
          const perimeter = 4 * (halfX + halfY - 2 * radius) + 2 * Math.PI * radius;
          return count === Math.round(perimeter / 8) && corners >= 8;
        });
        return valid && remaining.size === 0;
      }, undefined, {timeout: 3000});
    }
    await checkPerimeters();
    await page.setViewportSize({ width: 1000, height: 900 });
    await checkPerimeters();
    const heights = new Set([(await page.locator(".step").first().boundingBox()).height]);
    for (const name of [/Проверить смысл/, /Сравнить изменения/]) {
      const button = page.getByRole("button", {name});
      await button.click();
      await page.getByRole("button", {name, expanded: true}).waitFor();
      await checkPerimeters();
      heights.add((await page.locator(".step").first().boundingBox()).height);
    }
    assert.ok(heights.size > 1, "opening different details changes the measured perimeter");
    await toProgress(1.2);
    assert.equal(await checkCoast(), 0);
    assert.equal(await page.evaluate(() => window.land.length), count);
    await page.goto(origin + "#features");
    await page.waitForFunction(() => document.querySelector(".story-stage")?.dataset.phase === "ATLAS_HOLD");
    assert.equal(await checkCoast(), 0);
  } finally {
    await page.close();
  }
});

test("map markers select the evidence, with a network total and both comparison samples", async () => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, hasTouch: true });
  try {
    await page.goto(origin + "#features");
    await page.waitForFunction(() => Number(document.querySelector(".point-field").dataset.morph) > 0.99);
    const pin = (name) => page.getByRole("button", { name: "На карте: " + name, exact: true });
    const button = (name) => page.getByRole("button", { name, exact: true });
    const panels = page.locator(".atlas-brief .atlas-proof");
    const details = page.locator(".atlas-details");
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
    const action = button("Состав расчёта");
    await action.tap();
    assert.equal(await details.locator("li").count(), 3);
    await button("Закрыть").tap();
    assert.equal(await action.evaluate(el => el === document.activeElement), true);
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
    await action.scrollIntoViewIfNeeded();
    const y = await page.evaluate(() => scrollY);
    await action.tap();
    for (const panel of await details.locator(".atlas-proof").all())
      assert.ok((await panel.locator("li").count()) >= 3);
    assert.equal(await details.locator(".atlas-proof").count(), 2);
    await details.evaluate(el => el.scrollTop = el.scrollHeight);
    await page.keyboard.press("Escape");
    assert.equal(await details.isVisible(), false);
    assert.equal(await action.evaluate(el => el === document.activeElement), true);
    assert.equal(await page.evaluate(() => scrollY), y);
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

test("the hero keeps its field alive at morph zero and drops the permanent guides", async () => {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  try {
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector("canvas").dataset.morph === "0.000");
    for (const text of [
      "Наведите для чтения",
      "Коснитесь для чтения",
      "К точкам на карте",
      "Одна проблема",
      "Общая тема",
      "Сбросить расположение",
    ])
      assert.equal(await page.getByText(text, { exact: false }).count(), 0, text);
    assert.equal(await page.getByRole("button", { name: "Перемещение", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "Действия с карточкой #02" }).count(), 1);
    const mast = await page.locator(".mast").boundingBox(),
      eyebrow = await page.locator(".eyebrow").boundingBox(),
      board = await page.locator(".board-canvas").boundingBox(),
      h1 = await page.locator(".opening h1").boundingBox();
    const gap = eyebrow.y - (mast.y + mast.height);
    assert.ok(gap > 24 && gap < 110, `hero gap is ${gap}px`);
    assert.ok(board.y + board.height < 768, "board fits the first screen");
    assert.ok(h1.y > mast.y && h1.y + h1.height < 768, "headline fits the first screen");
    const read = () =>
      page.locator("canvas").evaluate((el) => {
        const { data } = el.getContext("2d").getImageData(0, 0, el.width, el.height);
        const alphas = [];
        for (let i = 3; i < data.length; i += 160) alphas.push(data[i]);
        return alphas;
      });
    const before = await read();
    await page.waitForTimeout(1700);
    const after = await read();
    const changed = before.filter((a, i) => Math.abs(a - after[i]) > 2).length;
    assert.ok(changed > 20, `only ${changed} alpha samples changed on a still hero`);
    assert.equal(await page.locator("canvas").getAttribute("data-stage"), "0");
  } finally {
    await page.close();
  }
});

test("companies are coloured particles, not drawn marker disks", async () => {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  try {
    await page.goto(origin + "#features");
    await page.waitForFunction(() => Number(document.querySelector("canvas").dataset.morph) > 0.99);
    await page.waitForTimeout(300);
    const pin = (id) => page.locator(`.pin[data-place="${id}"]`);
    for (const id of ["arc", "line", "cape"]) {
      assert.equal(await pin(id).locator("i").count(), 0);
      assert.equal(await pin(id).evaluate((e) => getComputedStyle(e).pointerEvents), "auto");
    }
    assert.equal(
      await page.evaluate(() => new Set([...document.querySelectorAll(".pin")].map((e) => e.dataset.particle)).size),
      3,
    );
    const sample = (id) =>
      pin(id).evaluate((el) => {
        const canvas = document.querySelector("canvas"),
          box = el.getBoundingClientRect(),
          rect = canvas.getBoundingClientRect();
        const x = Math.round(((box.left + box.width / 2 - rect.left) * canvas.width) / rect.width),
          y = Math.round(((box.top + box.height / 2 - rect.top) * canvas.height) / rect.height),
          data = canvas.getContext("2d").getImageData(x, y, 1, 1).data;
        return [data[0], data[1], data[2], data[3]];
      });
    const arcSelected = await sample("arc"),
      lineIdle = await sample("line");
    assert.ok(arcSelected[0] > arcSelected[2], `selected particle should be red: ${arcSelected}`);
    assert.ok(lineIdle[2] > lineIdle[0], `unselected particle should be blue: ${lineIdle}`);
    const arcBox = await pin("arc").boundingBox();
    await page.mouse.click(arcBox.x - 3, arcBox.y + arcBox.height / 2);
    await pin("arc").dispatchEvent("pointerdown", { pointerType: "touch" });
    await pin("arc").dispatchEvent("pointercancel", { pointerType: "touch" });
    await pin("cape").focus();
    await page.keyboard.press("Space");
    assert.equal(await pin("cape").getAttribute("aria-pressed"), "true");
    const line = await pin("line").boundingBox();
    await page.mouse.click(line.x + line.width / 2, line.y + line.height / 2);
    await page.waitForFunction(() => document.querySelector("#features").dataset.selected === "line");
    await page.waitForTimeout(240);
    const arcIdle = await sample("arc"),
      lineSelected = await sample("line");
    assert.ok(arcIdle[2] > arcIdle[0], `hidden particle should be neutral: ${arcIdle}`);
    assert.ok(lineSelected[0] > lineSelected[2], `selected particle should be red: ${lineSelected}`);
  } finally {
    await page.close();
  }
});

test("reduced motion uses the short flow layout with readable scenes and no morph", async () => {
  const page = await browser.newPage({
    viewport: { width: 1366, height: 768 },
    reducedMotion: "reduce",
  });
  try {
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector(".landing")?.dataset.layout === "flow");
    const geometry = await page.evaluate(() => {
      const story = document.getElementById("story"),
        stage = document.querySelector(".story-stage");
      return {
        storyHeight: story.offsetHeight,
        stageHeight: stage.offsetHeight,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth,
      };
    });
    assert.equal(await page.locator(".heading-motion span").count(), 0);
    assert.ok(geometry.storyHeight < geometry.stageHeight * 1.5, JSON.stringify(geometry));
    assert.ok(geometry.scrollWidth <= geometry.innerWidth, JSON.stringify(geometry));
    assert.equal(await page.locator(".opening h1").isVisible(), true);
    await page.getByRole("heading", { name: /Одна точка. Вся сеть/ }).scrollIntoViewIfNeeded();
    assert.equal(await page.getByRole("heading", { name: /Одна точка. Вся сеть/ }).isVisible(), true);
    assert.equal(await page.getByRole("button", { name: "Сравнение", exact: true }).isVisible(), true);
    assert.equal(await page.locator(".atlas").getAttribute("inert"), null);
    await page.locator(".atlas-plane").scrollIntoViewIfNeeded();
    const still = () => page.locator("canvas").evaluate(el => el.toDataURL());
    await page.waitForTimeout(100);
    const initial = await still();
    await page.waitForTimeout(450);
    assert.equal(await still(), initial);
    assert.equal(await page.locator("#how").getAttribute("inert"), null);
  } finally {
    await page.close();
  }
});

test("a long atlas hold keeps geometry still while the light continues", async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  try {
    await page.goto(origin);
    await page.evaluate(() => {
      const story = document.getElementById("story"),
        stage = document.querySelector(".story-stage");
      scrollTo(0, story.getBoundingClientRect().top + scrollY + stage.clientHeight * 1.35);
    });
    await page.waitForFunction(() => document.querySelector(".story-stage").dataset.phase === "ATLAS_HOLD");
    const before = {
      title: await page.locator("#atlas-title").boundingBox(),
      controls: await page.locator(".atlas-controls").boundingBox(),
      map: await page.locator(".atlas-plane").boundingBox(),
      alpha: await page.locator("canvas").evaluate((el) => {
        const { data } = el.getContext("2d").getImageData(0, 0, el.width, el.height);
        const samples = [];
        for (let i = 3; i < data.length; i += 400) samples.push(data[i]);
        return samples;
      }),
    };
    await page.waitForTimeout(6500);
    const after = {
      title: await page.locator("#atlas-title").boundingBox(),
      controls: await page.locator(".atlas-controls").boundingBox(),
      map: await page.locator(".atlas-plane").boundingBox(),
      alpha: await page.locator("canvas").evaluate((el) => {
        const { data } = el.getContext("2d").getImageData(0, 0, el.width, el.height);
        const samples = [];
        for (let i = 3; i < data.length; i += 400) samples.push(data[i]);
        return samples;
      }),
    };
    for (const key of ["title", "controls", "map"]) {
      assert.ok(Math.abs(before[key].x - after[key].x) < 0.6, key);
      assert.ok(Math.abs(before[key].y - after[key].y) < 0.6, key);
    }
    const changed = before.alpha.filter((value, i) => Math.abs(value - after.alpha[i]) > 2).length;
    assert.ok(changed > 20, `only ${changed} alpha samples changed on hold`);
    assert.equal(await page.locator(".story-stage").getAttribute("data-phase"), "ATLAS_HOLD");
    assert.equal(new URL(page.url()).hash, "");
  } finally {
    await page.close();
  }
});

test("narrow phones keep readable content reachable without horizontal overflow", async () => {
  const page = await browser.newPage({ viewport: { width: 320, height: 700 }, hasTouch: true });
  try {
    await page.goto(origin);
    await page.waitForFunction(() => document.querySelector(".landing")?.dataset.layout === "flow");
    assert.equal(await page.locator(".heading-motion span").count(), 0);
    const overflow = () =>
      page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok((await overflow()) <= 0, `first screen overflow is ${await overflow()}px`);
    assert.equal(await page.locator(".opening h1").isVisible(), true);
    const heading = page.getByRole("heading", { name: /Одна точка. Вся сеть/ });
    await heading.scrollIntoViewIfNeeded();
    assert.equal(await heading.isVisible(), true);
    await page.getByRole("button", { name: "Моя сеть", exact: true }).tap();
    assert.match(await page.locator(".atlas-brief .atlas-proof").innerText(), /3\/10/);
    assert.ok((await overflow()) <= 0, `atlas overflow is ${await overflow()}px`);
  } finally {
    await page.close();
  }
});
