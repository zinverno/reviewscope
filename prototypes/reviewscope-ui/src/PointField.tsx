import { useEffect, useLayoutEffect, useRef } from "react";
import { mapFrame } from "./network";
import { clamp, columns, landDots, mix, paintField, smooth, spacing } from "./pointTexture";

export default function PointField({ active }: { active: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const requestRef = useRef<() => void>(() => {});
  const activeRef = useRef(active);
  activeRef.current = active;

  useLayoutEffect(() => {
    const found = canvas.current;
    if (!found) return;
    const node = found,
      maybeContext = node.getContext("2d", { alpha: true });
    if (!maybeContext) return;
    const context = maybeContext;
    const dots = landDots(),
      media = matchMedia("(prefers-reduced-motion: reduce)"),
      rows = Math.ceil(dots.length / columns),
      gridHeight = rows * spacing;
    let frame = 0,
      timer: number | undefined,
      lastTime = 0,
      clock = 0,
      origin = 0;

    function measure() {
      const host = node.closest<HTMLElement>(".landing"),
        cards = document.querySelector<HTMLElement>(".board-canvas"),
        map = document.querySelector<HTMLElement>(".atlas-plane");
      if (!host || !cards || !map) return;
      const hostBox = host.getBoundingClientRect(),
        cardBox = cards.getBoundingClientRect(),
        mapBox = map.getBoundingClientRect(),
        dpr = Math.min(devicePixelRatio || 1, 2);
      node.width = Math.round(host.clientWidth * dpr);
      node.height = Math.round((Math.max(gridHeight, mapBox.height) + 8) * dpr);
      node.style.height = `${Math.max(gridHeight, mapBox.height) + 8}px`;
      const heroY = cardBox.top - hostBox.top + (cardBox.height - gridHeight) / 2,
        mapY = mapBox.top - hostBox.top,
        mapX = mapBox.left - hostBox.left - mapFrame.left * (mapBox.width / mapFrame.width),
        scale = mapBox.width / mapFrame.width,
        middle = cardBox.left - hostBox.left + cardBox.width / 2;
      for (const dot of dots) {
        dot.hx = middle + (dot.col - (columns - 1) / 2) * spacing;
        dot.hy = gridHeight / 2 + (dot.row - (rows - 1) / 2) * spacing;
      }
      return { host, heroY, mapY, mapX, scale, dpr, boardWidth: cardBox.width };
    }

    function geometry() {
      const features = document.getElementById("features");
      if (!features) return null;
      const end = features.getBoundingClientRect().top + scrollY,
        start = Math.max(0, end - innerHeight),
        p = media.matches
          ? scrollY >= (start + end) / 2
            ? 1
            : 0
          : clamp((scrollY - start) / (end - start || 1)),
        morph = smooth(p),
        hold = Math.sin(Math.PI * morph) * Math.min(innerHeight * 0.16, 130),
        host = document.querySelector<HTMLElement>(".landing");
      if (!host) return null;
      host.style.setProperty("--morph", morph.toFixed(3));
      const state = measure();
      if (!state) return null;
      origin = mix(state.heroY, state.mapY, morph) - hold;
      features.toggleAttribute("inert", morph < 0.985);
      return { ...state, morph, p };
    }

    function paint(time: number) {
      const state = geometry(),
        features = document.getElementById("features");
      if (!state || !features) return;
      if (!media.matches && lastTime && time > lastTime) clock += Math.min(time - lastTime, 80);
      lastTime = time;
      const viewportTop = -state.host.getBoundingClientRect().top,
        visibleTop = Math.max(origin, viewportTop),
        visibleBottom = Math.min(origin + node.height / state.dpr, viewportTop + innerHeight),
        visible = visibleBottom - visibleTop > innerHeight * 0.06;
      const image = context.createImageData(node.width, node.height);
      paintField(
        image,
        dots,
        state.morph,
        state.mapX,
        state.scale,
        state.dpr,
        clock,
        state.boardWidth,
        {
          selected: features.dataset.selected ?? "",
          visible: (features.dataset.places ?? "").split(",").filter(Boolean),
        },
      );
      context.putImageData(image, 0, 0);
      node.style.transform = `translate3d(0,${origin.toFixed(2)}px,0)`;
      node.dataset.stage = state.p.toFixed(3);
      if (visible && activeRef.current && document.visibilityState === "visible" && !media.matches)
        schedule();
    }

    function draw() {
      if (frame) return;
      frame = requestAnimationFrame((time) => {
        frame = 0;
        paint(time);
      });
    }
    function schedule() {
      window.clearTimeout(timer);
      timer = window.setTimeout(draw, 75);
    }
    function request() {
      lastTime = 0;
      draw();
    }
    requestRef.current = request;

    const observer = new ResizeObserver(request);
    observer.observe(node);
    addEventListener("scroll", draw, { passive: true });
    addEventListener("resize", request);
    media.addEventListener("change", request);
    document.addEventListener("visibilitychange", request);
    request();
    return () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(timer);
      observer.disconnect();
      removeEventListener("scroll", draw);
      removeEventListener("resize", request);
      media.removeEventListener("change", request);
      document.removeEventListener("visibilitychange", request);
    };
  }, []);

  useEffect(() => {
    const redraw = () => requestRef.current();
    addEventListener("map-selection", redraw);
    return () => removeEventListener("map-selection", redraw);
  }, []);

  return <canvas className="point-field" ref={canvas} inert />;
}
