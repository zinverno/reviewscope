import geometry from "./eurasia.svg?raw";
import { places, project, type PlaceId } from "./network";

export const W = 615,
  H = 305;
export const clamp = (x: number) => Math.max(0, Math.min(1, x));
export const mix = (a: number, b: number, t: number) => a + (b - a) * t;
export const smooth = (t: number) => t * t * (3 - 2 * t);

type Target = { x: number; y: number; alpha: number };
export type Dot = {
  id: number;
  x: number;
  y: number;
  phase: number;
  hero: Target;
  atlas: Target;
  method: Target;
  groupX: number;
  groupY: number;
};

export function sampleLand(ctx: CanvasRenderingContext2D): Dot[] {
  const pathNode = new DOMParser().parseFromString(geometry, "image/svg+xml").querySelector("path");
  const land = new Path2D(pathNode?.getAttribute("d") ?? "");
  const samples: Array<[number, number]> = [];
  for (let y = 4; y < H - 3; y += 5)
    for (let x = 4; x < W - 3; x += 5) if (ctx.isPointInPath(land, x, y)) samples.push([x, y]);
  return samples.map(([x, y], id) => ({
    id,
    x,
    y,
    phase: ((id * 0.61803398875) % 1) * 2 * Math.PI,
    hero: { x: 0, y: 0, alpha: 0 },
    atlas: { x: 0, y: 0, alpha: 0.64 },
    method: { x: 0, y: 0, alpha: 0 },
    groupX: 0,
    groupY: 0,
  }));
}

let dotsCache: Dot[] | null = null;
export function landDots(): Dot[] {
  if (!dotsCache) {
    const ctx = document.createElement("canvas").getContext("2d");
    if (!ctx) throw new Error("Canvas 2D is required for the prototype field");
    dotsCache = sampleLand(ctx);
  }
  return dotsCache;
}

let assignmentCache: Map<PlaceId, number> | null = null;
export function placeParticles(): Map<PlaceId, number> {
  if (assignmentCache) return assignmentCache;
  const dots = landDots();
  const ranked = places
    .map((place) => {
      const [px, py] = project(place);
      const nearest = dots
        .map((dot, id) => ({ id, distance: (dot.x - px) ** 2 + (dot.y - py) ** 2 }))
        .sort((a, b) => a.distance - b.distance || a.id - b.id);
      return { place, nearest };
    })
    .sort(
      (a, b) =>
        a.nearest[0].distance - b.nearest[0].distance || a.place.id.localeCompare(b.place.id),
    );
  const taken = new Set<number>(),
    result = new Map<PlaceId, number>();
  for (const { place, nearest } of ranked) {
    const pick = nearest.find((candidate) => !taken.has(candidate.id));
    if (!pick) continue;
    taken.add(pick.id);
    result.set(place.id, pick.id);
    if (nearest[0].distance > 24 ** 2 && import.meta.env.DEV)
      console.warn(`Company ${place.id} is far from the decorative land grid`);
  }
  assignmentCache = result;
  return result;
}

export function particleCenter(place: PlaceId) {
  const dot = landDots()[placeParticles().get(place) ?? 0];
  if (!dot) throw new Error(`No particle assigned to ${place}`);
  return { x: dot.x, y: dot.y };
}

type Box = { x: number; y: number; width: number; height: number };

export function arrangeGrid(dots: Dot[], target: "hero" | "method", box: Box) {
  const columns = Math.max(1, Math.round(Math.sqrt((dots.length * box.width) / box.height)));
  const rows = Math.ceil(dots.length / columns);
  const step = Math.min(box.width / columns, box.height / rows);
  const ordered = [...dots].sort((a, b) => a.y - b.y || a.x - b.x);
  for (let row = 0; row < rows; row++) {
    const band = ordered.slice(row * columns, (row + 1) * columns).sort((a, b) => a.x - b.x);
    band.forEach((dot, col) => {
      const dx = (col - (band.length - 1) / 2) * step;
      const dy = (row - (rows - 1) / 2) * step;
      const edge = Math.max(Math.abs((dx * 2) / box.width), Math.abs((dy * 2) / box.height));
      if (target === "method") {
        dot.groupX = box.x + box.width / 2;
        dot.groupY = box.y + box.height / 2;
      }
      dot[target] = {
        x: box.x + box.width / 2 + dx,
        y: box.y + box.height / 2 + dy,
        alpha: (target === "hero" ? 0.54 : 0.36) * smooth(clamp((1 - edge) * 4)),
      };
    });
  }
}

// Reuse the light profile of a particle; only its centre and brightness change each frame.
function sprite(color: string, company: boolean) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 96;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Canvas 2D is required for particle lighting");
  const glow = ctx.createRadialGradient(48, 48, 0, 48, 48, 48);
  for (const [stop, alpha] of [
    [0, 1],
    [0.11, 1],
    [0.18, company ? 0.75 : 0.9],
    [0.25, company ? 0.56 : 0.4],
    [0.4, company ? 0.34 : 0.21],
    [0.7, 0.09],
    [1, 0],
  ]) {
    glow.addColorStop(stop, `rgba(${color},${alpha})`);
  }
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, 96, 96);
  return canvas;
}

export function fieldRenderer(ctx: CanvasRenderingContext2D, dots: Dot[]) {
  const neutral = sprite("213,228,240", false);
  const red = sprite("255,76,67", true);
  const blue = sprite("94,184,255", true);
  return (
    first: number,
    second: number,
    time: number,
    highlight: Map<number, boolean>,
    mapRadius: number,
    top: number,
    bottom: number,
  ) => {
    const from = second > 0 ? "atlas" : "hero";
    const to = second > 0 ? "method" : "atlas";
    const t = second > 0 ? second : first;
    const accent = smooth(clamp((first - 0.68) / 0.32)) * (1 - smooth(clamp(second / 0.18)));
    const radius = mix(mix(5.6, mapRadius, first), 5, second);
    const gather = 0.34 * Math.sin(Math.PI * second);
    for (const dot of dots) {
      const a = dot[from],
        b = dot[to];
      const x = mix(mix(a.x, b.x, t), dot.groupX, gather);
      const y = mix(mix(a.y, b.y, t), dot.groupY, gather);
      if (y < top - 22 || y > bottom + 22) continue;
      const light = (1 + Math.sin(time * (0.00142 + (dot.id % 9) * 0.000045) + dot.phase)) / 2;
      const selected = highlight.get(dot.id);
      const lit = selected !== undefined && accent > 0;
      const r = lit ? mix(radius, 20, accent) : radius;
      const alpha = mix(a.alpha, b.alpha, t) * (0.53 + 0.47 * light);
      ctx.globalAlpha = lit ? mix(alpha, 0.85 + 0.15 * light, accent) : alpha;
      ctx.drawImage(lit ? (selected ? red : blue) : neutral, x - r, y - r, r * 2, r * 2);
    }
  };
}
