import { clamp, smooth } from "./pointTexture";

export const ramp = (value: number, start: number, end: number) =>
  smooth(clamp((value - start) / (end - start)));

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
