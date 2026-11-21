import geometry from "./eurasia.svg?raw";
import { places, project, type PlaceId } from "./network";

export const W = 615,
  H = 305,
  columns = 57,
  spacing = 15.5;
export const clamp = (x: number) => Math.max(0, Math.min(1, x));
export const mix = (a: number, b: number, t: number) => a + (b - a) * t;
export const smooth = (t: number) => t * t * (3 - 2 * t);

const DOT_COLOR = [208, 221, 230, 255] as const;
const BLUE = [100, 181, 246, 255] as const;
const RED = [229, 57, 53, 255] as const;
const wobble = 0.26;

export type Dot = {
  id: number;
  x: number;
  y: number;
  col: number;
  row: number;
  phase: number;
  hx: number;
  hy: number;
  alpha: number;
};

export function sampleLand(ctx: CanvasRenderingContext2D): Dot[] {
  const pathNode = new DOMParser().parseFromString(geometry, "image/svg+xml").querySelector("path");
  const land = new Path2D(pathNode?.getAttribute("d") ?? "");
  const samples: Array<[number, number]> = [];
  for (let y = 4; y < H - 3; y += 5)
    for (let x = 4; x < W - 3; x += 5) if (ctx.isPointInPath(land, x, y)) samples.push([x, y]);
  return samples.map(([x, y], i) => {
    const col = i % columns,
      row = Math.floor(i / columns),
      phase = (i * 0.61803398875) % 1;
    return {
      id: i,
      x,
      y,
      col,
      row,
      phase: phase * Math.PI * 2,
      hx: Math.sin(phase * 17.3) * wobble,
      hy: Math.cos(phase * 23.7) * wobble,
      alpha: 0,
    };
  });
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
    .sort((a, b) => a.nearest[0].distance - b.nearest[0].distance || a.place.id.localeCompare(b.place.id));
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

const profiles = new Map<string, Float32Array>();
function profile(radius: number, kind: "core" | "halo") {
  const key = `${kind}:${Math.round(radius * 20) / 20}`,
    size = Math.ceil(radius) * 2 + 1,
    extent = (size - 1) / 2;
  let table = profiles.get(key);
  if (!table) {
    table = new Float32Array(size * size);
    for (let y = -extent; y <= extent; y++)
      for (let x = -extent; x <= extent; x++) {
        const distance = Math.hypot(x, y);
        table[(y + extent) * size + x + extent] =
          kind === "core"
            ? clamp(radius + 0.5 - distance)
            : distance >= radius
              ? 0
              : (1 - distance / radius) ** 2;
      }
    profiles.set(key, table);
  }
  return { table, size, extent };
}

export function paintField(
  image: ImageData,
  dots: Dot[],
  morph: number,
  mapX: number,
  scale: number,
  dpr: number,
  time: number,
  boardWidth: number,
  highlight: { selected: string; visible: string[] } = { selected: "", visible: [] },
) {
  const { width, height, data } = image;
  data.fill(0);
  const rows = Math.max(...dots.map((dot) => dot.row)) + 1,
    gridWidth = columns * spacing,
    gridHeight = rows * spacing,
    radius = dpr * mix(1.1, Math.min(1.85, Math.max(0.54, scale * 0.8)), morph),
    glow = 0.42 + 0.12 * morph,
    edgeX = Math.min(gridWidth / 2, boardWidth * 0.74);
  const assignment = placeParticles(),
    company = new Map<number, readonly [number, number, number, number]>();
  for (const id of highlight.visible) {
    const particle = assignment.get(id as PlaceId);
    if (particle !== undefined)
      company.set(particle, id === highlight.selected ? RED : BLUE);
  }
  const seconds = time / 1000;
  for (const dot of dots) {
    const cx = mix(dot.hx, mapX + dot.x * scale, morph) * dpr,
      cy = mix(dot.hy, dot.y * scale, morph) * dpr;
    if (cx < -14 || cy < -14 || cx > width + 14 || cy > height + 14) continue;
    const edge = Math.hypot(
      ((dot.col - (columns - 1) / 2) * spacing) / edgeX,
      ((dot.row - (rows - 1) / 2) * spacing) / (gridHeight / 2),
    );
    const heroAlpha = 0.29 * smooth(clamp((1 - edge) * 1.7));
    const opacity = mix(heroAlpha, 0.79, morph);
    if (opacity < 0.01) continue;
    const light =
      0.12 + 0.83 * (0.5 + 0.5 * Math.sin(seconds * (1.42 + (dot.id % 9) * 0.045) + dot.phase));
    const tint = company.get(dot.id),
      core = profile(radius * (tint ? 1.14 : 1), "core"),
      halo = profile(radius * (tint ? 4.8 : 4.1), "halo"),
      haloGain = tint ? 0.56 + 0.34 * light : glow * (0.25 + 1.15 * light),
      coreGain = 0.82 + 0.18 * light,
      color = tint ?? DOT_COLOR;
    const ix = Math.round(cx),
      iy = Math.round(cy);
    for (let oy = -halo.extent; oy <= halo.extent; oy++) {
      const py = iy + oy;
      if (py < 0 || py >= height) continue;
      const hi = (oy + halo.extent) * halo.size + halo.extent;
      const inCoreY = Math.abs(oy) <= core.extent;
      for (let ox = -halo.extent; ox <= halo.extent; ox++) {
        const px = ix + ox;
        if (px < 0 || px >= width) continue;
        const alpha = Math.min(
          1,
          opacity *
            (halo.table[hi + ox] * haloGain +
              (inCoreY && Math.abs(ox) <= core.extent
                ? core.table[(oy + core.extent) * core.size + ox + core.extent] * coreGain
                : 0)),
        );
        const offset = (py * width + px) * 4;
        if (alpha <= data[offset + 3] / 255) continue;
        data[offset] = color[0];
        data[offset + 1] = color[1];
        data[offset + 2] = color[2];
        data[offset + 3] = alpha * 255;
      }
    }
  }
}
