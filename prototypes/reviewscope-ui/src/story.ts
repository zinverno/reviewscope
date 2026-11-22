import { clamp, smooth } from "./pointTexture";

export const ramp = (value: number, start: number, end: number) =>
  smooth(clamp((value - start) / (end - start)));

// Integrate a smooth rise in speed at each end, with even travel in the middle.
export function travelProgress(value: number, start: number, end: number) {
  const t = clamp((value - start) / (end - start));
  const edge = 0.3;
  const d = Math.min(t, 1 - t);
  const u = d / edge;
  const distance = d < edge ? edge * u ** 3 * (1 - u / 2) : d - edge / 2;
  const progress = distance / (1 - edge);
  return t <= 0.5 ? progress : 1 - progress;
}

export const desktopStops = { first: [0.15, 1.05], second: [1.55, 2.35] } as const;

export function sceneScrollTarget(page: string): number | null {
  const story = document.getElementById("story");
  const target = document.getElementById(page);
  if (page === "home") return 0;
  if (!story || !target || (page !== "features" && page !== "how")) return null;
  if (story.closest(".landing")?.getAttribute("data-layout") === "flow")
    return target.getBoundingClientRect().top + scrollY;
  const stop = page === "features" ? desktopStops.first[1] + 0.18 : desktopStops.second[1] + 0.08;
  return story.getBoundingClientRect().top + scrollY + window.innerHeight * stop;
}
