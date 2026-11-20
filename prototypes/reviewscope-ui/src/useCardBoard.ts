import { useLayoutEffect, useRef } from "react";

const homes = [
  [0.12, 0.07],
  [0.97, 0.4],
  [0.04, 0.74],
];
const clamp = (n: number, max: number) => Math.min(Math.max(0, n), Math.max(0, max));
type Drag = {
  handle: HTMLButtonElement;
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
    let suppressed: HTMLButtonElement | null = null;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)");
    const lift = (card: HTMLElement) => (card.dataset.open === "true" && !reduced.matches ? 5 : 0);
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
          Math.max(minY, Math.min(maxY, homes[i][1] * h + offsets.current[i][1] - lift(card))),
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
    function finish() {
      if (!drag || !host) return;
      const { handle, pointer } = drag;
      drag = null;
      delete host.dataset.dragging;
      if (handle.hasPointerCapture(pointer)) handle.releasePointerCapture(pointer);
    }
    function down(e: PointerEvent) {
      const handle = (e.target as Element).closest<HTMLButtonElement>(".grip");
      if (!host || !handle || !e.isPrimary || e.button !== 0 || drag) return;
      suppressed = null;
      const index = cards.indexOf(handle.closest("article") as HTMLElement);
      // Rebase on the clamped position, including any prior viewport resize.
      const r = cards[index].getBoundingClientRect(),
        b = host.getBoundingClientRect();
      const x = r.left - b.left - homes[index][0] * (b.width - r.width),
        y = r.top - b.top - homes[index][1] * b.height + lift(cards[index]);
      drag = {
        handle,
        pointer: e.pointerId,
        index,
        x,
        y,
        startX: e.clientX,
        startY: e.clientY,
        moved: false,
      };
      host.dataset.dragging = String(index);
      handle.setPointerCapture(e.pointerId);
    }
    function motion(e: PointerEvent) {
      if (!drag || drag.pointer !== e.pointerId) return;
      const dx = e.clientX - drag.startX,
        dy = e.clientY - drag.startY;
      if (!drag.moved && Math.hypot(dx, dy) < 6) return;
      drag.moved = true;
      offsets.current[drag.index] = [drag.x + dx, drag.y + dy];
      schedule();
    }
    function end(e: PointerEvent) {
      if (drag?.pointer !== e.pointerId) return;
      if (drag.moved && e.type === "pointerup") suppressed = drag.handle;
      finish();
    }
    function click(e: MouseEvent) {
      if (suppressed && e.detail > 0 && suppressed.contains(e.target as Node)) {
        e.preventDefault();
        e.stopPropagation();
      }
      suppressed = null;
    }
    function key(e: KeyboardEvent) {
      if (e.key === "Escape") finish();
    }
    function resize() {
      finish();
      schedule();
    }
    move.current = (i, dx, dy) => {
      const box = cards[i].getBoundingClientRect(),
        base = host.getBoundingClientRect();
      offsets.current[i] = [
        box.left - base.left - homes[i][0] * (base.width - box.width) + dx,
        box.top - base.top - homes[i][1] * base.height + lift(cards[i]) + dy,
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
    host.addEventListener("pointermove", motion);
    host.addEventListener("pointerup", end);
    host.addEventListener("pointercancel", end);
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
      host.removeEventListener("pointermove", motion);
      host.removeEventListener("pointerup", end);
      host.removeEventListener("pointercancel", end);
      host.removeEventListener("lostpointercapture", end);
      host.removeEventListener("click", click, true);
      window.removeEventListener("keydown", key);
      window.removeEventListener("resize", resize);
    };
  }, []);
  return { board, move, reset };
}
