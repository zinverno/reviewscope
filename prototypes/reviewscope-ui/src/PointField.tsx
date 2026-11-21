import { useEffect, useLayoutEffect, useRef } from "react";
import { mapFrame } from "./network";
import { columns, landDots, mix, paintField, spacing, type Dot } from "./pointTexture";
import { morphFor, phaseFor, progressFor, sceneTransitionFor } from "./story";

type Layout = "fit" | "flow";

type Metrics = {
  host: HTMLElement;
  stage: HTMLElement;
  story: HTMLElement;
  opening: HTMLElement;
  atlas: HTMLElement;
  storyTop: number;
  storyHeight: number;
  V: number;
  layout: Layout;
  heroX: number;
  heroY: number;
  mapX: number;
  mapY: number;
  scale: number;
  mapHeight: number;
  gridHeight: number;
  boardWidth: number;
  flowRange: { from: number; to: number };
};

function offsetWithin(element: HTMLElement, root: HTMLElement) {
  let x = 0;
  let y = 0;
  let node: HTMLElement | null = element;
  while (node && node !== root) {
    x += node.offsetLeft;
    y += node.offsetTop;
    node = node.offsetParent as HTMLElement | null;
  }
  return { x, y };
}

export default function PointField({ active }: { active: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const activeRef = useRef(active);
  const requestRef = useRef<() => void>(() => {});

  useLayoutEffect(() => {
    const currentCanvas = canvasRef.current;
    const currentContext = currentCanvas?.getContext("2d", { alpha: true });
    if (!currentCanvas || !currentContext) return;
    const node = currentCanvas;
    const context = currentContext;

    const dots: Dot[] = landDots();
    const rows = Math.ceil(dots.length / columns);
    const gridHeight = rows * spacing;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let metrics: Metrics | null = null;
    let image: ImageData | null = null;
    let frame = 0;
    let measureFrame = 0;
    let lastTime = 0;
    let clock = 0;

    function measure() {
      const host = document.querySelector<HTMLElement>(".landing");
      const stage = document.querySelector<HTMLElement>(".story-stage");
      const story = document.getElementById("story") as HTMLElement | null;
      const opening = document.querySelector<HTMLElement>(".opening");
      const atlas = document.querySelector<HTMLElement>(".atlas");
      const board = document.querySelector<HTMLElement>(".board-canvas");
      const heading = document.querySelector<HTMLElement>(".atlas-heading");
      const brief = document.querySelector<HTMLElement>(".atlas-brief");
      const map = document.querySelector<HTMLElement>(".atlas-plane");
      if (!host || !stage || !story || !opening || !atlas || !board || !heading || !brief || !map) return false;

      const stageWidth = stage.clientWidth;
      const stageHeight = stage.clientHeight;
      const chrome = heading.offsetHeight + brief.offsetHeight + 112;
      const freeHeight = Math.max(220, stageHeight - chrome);
      const fittedWidth = Math.min(stageWidth * 0.94, freeHeight * (mapFrame.width / mapFrame.height));
      const fit = !reduced.matches && stageWidth >= 900 && stageHeight >= 680 && fittedWidth >= 560;
      const layout: Layout = fit ? "fit" : "flow";

      if (host.dataset.layout !== layout) host.dataset.layout = layout;
      stage.style.setProperty("--map-width", fit ? `${Math.round(fittedWidth)}px` : "100%");

      const boardBox = offsetWithin(board, stage);
      const mapBox = offsetWithin(map, stage);
      const atlasBox = offsetWithin(atlas, stage);
      const scale = (map.clientWidth || fittedWidth) / mapFrame.width;
      const mapHeight = map.clientHeight || mapFrame.height * scale;
      const heroX = boardBox.x + board.clientWidth / 2;
      const heroY = boardBox.y + (board.clientHeight - gridHeight) / 2;

      for (const dot of dots) {
        dot.hx = heroX + (dot.col - (columns - 1) / 2) * spacing;
        dot.hy = heroY + (dot.row - (rows - 1) / 2) * spacing;
      }

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const cssHeight = Math.max(gridHeight, mapHeight) + 8;
      const pixelWidth = Math.max(1, Math.round(stageWidth * dpr));
      const pixelHeight = Math.max(1, Math.round((fit ? stageHeight : cssHeight) * dpr));
      if (node.width !== pixelWidth || node.height !== pixelHeight) {
        node.width = pixelWidth;
        node.height = pixelHeight;
        image = context.createImageData(pixelWidth, pixelHeight);
      }
      node.style.height = `${fit ? stageHeight : cssHeight}px`;

      const from = Math.max(0, (atlasBox.y - stageHeight) / stageHeight);
      const to = Math.max(from + 0.01, atlasBox.y / stageHeight);
      metrics = {
        host,
        stage,
        story,
        opening,
        atlas,
        storyTop: story.getBoundingClientRect().top + window.scrollY,
        storyHeight: story.offsetHeight,
        V: stageHeight,
        layout,
        heroX,
        heroY,
        mapX: mapBox.x - mapFrame.left * scale,
        mapY: mapBox.y,
        scale,
        mapHeight,
        gridHeight,
        boardWidth: board.clientWidth,
        flowRange: { from, to },
      };
      return true;
    }

    function paint(time: number) {
      frame = 0;
      if (!metrics && !measure()) return;
      if (!metrics || !image) return;
      const m = metrics;

      const s = progressFor(window.scrollY, m.storyTop, m.V);
      const phase = phaseFor(s);
      const morph = morphFor(s, m.layout, reduced.matches, m.flowRange);
      const { heroOut, atlasIn } = sceneTransitionFor(s, m.layout, morph);

      m.stage.style.setProperty("--morph", morph.toFixed(3));
      m.stage.style.setProperty("--hero-out", heroOut.toFixed(3));
      m.stage.style.setProperty("--atlas-in", atlasIn.toFixed(3));
      m.stage.dataset.phase = phase;
      m.stage.dataset.progress = s.toFixed(3);
      node.dataset.stage = morph >= 0.5 ? "1" : "0";
      node.dataset.morph = morph.toFixed(3);
      if (m.layout === "fit") {
        m.opening.toggleAttribute("inert", heroOut > 0.55);
        m.atlas.toggleAttribute("inert", atlasIn < 0.55);
      } else {
        m.opening.removeAttribute("inert");
        m.atlas.removeAttribute("inert");
      }

      if (!reduced.matches && lastTime && time > lastTime) clock += Math.min(time - lastTime, 80);
      lastTime = time;

      const highlight = {
        selected: m.atlas.dataset.selected ?? "",
        visible: (m.atlas.dataset.places ?? "").split(",").filter(Boolean),
      };
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const originY = mix(m.heroY, m.mapY, morph);
      paintField(image, dots, morph, m.mapX, m.scale, dpr, reduced.matches ? 0 : clock, m.boardWidth, highlight);
      context.putImageData(image, 0, 0);
      node.style.transform = `translate3d(0, ${originY.toFixed(2)}px, 0)`;
      if (!reduced.matches) request();
    }

    function request() {
      if (!frame && activeRef.current && !document.hidden) frame = window.requestAnimationFrame(paint);
    }

    function requestMeasure() {
      if (measureFrame) return;
      measureFrame = window.requestAnimationFrame(() => {
        measureFrame = 0;
        measure();
        request();
      });
    }

    requestRef.current = request;
    const onMotionChange = () => requestMeasure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(requestMeasure);
    const observed = [
      ".story-stage",
      ".board-canvas",
      ".atlas-heading",
      ".atlas-brief",
      ".atlas-plane",
    ];
    for (const selector of observed) {
      const element = document.querySelector(selector);
      if (element) observer?.observe(element);
    }

    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", requestMeasure);
    window.addEventListener("orientationchange", requestMeasure);
    window.addEventListener("story-measure", requestMeasure);
    reduced.addEventListener?.("change", onMotionChange);
    window.addEventListener("map-selection", request);
    document.addEventListener("visibilitychange", request);
    document.fonts?.ready.then(requestMeasure);
    measure();
    request();

    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      if (measureFrame) window.cancelAnimationFrame(measureFrame);
      observer?.disconnect();
      window.removeEventListener("scroll", request);
      window.removeEventListener("resize", requestMeasure);
      window.removeEventListener("orientationchange", requestMeasure);
      window.removeEventListener("story-measure", requestMeasure);
      reduced.removeEventListener?.("change", onMotionChange);
      window.removeEventListener("map-selection", request);
      document.removeEventListener("visibilitychange", request);
      requestRef.current = () => {};
    };
  }, []);

  useEffect(() => {
    activeRef.current = active;
    if (active) requestRef.current();
  }, [active]);

  return <canvas className="point-field" ref={canvasRef} inert />;
}
