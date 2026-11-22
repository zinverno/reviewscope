import { useLayoutEffect, useRef } from "react";

const homes = [
  [0.12, 0.07],
  [0.97, 0.4],
  [0.04, 0.74],
];
const clamp = (n: number, max: number) => Math.min(Math.max(0, n), Math.max(0, max));
type Drag = {
  link: HTMLAnchorElement;
  original: number[];
  pointer: number;
  index: number;
  x: number;
  y: number;
  startX: number;
  startY: number;
  moved: boolean;
};
export function useCardBoard() {
  const board = useRef<HTMLDivElement>(null);
  const move = useRef<(index: number, dx: number, dy: number) => void>(() => {});
  const reset = useRef<() => void>(() => {});
  const offsets = useRef(homes.map(() => [0, 0]));
  useLayoutEffect(() => {
    const host = board.current;
    if (!host) return;
    const cards = Array.from(host.querySelectorAll<HTMLElement>("article"));
    const paths = Array.from(host.querySelectorAll<SVGPathElement>(".threads path"));
    let drag: Drag | null = null,
      frame = 0;
    let suppressed: HTMLAnchorElement | null = null;
    function draw() {
      if (!host) return;
      const w = host.clientWidth,
        h = host.clientHeight;
      const boxes = cards.map((card, i) => {
        const width = card.offsetWidth,
          height = card.offsetHeight;
        const x = clamp(homes[i][0] * (w - width) + offsets.current[i][0], w - width);
        // Keep a reachable header lane for each review, even if panels overlap.
        const minY = i === 0 ? 0 : (i === 1 ? 0.29 : 0.6) * h;
        const maxY = Math.min(h - height, (i === 0 ? 0.24 : i === 1 ? 0.55 : 1) * h);
        const y = clamp(
          Math.max(minY, Math.min(maxY, homes[i][1] * h + offsets.current[i][1])),
          h - height,
        );
        card.style.transform = `translate3d(${x}px,${y}px,0)`;
        return { x: x + width / 2, y: y + height / 2, width, height };
      });
      paths.forEach((path, i) => {
        const a = boxes[i],
          b = boxes[i + 1];
        const dx = b.x - a.x,
          dy = b.y - a.y;
        if (Math.abs(dx) < (a.width + b.width) / 2 && Math.abs(dy) < (a.height + b.height) / 2) {
          // Connect exposed sides when the segment between centres would lie under the cards.
          const left = Math.min(a.x - a.width / 2, b.x - b.width / 2);
          const right = Math.max(a.x + a.width / 2, b.x + b.width / 2);
          const side = left > w - right ? -1 : 1;
          const bend = clamp(side < 0 ? left - 32 : right + 32, w);
          const start = a.x + (side * a.width) / 2,
            end = b.x + (side * b.width) / 2;
          path.setAttribute("d", `M${start} ${a.y} C${bend} ${a.y},${bend} ${b.y},${end} ${b.y}`);
          return;
        }
        const k = (box: typeof a) =>
          Math.min(box.width / (2 * Math.abs(dx) || 1), box.height / (2 * Math.abs(dy) || 1));
        const x = a.x + dx * k(a),
          y = a.y + dy * k(a),
          tx = b.x - dx * k(b),
          ty = b.y - dy * k(b);
        const mid = (y + ty) / 2;
        path.setAttribute("d", `M${x} ${y} C${x} ${mid},${tx} ${mid},${tx} ${ty}`);
      });
    }
    function schedule() {
      if (!frame)
        frame = requestAnimationFrame(() => {
          frame = 0;
          draw();
        });
    }
    function finish(cancelled = false) {
      if (!drag || !host) return;
      const current = drag;
      drag = null;
      if (cancelled) offsets.current[current.index] = current.original;
      if (current.moved || cancelled) suppressed = current.link;
      delete host.dataset.dragging;
      delete host.dataset.pending;
      if (current.link.hasPointerCapture(current.pointer))
        current.link.releasePointerCapture(current.pointer);
      draw();
    }
    function down(e: PointerEvent) {
      if (!host || !e.isPrimary || e.button !== 0 || drag) return;
      suppressed = null;
      if (
        e.metaKey ||
        e.ctrlKey ||
        e.shiftKey ||
        e.altKey ||
        (e.pointerType === "touch" && !host.hasAttribute("data-move"))
      )
        return;
      const link = (e.target as Element).closest<HTMLAnchorElement>(".slip-shell");
      if (!link) return;
      const index = cards.indexOf(link.parentElement as HTMLElement);
      const box = cards[index].getBoundingClientRect(),
        base = host.getBoundingClientRect();
      drag = {
        link,
        index,
        pointer: e.pointerId,
        original: [...offsets.current[index]],
        x: box.left - base.left - homes[index][0] * (base.width - box.width),
        y: box.top - base.top - homes[index][1] * base.height,
        startX: e.clientX,
        startY: e.clientY,
        moved: false,
      };
      host.dataset.pending = String(index);
    }
    function motion(e: PointerEvent) {
      if (!host || !drag || drag.pointer !== e.pointerId) return;
      const dx = e.clientX - drag.startX,
        dy = e.clientY - drag.startY;
      if (!drag.moved) {
        if (Math.hypot(dx, dy) < 8) return;
        drag.moved = true;
        host.dataset.dragging = String(drag.index);
        drag.link.setPointerCapture(e.pointerId);
      }
      offsets.current[drag.index] = [drag.x + dx, drag.y + dy];
      schedule();
    }
    function end(e: PointerEvent) {
      if (
        drag?.pointer === e.pointerId &&
        (e.type !== "lostpointercapture" || e.target === drag.link)
      )
        finish(e.type !== "pointerup");
    }
    function click(e: MouseEvent) {
      if (suppressed && e.detail > 0 && suppressed.contains(e.target as Node)) {
        e.preventDefault();
        e.stopPropagation();
      }
      suppressed = null;
    }
    function key(e: KeyboardEvent) {
      if (e.key === "Escape") finish(true);
    }
    function resize() {
      finish(true);
      schedule();
    }
    move.current = (i, dx, dy) => {
      const box = cards[i].getBoundingClientRect(),
        base = host.getBoundingClientRect();
      offsets.current[i] = [
        box.left - base.left - homes[i][0] * (base.width - box.width) + dx,
        box.top - base.top - homes[i][1] * base.height + dy,
      ];
      draw();
    };
    reset.current = () => {
      finish();
      offsets.current = homes.map(() => [0, 0]);
      draw();
    };
    const observer = new ResizeObserver(draw);
    observer.observe(host);
    cards.forEach((c) => {
      observer.observe(c);
    });
    host.addEventListener("pointerdown", down);
    window.addEventListener("pointermove", motion);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    host.addEventListener("lostpointercapture", end);
    host.addEventListener("click", click, true);
    window.addEventListener("keydown", key);
    window.addEventListener("resize", resize);
    draw();
    return () => {
      finish();
      observer.disconnect();
      cancelAnimationFrame(frame);
      host.removeEventListener("pointerdown", down);
      window.removeEventListener("pointermove", motion);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      host.removeEventListener("lostpointercapture", end);
      host.removeEventListener("click", click, true);
      window.removeEventListener("keydown", key);
      window.removeEventListener("resize", resize);
    };
  }, []);
  return { board, move, reset };
}
