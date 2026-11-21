import { clamp, smooth } from "./pointTexture";

export type ScenePhase = "HERO_HOLD" | "MORPH" | "ATLAS_HOLD" | "RELEASE";

const PHASE_RANGES = [
  { phase: "HERO_HOLD", from: 0, to: 0.3 },
  { phase: "MORPH", from: 0.3, to: 1.1 },
  { phase: "ATLAS_HOLD", from: 1.1, to: 1.8 },
  { phase: "RELEASE", from: 1.8, to: Infinity },
] as const;

export function progressFor(scrollY: number, storyTop: number, stageHeight: number) {
  if (stageHeight <= 0) return 0;
  return (scrollY - storyTop) / stageHeight;
}

export function phaseFor(progress: number): ScenePhase {
  const found = PHASE_RANGES.find((range) => progress < range.to);
  return found ? found.phase : "RELEASE";
}

export function morphFor(progress: number, layout: "fit" | "flow", reduced: boolean, flowRange: { from: number; to: number }) {
  if (layout === "fit") {
    return smooth(clamp((progress - 0.3) / 0.8));
  }
  if (reduced) {
    return progress >= flowRange.to ? 1 : 0;
  }
  return smooth(clamp((progress - flowRange.from) / (flowRange.to - flowRange.from)));
}

export function sceneTransitionFor(progress: number, layout: "fit" | "flow", morph: number) {
  if (layout === "fit") {
    return {
      heroOut: smooth(clamp((progress - 0.34) / 0.5)),
      atlasIn: smooth(clamp((progress - 0.55) / 0.45)),
    };
  }
  return {
    heroOut: morph,
    atlasIn: morph,
  };
}

export function sceneScrollTarget(page: string): number | null {
  if (typeof window === "undefined") return null;
  const landing = document.querySelector<HTMLElement>(".landing");
  const story = document.getElementById("story");
  const stage = document.querySelector<HTMLElement>(".story-stage");
  if (page === "home") return 0;
  if (page !== "features") return null;
  if (!story || !stage || landing?.dataset.layout !== "fit") {
    const target = document.getElementById("features");
    if (!target) return null;
    return target.getBoundingClientRect().top + window.scrollY + 8;
  }
  return story.getBoundingClientRect().top + window.scrollY + stage.clientHeight * 1.34;
}
