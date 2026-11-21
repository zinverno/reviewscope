import geometry from "./eurasia.svg?raw";

export const W = 615,
  H = 305,
  columns = 45,
  spacing = 18;
export const clamp = (x: number) => Math.max(0, Math.min(1, x));
export const mix = (a: number, b: number, t: number) => a + (b - a) * t;
export const smooth = (t: number) => t * t * (3 - 2 * t);

export function sampleLand(ctx: CanvasRenderingContext2D) {
  const path = new DOMParser().parseFromString(geometry, "image/svg+xml").querySelector("path");
  const land = new Path2D(path?.getAttribute("d") ?? "");
  const dots = [];
  for (let y = 4; y < H - 3; y += 6) {
    for (let x = 4; x < W - 3; x += 6) {
      if (!ctx.isPointInPath(land, x, y)) continue;
      const id: number = dots.length;
      dots.push({
        x,
        y,
        col: id % columns,
        row: Math.floor(id / columns),
        phase: Math.floor(12 * ((id * 0.61803399) % 1)),
        hx: 0,
        hy: 0,
        alpha: 0,
      });
    }
  }
  return dots;
}

// A shared radial profile avoids a gradient, blur or draw call per particle.
// Distances and centres are subpixel; the canvas receives one bitmap per paint.
export function paintField(
  image: ImageData,
  dots: ReturnType<typeof sampleLand>,
  morph: number,
  mapX: number,
  scale: number,
  dpr: number,
  time: number,
  still: boolean,
) {
  const { width, height, data } = image;
  data.fill(0);
  const radius = dpr * mix(0.9, Math.min(1.85, Math.max(0.54, scale * 0.8)), morph);
  const glow = smooth(clamp((morph - 0.25) / 0.75));
  const extent = glow ? 3.9 * radius + 0.5 : radius + 0.5;
  const core = new Float32Array(256),
    halo = new Float32Array(256);
  const indexScale = 256 / (extent * extent);
  for (let i = 0; i < 256; i++) {
    const distance = Math.sqrt(i / indexScale);
    core[i] = clamp(radius + 0.5 - distance);
    halo[i] = (1 - core[i]) * glow * 0.6 * Math.exp(-0.44 * (distance / radius) ** 2);
  }
  const lights = Array.from({ length: 12 }, (_, k) =>
    still ? 0.48 : 0.51 + 0.48 * Math.sin(time * (1.22 + k / 22) + k * 2.4),
  );
  for (const dot of dots) {
    const opacity = 255 * mix(dot.alpha, 0.79, morph);
    if (opacity < 1) continue;
    const cx = dpr * mix(dot.hx, mapX + dot.x * scale, morph);
    const cy = dpr * mix(dot.hy, dot.y * scale, morph);
    const left = Math.max(0, Math.floor(cx - extent)),
      right = Math.min(width, Math.ceil(cx + extent));
    const bottom = Math.min(height, Math.ceil(cy + extent));
    const light = lights[dot.phase];
    for (let y = Math.max(0, Math.floor(cy - extent)); y < bottom; y++) {
      const yy = (y + 0.5 - cy) ** 2;
      for (let x = left; x < right; x++) {
        const i = Math.floor(((x + 0.5 - cx) ** 2 + yy) * indexScale);
        if (i >= 256) continue;
        const offset = 4 * (width * y + x);
        const alpha = opacity * (core[i] + halo[i] * light);
        if (data[offset + 3] >= alpha) continue;
        data[offset] = 208;
        data[offset + 1] = 221;
        data[offset + 2] = 230;
        data[offset + 3] = alpha;
      }
    }
  }
}
