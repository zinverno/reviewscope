import { useLayoutEffect, useRef } from "react";
import { measureHeadings } from "./headingMotion";
import { mapFrame, type PlaceId } from "./network";
import {
  arrangeContour,
  arrangeHero,
  fieldRenderer,
  landDots,
  placeParticles,
} from "./pointTexture";
import { desktopStops, ramp } from "./story";

function boxWithin(element: HTMLElement, root: HTMLElement) {
  let x = 0,
    y = 0;
  for (
    let node: HTMLElement | null = element;
    node && node !== root;
    node = node.offsetParent as HTMLElement | null
  ) {
    x += node.offsetLeft;
    y += node.offsetTop;
  }
  return { x, y, width: element.offsetWidth, height: element.offsetHeight };
}

export default function PointField({ active }: { active: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context || !active) return;
    const node = canvas,
      ctx = context;
    const stage = node.closest<HTMLElement>(".story-stage");
    const story = node.closest<HTMLElement>(".story");
    const host = node.closest<HTMLElement>(".landing");
    if (!stage || !story || !host) return;
    function sceneElement(selector: string) {
      const element = stage?.querySelector<HTMLElement>(selector);
      if (!element) throw new Error(`Missing story element: ${selector}`);
      return element;
    }
    const opening = sceneElement(".opening"),
      atlas = sceneElement(".atlas"),
      method = sceneElement(".method");
    const board = sceneElement(".board-canvas"),
      map = sceneElement(".atlas-plane");
    const cards = [...stage.querySelectorAll<HTMLElement>(".step")];
    const layer = sceneElement(".heading-motion");
    const headings = [
      sceneElement(".opening h1"),
      sceneElement("#atlas-title"),
      sceneElement("#method-title"),
    ];
    let moveHeadings: ReturnType<typeof measureHeadings> | undefined;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    const dots = landDots();
    const westToEast = [...dots].sort((a, b) => a.x - b.x || a.y - b.y);
    const draw = fieldRenderer(ctx, dots);
    const xs = dots.map((dot) => dot.x),
      ys = dots.map((dot) => dot.y);
    const landWidth = Math.max(...xs) - Math.min(...xs);
    const landHeight = Math.max(...ys) - Math.min(...ys);
    let frame = 0,
      measureFrame = 0,
      lastTime = 0,
      clock = 0;
    let width = 0,
      height = 0,
      dpr = 1,
      storyTop = 0,
      fit = true;
    let mapRadius = 0,
      flowStart = 0,
      flowMap = 0,
      flowLeave = 0,
      flowMethod = 0;
    let lastProgress = NaN;
    const highlight = new Map<number, boolean>();
    function selection() {
      highlight.clear();
      for (const id of atlas.dataset.places?.split(",") ?? []) {
        const particle = placeParticles().get(id as PlaceId);
        if (particle !== undefined) highlight.set(particle, atlas.dataset.selected === id);
      }
      request();
    }

    const measure = () => {
      width = stage.clientWidth;
      height = window.innerHeight;
      fit = !reduced.matches && width >= 900 && height >= (width < 1100 ? 760 : 680);
      host.dataset.layout = fit ? "fit" : "flow";
      const scale = Math.min((width * 0.94 - 12) / landWidth, (height * 0.94 - 12) / landHeight);
      stage.style.setProperty("--map-width", `${mapFrame.width * scale}px`);
      const grid = boxWithin(board, stage);
      arrangeHero(dots, {
        x: grid.x - 22,
        y: grid.y - 24,
        width: grid.width + 44,
        height: grid.height + 48,
      });
      const plane = map.getBoundingClientRect(),
        origin = stage.getBoundingClientRect();
      const unit = plane.width / mapFrame.width;
      mapRadius = Math.max(2.4, Math.min(7.8, unit * 3));
      for (const dot of dots) {
        dot.atlas.x = plane.left - origin.left + (dot.x - mapFrame.left) * unit;
        dot.atlas.y = plane.top - origin.top + dot.y * unit;
      }
      cards.forEach((card, i) => {
        const rect = card.getBoundingClientRect();
        const border = getComputedStyle(card);
        const inset = parseFloat(border.borderTopWidth) / 2;
        arrangeContour(
          westToEast.slice(
            Math.round((i * dots.length) / 3),
            Math.round(((i + 1) * dots.length) / 3),
          ),
          {
            x: rect.x - origin.x + inset,
            y: rect.y - origin.y + inset,
            width: rect.width - 2 * inset,
            height: rect.height - 2 * inset,
          },
          Math.max(0, parseFloat(border.borderTopLeftRadius) - inset),
        );
      });
      if (fit && typeof Intl.Segmenter === "function") {
        stage.setAttribute("data-measuring-heading", "");
        moveHeadings = measureHeadings(layer, headings, origin);
        stage.removeAttribute("data-measuring-heading");
      } else {
        moveHeadings = undefined;
        stage.removeAttribute("data-moving-heading");
        layer.replaceChildren();
      }
      dpr = Math.min(window.devicePixelRatio, 2);
      node.style.height = `${height}px`;
      if (node.width !== Math.round(width * dpr)) node.width = Math.round(width * dpr);
      if (node.height !== Math.round(height * dpr)) node.height = Math.round(height * dpr);
      storyTop = story.getBoundingClientRect().top + scrollY;
      flowMap = Math.min(atlas.offsetTop, plane.top - origin.top - height * 0.48);
      flowStart = Math.min(flowMap - height * 0.2, grid.y + grid.height - height * 0.7);
      flowLeave = Math.max(atlas.offsetTop + 1, plane.bottom - origin.top - height * 0.12);
      flowMethod = Math.max(
        flowLeave + height * 0.2,
        cards[0].getBoundingClientRect().top - origin.top - height * 0.48,
      );
      lastProgress = NaN;
      cancelAnimationFrame(frame);
      paint(performance.now());
    };

    const paint = (time: number) => {
      frame = 0;
      if (document.hidden) return;
      if (lastTime) clock += Math.min(80, time - lastTime);
      lastTime = time;
      const offset = window.scrollY - storyTop;
      const p = offset / height;
      const first = fit ? ramp(p, ...desktopStops.first) : ramp(offset, flowStart, flowMap);
      const second = fit ? ramp(p, ...desktopStops.second) : ramp(offset, flowLeave, flowMethod);
      const top = fit ? 0 : Math.max(0, offset);
      if (p !== lastProgress) {
        moveHeadings?.(first, second);
        const heroOut = ramp(first, 0.08, 0.72);
        const atlasIn = ramp(first, 0.5, 1) * (1 - ramp(second, 0, 0.22));
        const methodIn = ramp(second, 0.52, 0.97);
        stage.style.setProperty("--hero-out", String(heroOut));
        stage.style.setProperty("--atlas-in", String(atlasIn));
        stage.style.setProperty("--method-in", String(methodIn));
        opening.toggleAttribute("inert", fit && heroOut > 0.55);
        atlas.toggleAttribute("inert", fit && atlasIn < 0.55);
        method.toggleAttribute("inert", fit && methodIn < 0.55);
        stage.dataset.progress = p.toFixed(3);
        stage.dataset.phase =
          second === 1
            ? "METHOD_HOLD"
            : second > 0
              ? "TO_METHOD"
              : first === 1
                ? "ATLAS_HOLD"
                : first > 0
                  ? "MORPH"
                  : "HERO_HOLD";
        node.dataset.morph = first.toFixed(3);
        node.dataset.stage = first >= 0.5 ? "1" : "0";
        node.style.transform = fit ? "" : `translateY(${top}px)`;
        lastProgress = p;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, -top * dpr);
      ctx.clearRect(0, top, width, height);
      if (reduced.matches) {
        for (const [a, b] of [
          [0, 0],
          [1, 0],
          [1, 1],
        ])
          draw(a, b, 0, highlight, mapRadius, top, top + height);
      } else {
        draw(first, second, clock, highlight, mapRadius, top, top + height);
        request();
      }
    };
    function request() {
      if (!frame && !document.hidden) frame = requestAnimationFrame(paint);
    }
    function scheduleMeasure() {
      if (!measureFrame)
        measureFrame = requestAnimationFrame(() => {
          measureFrame = 0;
          measure();
        });
    }
    function visibility() {
      cancelAnimationFrame(frame);
      frame = lastTime = 0;
      request();
    }
    // ResizeObserver already runs before paint. Keep new panels and their outlines in the same frame.
    const observer = new ResizeObserver(measure);
    for (const el of [stage, opening, board, atlas, map, method, ...cards, ...headings])
      observer.observe(el);
    const changes = new MutationObserver(selection);
    changes.observe(atlas, { attributes: true, attributeFilter: ["data-selected", "data-places"] });
    selection();
    measure();
    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", scheduleMeasure);
    reduced.addEventListener("change", scheduleMeasure);
    document.addEventListener("visibilitychange", visibility);
    document.fonts.addEventListener("loadingdone", scheduleMeasure);
    return () => {
      cancelAnimationFrame(frame);
      cancelAnimationFrame(measureFrame);
      layer.replaceChildren();
      stage.removeAttribute("data-moving-heading");
      observer.disconnect();
      changes.disconnect();
      window.removeEventListener("scroll", request);
      window.removeEventListener("resize", scheduleMeasure);
      reduced.removeEventListener("change", scheduleMeasure);
      document.removeEventListener("visibilitychange", visibility);
      document.fonts.removeEventListener("loadingdone", scheduleMeasure);
    };
  }, [active]);

  return (
    <>
      <canvas className="point-field" ref={canvasRef} inert />
      <div className="heading-motion" aria-hidden="true" />
    </>
  );
}
