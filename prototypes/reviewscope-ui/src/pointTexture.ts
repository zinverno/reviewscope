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

// Mix a stable seed into two signed components, for both cell offsets and drift samples.
function hashPair(seed: number) {
  let bits = Math.imul(seed + 1, 0x9e3779b1);
  bits = Math.imul(bits ^ (bits >>> 16), 0x85ebca6b);
  bits ^= bits >>> 13;
  return [(bits & 0xffff) / 32767.5 - 1, (bits >>> 16) / 32767.5 - 1];
}

export function arrangeHero(dots: Dot[], box: Box) {
  const columns = Math.max(1, Math.round(Math.sqrt((dots.length * box.width) / box.height)));
  const rows = Math.ceil(dots.length / columns);
  const step = Math.min(box.width / columns, box.height / rows);
  const ordered = [...dots].sort((a, b) => a.y - b.y || a.x - b.x);
  for (let row = 0; row < rows; row++) {
    const band = ordered.slice(row * columns, (row + 1) * columns).sort((a, b) => a.x - b.x);
    band.forEach((dot, col) => {
      const dx = (col - (band.length - 1) / 2) * step;
      const dy = (row - (rows - 1) / 2) * step;
      const nx = (dx * 2) / box.width,
        ny = (dy * 2) / box.height;
      const [jx, jy] = hashPair(dot.id);
      const fringe = smooth(clamp((Math.hypot(nx, ny) - 0.73) / 0.27));
      const jitter = step * mix(0.36, 0.65, fringe);
      // Bend neighbouring rows together, with a wider lower shoulder and a broken, fading rim.
      const bendX = 0.13 * Math.sin(4.4 * ny - 0.2) + 0.07 * nx * Math.sin(5 * ny + 1.2);
      const bendY = 0.11 * Math.sin(3.8 * nx + ny) - 0.08 * nx + 0.05 * Math.cos(6 * nx - ny);
      const coverage = clamp((1 - fringe) * (1 + fringe * (jx + jy)));
      // Leave room for the bent edge, its halo, and the small wandering offset.
      dot.hero = {
        x: box.x + (0.5 + 0.45 * (nx + bendX)) * box.width + jitter * jx,
        y: box.y + (0.5 + 0.45 * (ny + bendY)) * box.height + jitter * jy,
        alpha: 0.54 * coverage,
      };
    });
  }
}

// Walk the four straight sides and their tangent quarter circles at constant spacing.
export function arrangeContour(group: Dot[], box: Box, radius: number) {
  const { x, y, width, height } = box;
  const r = Math.min(radius, width / 2, height / 2);
  const horizontal = width - 2 * r,
    vertical = height - 2 * r;
  const arc = (Math.PI * r) / 2;
  const half = horizontal + vertical + 2 * arc;
  const visible = Math.min(group.length, Math.round((2 * half) / 8));
  const targets = group.map((_, i) => {
    const along = (i * 2 * half) / group.length;
    const d = along % half;
    let px: number, py: number;
    if (d < horizontal) {
      px = r + d;
      py = 0;
    } else if (d < horizontal + arc) {
      const angle = (d - horizontal) / r;
      px = width - r + r * Math.sin(angle);
      py = r - r * Math.cos(angle);
    } else if (d < half - arc) {
      px = width;
      py = r + d - horizontal - arc;
    } else {
      const angle = (d - (half - arc)) / r;
      px = width - r + r * Math.cos(angle);
      py = height - r + r * Math.sin(angle);
    }
    if (along >= half) {
      px = width - px;
      py = height - py;
    }
    const lit =
      Math.floor(((i + 1) * visible) / group.length) !== Math.floor((i * visible) / group.length);
    return { x: x + px, y: y + py, alpha: lit ? 0.78 : 0 };
  });

  // Pair angular neighbours so a coast approaches the facing side of its panel.
  const cx = group.reduce((sum, dot) => sum + dot.atlas.x, 0) / group.length;
  const cy = group.reduce((sum, dot) => sum + dot.atlas.y, 0) / group.length;
  const bearing = (point: Target, centerX: number, centerY: number) =>
    Math.atan2(point.y - centerY, point.x - centerX);
  group.sort((a, b) => bearing(a.atlas, cx, cy) - bearing(b.atlas, cx, cy));
  targets.sort(
    (a, b) => bearing(a, x + width / 2, y + height / 2) - bearing(b, x + width / 2, y + height / 2),
  );
  group.forEach((dot, i) => {
    dot.method = targets[i];
  });
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
  const noise = Array.from({ length: 1024 }, (_, i) => hashPair(i));
  return (
    first: number,
    second: number,
    time: number | null,
    highlight: Map<number, boolean>,
    mapRadius: number,
    top: number,
    bottom: number,
  ) => {
    const from = second > 0 ? "atlas" : "hero";
    const to = second > 0 ? "method" : "atlas";
    const t = second > 0 ? second : first;
    const accent = smooth(clamp((first - 0.68) / 0.32)) * (1 - smooth(clamp(second / 0.26)));
    const radius = mix(mix(5.6, mapRadius, first), 5, second);
    const separation = second * (1 - second);
    const edgeTravel = t + 0.64 * separation;
    const fadingTravel = t - 0.08 * separation;
    const elapsed = time ?? 0;
    const drift = time === null ? 0 : 2.1 * (1 - smooth(clamp(first / 0.44)));
    const noiseTime = elapsed / 2700;
    const pulseRange = 0.47 + 0.37 * first * (1 - second);
    for (const dot of dots) {
      const a = dot[from],
        b = dot[to];
      const travel = dot.method.alpha ? edgeTravel : fadingTravel;
      let x = mix(a.x, b.x, travel),
        y = mix(a.y, b.y, travel);
      if (drift && a.alpha > 0) {
        const position = noiseTime + dot.phase;
        const knot = Math.floor(position);
        const weight = smooth(position - knot);
        const index = (dot.id * 331 + knot) & 1023;
        const start = noise[index], end = noise[(index + 1) & 1023];
        x += drift * mix(start[0], end[0], weight);
        y += drift * mix(start[1], end[1], weight);
      }
      if (y < top - 22 || y > bottom + 22) continue;
      const light = (1 + Math.sin(elapsed * (0.00142 + (dot.id % 9) * 0.000045) + dot.phase)) / 2;
      const selected = highlight.get(dot.id);
      const lit = selected !== undefined && accent > 0;
      const r = lit ? mix(radius, 20, accent) : radius;
      const alpha = mix(a.alpha, b.alpha, t) * (0.53 + pulseRange * light);
      if (alpha <= 0) continue;
      ctx.globalAlpha = lit ? mix(alpha, 0.85 + 0.15 * light, accent) : alpha;
      ctx.drawImage(lit ? (selected ? red : blue) : neutral, x - r, y - r, r * 2, r * 2);
    }
  };
}
