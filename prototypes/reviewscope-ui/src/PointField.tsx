import { useLayoutEffect, useRef } from "react";
import { mapFrame } from "./network";
import { columns, spacing, clamp, mix, smooth, sampleLand, paintField } from "./pointTexture";

export default function PointField({ active }: { active: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const sample = useRef<ReturnType<typeof sampleLand> | null>(null);
  const clock = useRef(0);
  useLayoutEffect(() => {
    const el = canvas.current;
    if (!el || !active) return;
    const root = el.closest("main");
    const atlas = root?.querySelector<HTMLElement>("#features");
    const plane = atlas?.querySelector<HTMLElement>(".atlas-plane");
    const board = root?.querySelector<HTMLElement>(".board-canvas");
    const head = atlas?.querySelector<HTMLElement>(".atlas-heading");
    const ctx = el.getContext("2d");
    if (!root || !atlas || !plane || !board || !head || !ctx) return;
    if (!sample.current) sample.current = sampleLand(ctx);
    const dots = sample.current;
    let bitmap: ImageData;
    const media = matchMedia("(prefers-reduced-motion: reduce)");
    let width = 0,
      height = 0,
      heroY = 0,
      mapY = 0,
      mapX = 0,
      scale = 1;
    let start = 0,
      end = 1,
      measured = false,
      frame = 0,
      timer = 0,
      last = 0;
    let lastMorph = -1,
      lastOrigin = -1;
    let scrollEnd = 0;
    function finishScroll() {
      clearTimeout(scrollEnd);
      root?.removeAttribute("data-scrolling");
    }
    function onScroll() {
      // Sampling a changing backdrop dominates Firefox's software render path.
      // Keep the actual transparent surface and restore its blur when scroll rests.
      if (!root?.hasAttribute("data-scrolling")) root?.setAttribute("data-scrolling", "");
      clearTimeout(scrollEnd);
      scrollEnd = window.setTimeout(finishScroll, 140);
      request();
    }
    function stop() {
      finishScroll();
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      frame = timer = last = 0;
    }
    function request() {
      if (!frame && !document.hidden) frame = requestAnimationFrame(draw);
    }
    function measure() {
      if (!el || !ctx || !root || !atlas || !plane || !board) return;
      const origin = root.getBoundingClientRect(),
        map = plane.getBoundingClientRect();
      const cards = board.getBoundingClientRect();
      width = origin.width;
      if (!width || !map.width) return;
      const rows = Math.ceil(dots.length / columns);
      const gridHeight = rows * spacing;
      // Both endpoint rectangles live in the landing's document coordinate system.
      heroY = cards.top - origin.top + (cards.height - gridHeight) / 2;
      mapY = map.top - origin.top;
      scale = map.width / mapFrame.width;
      mapX = map.left - origin.left - mapFrame.left * scale;
      height = Math.ceil(Math.max(gridHeight, map.height) + 8);
      el.style.height = `${height}px`;
      const dpr = Math.min(devicePixelRatio || 1, 1.5);
      const bw = Math.round(width * dpr),
        bh = Math.round(height * dpr);
      if (el.width !== bw) el.width = bw;
      if (el.height !== bh) el.height = bh;
      if (!bitmap || bitmap.width !== bw || bitmap.height !== bh)
        bitmap = ctx.createImageData(bw, bh);
      const middle = cards.left - origin.left + cards.width / 2;
      for (const dot of dots) {
        const dx = (dot.col - (columns - 1) / 2) * spacing;
        const dy = (dot.row - (rows - 1) / 2) * spacing;
        dot.hx = middle + dx;
        dot.hy = gridHeight / 2 + dy;
        const r = Math.hypot(
          dx / Math.min((columns * spacing) / 2, cards.width * 0.74),
          dy / (gridHeight / 2),
        );
        dot.alpha = 0.29 * smooth(clamp((1 - r) * 1.7));
      }
      end = atlas.getBoundingClientRect().top + scrollY;
      start = Math.max(origin.top + scrollY, end - innerHeight);
      lastMorph = -1;
      measured = true;
    }
    function draw(now: number) {
      frame = 0;
      if (!el || !ctx || document.hidden) return;
      if (!measured) measure();
      if (!measured) return;
      const scroll = window.scrollY;
      const p = scroll >= end - 1 ? 1 : clamp((scroll - start) / Math.max(1, end - start));
      const morph = media.matches ? Number(p >= 0.5) : smooth(p);
      const origin = mix(heroY, mapY, morph);
      if (origin !== lastOrigin) {
        el.style.transform = `translateY(${origin}px)`;
        lastOrigin = origin;
      }
      // Inside the atlas the canvas shares native document scrolling with the pins.
      // Only the light needs another paint once the centres have reached their targets.
      // Freeze the last sliver of coast above the manual third scene.
      const visibleHeight =
        Math.min(origin + (morph === 1 ? mapFrame.height * scale : height), scroll + innerHeight) -
        Math.max(origin, scroll);
      const visible = visibleHeight > Math.min(0.18 * mapFrame.height * scale, 0.16 * innerHeight);
      const pulsing = visible && !media.matches && morph > 0;
      if (!pulsing) {
        clearTimeout(timer);
        timer = last = 0;
      }
      if (morph === lastMorph && (!pulsing || (last && now - last < 65))) return;
      if (pulsing && last) clock.current += Math.min(now - last, 100) / 1000;
      last = pulsing ? now : 0;
      lastMorph = morph;
      el.dataset.stage = p.toFixed(3);
      paintField(
        bitmap,
        dots,
        morph,
        mapX,
        scale,
        bitmap.width / width,
        clock.current,
        media.matches,
      );
      ctx.putImageData(bitmap, 0, 0);
      if (pulsing) {
        clearTimeout(timer);
        timer = window.setTimeout(request, 75);
      }
    }
    function layout() {
      measured = false;
      request();
    }
    function visibility() {
      stop();
      if (!document.hidden) layout();
    }
    const sizes = new ResizeObserver(layout);
    for (const node of [root, board, head, plane, atlas]) sizes.observe(node);
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", layout);
    document.addEventListener("visibilitychange", visibility);
    media.addEventListener("change", layout);
    layout();
    return () => {
      stop();
      sizes.disconnect();
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", layout);
      document.removeEventListener("visibilitychange", visibility);
      media.removeEventListener("change", layout);
    };
  }, [active]);
  return <canvas ref={canvas} className="point-field" inert />;
}
