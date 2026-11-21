import { useLayoutEffect, useRef } from "react";
import geometry from "./eurasia.svg?raw";

const W = 615,
  H = 305,
  TAU = 2 * Math.PI;
const unit = (n: number) => n - Math.floor(n);
const bound = (n: number) => Math.min(1, Math.max(0, n));
const ease = (n: number) => {
  const t = bound(n);
  return t * t * (3 - 2 * t);
};
const mix = (a: number, b: number, t: number) => a + (b - a) * t;
const inks = ["#b8cad7", "#a6a8aa", "#76accc", "#d09290"];

function sampleLand(ctx: CanvasRenderingContext2D) {
  const outline = new DOMParser()
    .parseFromString(geometry, "image/svg+xml")
    .querySelector("path")
    ?.getAttribute("d");
  if (!outline) throw Error("Отсутствует геометрия Евразии");
  const land = new Path2D(outline);
  const dots: {
    id: number;
    x: number;
    y: number;
    u: number;
    v: number;
    phase: number;
    size: number;
    ink: number;
  }[] = [];
  for (let y = 4; y < H - 3; y += 6)
    for (let x = 4; x < W - 3; x += 6) {
      if (!ctx.isPointInPath(land, x, y, "nonzero")) continue;
      const id = dots.length,
        phase = TAU * unit(id * 0.618033989);
      // Low-discrepancy starting positions keep the field dispersed and reproducible.
      const u = unit(0.31 + id * 0.754877666),
        v = unit(0.67 + id * 0.569840296);
      dots.push({
        id,
        x,
        y,
        u: 0.035 + 0.94 * Math.sqrt(u),
        v: 0.025 + 0.95 * v,
        phase,
        size: 0.78 + 0.42 * unit(id * 0.414213562),
        ink: Math.floor(4 * unit(id * 0.324717957)),
      });
    }
  return inks.map((_, i) => dots.filter((p) => p.ink === i));
}

export default function PointField({ active }: { active: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const particles = useRef<ReturnType<typeof sampleLand> | null>(null);
  const clock = useRef(0);
  useLayoutEffect(() => {
    const el = canvas.current;
    if (!el || !active) return;
    const parent = el.closest("main");
    const atlas = parent?.querySelector<HTMLElement>("#features"),
      plane = parent?.querySelector<HTMLElement>(".atlas-plane"),
      method = parent?.querySelector<HTMLElement>("#how");
    const ctx = el.getContext("2d");
    if (!ctx || !parent || !atlas || !plane || !method) return;
    if (!particles.current) particles.current = sampleLand(ctx);
    const dots = particles.current;
    const media = matchMedia("(prefers-reduced-motion: reduce)");
    let width = 0,
      height = 0,
      mapX = 0,
      mapY = 0,
      scale = 1,
      start = 0,
      end = 1,
      outStart = 0,
      outEnd = 1;
    let frame = 0,
      last = 0,
      visible = true;
    function stop() {
      cancelAnimationFrame(frame);
      frame = 0;
      last = 0;
    }
    function request() {
      if (!frame && visible && !document.hidden) frame = requestAnimationFrame(draw);
    }
    function draw(now: number) {
      frame = 0;
      if (!el || !ctx || !visible || document.hidden) return;
      const y = window.scrollY,
        p = bound((y - start) / (end - start)),
        out = bound((y - outStart) / (outEnd - outStart));
      const staticMode = media.matches;
      const exit = staticMode ? Number(out >= .5) : ease(out);
      const morph = staticMode ? Number(p >= 0.5) : ease(p);
      // Once assembled, the projected centres travel with the HTML marker plane.
      const top = mapY - (staticMode ? y : Math.max(end, y));
      const moving = !staticMode && exit < 1;
      if (moving && last && now - last < 1000 / 24) {
        request();
        return;
      }
      if (moving && last) clock.current += Math.min(now - last, 70) * 0.001;
      last = moving ? now : 0;
      const t = clock.current;
      ctx.clearRect(0, 0, width, height);
      const stage = p.toFixed(3);
      if (el.dataset.stage !== stage) el.dataset.stage = stage;
      ctx.globalAlpha = mix(mix(0.45, 0.73, morph), 0.12, exit);
      for (const [ink, group] of dots.entries()) {
        ctx.beginPath();
        ctx.fillStyle = inks[ink];
        for (const dot of group) {
          const amplitude = staticMode
            ? 0
            : mix(2.8, Math.min(0.55, scale * 0.34), morph) * (1 - exit);
          const hx = 4 + dot.u * (width - 8),
            hy = 4 + dot.v * (height - 8);
          const px =
            mix(mix(hx, mapX + dot.x * scale, morph), hx, exit) +
            amplitude * Math.sin(t * 0.76 + dot.phase);
          const py =
            mix(mix(hy, top + dot.y * scale, morph), hy, exit) +
            amplitude * Math.cos(t * 0.61 + dot.phase);
          const radius =
            dot.size *
            mix(Math.max(1.25, Math.min(1.95, width / 690)), Math.max(0.63, scale * 1.22), morph);
          if (py < -radius || py > height + radius) continue;
          ctx.moveTo(px + radius, py);
          ctx.arc(px, py, radius, 0, TAU);
        }
        ctx.fill();
      }
      if (moving) request();
    }
    function measure() {
      if (!el || !ctx || !parent || !atlas || !method || !plane) return;
      const size = el.getBoundingClientRect(),
        map = plane.getBoundingClientRect();
      width = size.width;
      height = size.height;
      if (!width || !height) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      if (el.width !== Math.round(width * dpr)) el.width = Math.round(width * dpr);
      if (el.height !== Math.round(height * dpr)) el.height = Math.round(height * dpr);
      ctx.setTransform(el.width / width, 0, 0, el.height / height, 0, 0);
      const scroll = window.scrollY;
      mapX = map.left - size.left;
      mapY = map.top + scroll;
      scale = map.width / W;
      end = atlas.getBoundingClientRect().top + scroll;
      start = Math.max(parent.getBoundingClientRect().top + scroll, end - height);
      const third = method.getBoundingClientRect().top + scroll;
      outEnd = Math.max(
        end + 2,
        Math.min(third - height * 0.08, parent.getBoundingClientRect().bottom + scroll - height),
      );
      outStart = Math.max(end + 1, Math.min(outEnd - 1, third - height * 0.68));
      stop();
      if (visible && !document.hidden) draw(performance.now());
    }
    function visibility() {
      stop();
      if (!document.hidden) measure();
    }
    const sizes = new ResizeObserver(measure);
    for (const node of [el, parent, atlas, plane, method]) sizes.observe(node);
    const observer = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      if (visible) measure();
      else stop();
    });
    observer.observe(parent);
    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", measure);
    document.addEventListener("visibilitychange", visibility);
    media.addEventListener("change", measure);
    measure();
    return () => {
      stop();
      observer.disconnect();
      sizes.disconnect();
      window.removeEventListener("scroll", request);
      window.removeEventListener("resize", measure);
      document.removeEventListener("visibilitychange", visibility);
      media.removeEventListener("change", measure);
    };
  }, [active]);
  return (
    <div className="field-anchor" aria-hidden="true">
      <canvas ref={canvas} className="point-field" />
    </div>
  );
}
