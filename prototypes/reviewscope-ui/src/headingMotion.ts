import { clamp, mix, smooth } from "./pointTexture";

type Glyph = {
  char: string;
  accent: boolean;
  x: number;
  y: number;
  size: number;
  width: number;
  face: HTMLSpanElement;
  insetY: number;
};
type Pair = { from?: Glyph; to?: Glyph; start: Glyph; end: Glyph };

function glyphsOf(heading: HTMLElement, origin: DOMRect, fragment: DocumentFragment) {
  const letters: Glyph[] = [];
  const walker = document.createTreeWalker(heading, NodeFilter.SHOW_TEXT);
  const segmenter = new Intl.Segmenter("ru", { granularity: "grapheme" });
  const range = document.createRange();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent) continue;
    const style = getComputedStyle(parent);
    for (const { segment: char, index } of segmenter.segment(node.textContent ?? "")) {
      if (!char.trim()) continue;
      range.setStart(node, index);
      range.setEnd(node, index + char.length);
      const rect = range.getBoundingClientRect();
      const face = document.createElement("span");
      face.textContent = char;
      Object.assign(face.style, {
        fontFamily: style.fontFamily,
        fontSize: style.fontSize,
        fontStyle: style.fontStyle,
        fontWeight: style.fontWeight,
        lineHeight: style.lineHeight,
        letterSpacing: style.letterSpacing,
        color: style.color,
        textShadow: style.textShadow,
      });
      fragment.append(face);
      letters.push({
        char,
        accent: Boolean(parent.closest("em")),
        face,
        size: parseFloat(style.fontSize),
        width: rect.width,
        x: rect.left - origin.left,
        y: rect.top - origin.top,
        insetY: 0,
      });
    }
  }
  return letters;
}

function pairGlyphs(source: Glyph[], target: Glyph[]) {
  const choices = source.flatMap((from, i) =>
    target.map((to, j) => ({
      i,
      j,
      exact: from.char === to.char,
      role: from.accent === to.accent,
      distance: Math.abs(i / source.length - j / target.length),
    })),
  );
  choices.sort(
    (a, b) =>
      Number(b.exact) - Number(a.exact) ||
      Number(b.role) - Number(a.role) ||
      a.distance - b.distance,
  );
  const used = new Set<number>();
  const matches = new Map<number, number>();
  for (const { i, j } of choices) {
    if (matches.has(i) || used.has(j)) continue;
    matches.set(i, j);
    used.add(j);
  }
  // Unpaired letters share the corridor of their nearest reading-order neighbour.
  const pairs: Pair[] = source.map((from, i) => {
    const j = matches.get(i);
    const to = j === undefined ? undefined : target[j];
    return {
      from,
      to,
      start: from,
      end: to ?? target[Math.floor((i * target.length) / source.length)],
    };
  });
  target.forEach((to, j) => {
    if (!used.has(j))
      pairs.push({ to, start: source[Math.floor((j * source.length) / target.length)], end: to });
  });
  // Open a little space at crossings before caching the curves. Both endpoints stay exact.
  const midpoints = pairs.map(({ start, end }) => ({
    x: (start.x + end.x) / 2,
    y: (start.y + end.y) / 2,
    width: (start.width + end.width) / 2 + 3,
    height: (start.size + end.size) * 0.48,
  }));
  for (let pass = 0; pass < 28; pass++) {
    for (let i = 0; i < pairs.length; i++)
      for (let j = i + 1; j < pairs.length; j++) {
        const a = midpoints[i],
          b = midpoints[j];
        const dx = b.x + b.width / 2 - a.x - a.width / 2,
          dy = b.y + b.height / 2 - a.y - a.height / 2;
        const overlapX = (a.width + b.width) / 2 - Math.abs(dx);
        const overlapY = (a.height + b.height) / 2 - Math.abs(dy);
        if (overlapX <= 0 || overlapY <= 0) continue;
        const axis = overlapX < overlapY ? "x" : "y";
        const delta =
          axis === "x" ? overlapX * (dx < 0 ? -0.5 : 0.5) : overlapY * (dy < 0 ? -0.5 : 0.5);
        a[axis] -= delta;
        b[axis] += delta;
      }
  }
  return pairs.map((pair, i) => ({
    ...pair,
    bendX: midpoints[i].x - (pair.start.x + pair.end.x) / 2,
    bendY: midpoints[i].y - (pair.start.y + pair.end.y) / 2,
  }));
}

export function measureHeadings(layer: HTMLElement, headings: HTMLElement[], origin: DOMRect) {
  const fragment = document.createDocumentFragment();
  const groups = headings.map((heading) => glyphsOf(heading, origin, fragment));
  layer.replaceChildren(fragment);
  const range = document.createRange();
  for (const glyph of groups.flat()) {
    // Align the font's native text box, including the italic face's own ascent.
    range.selectNodeContents(glyph.face);
    glyph.insetY = range.getBoundingClientRect().top - origin.top;
  }
  const transitions = [pairGlyphs(groups[0], groups[1]), pairGlyphs(groups[1], groups[2])];
  let active = -1;
  return (first: number, second: number) => {
    const index = first > 0 && first < 1 ? 0 : second > 0 && second < 1 ? 1 : -1;
    if (index !== active) {
      for (const group of groups) for (const glyph of group) glyph.face.style.opacity = "0";
      active = index;
    }
    layer.parentElement?.toggleAttribute("data-moving-heading", index !== -1);
    if (index === -1) return;

    const t = index === 0 ? first : second;
    for (const [i, pair] of transitions[index].entries()) {
      const { from, to, start, end } = pair;
      const position = t + (i / transitions[index].length - 0.5) * 0.25 * t * (1 - t);
      const curve = 4 * position * (1 - position);
      const x = mix(start.x, end.x, position) + curve * pair.bendX;
      const y = mix(start.y, end.y, position) + curve * pair.bendY;
      const size = mix(start.size, end.size, position);
      const change = smooth(clamp((position - 0.31 - (0.25 * (i % 7)) / 6) / 0.16));
      function place(glyph: Glyph, opacity: number) {
        glyph.face.style.opacity = String(opacity);
        if (!opacity) return;
        const scale = size / glyph.size;
        glyph.face.style.transform = `translate(${x}px, ${y - glyph.insetY * scale}px) scale(${scale})`;
      }
      if (from) place(from, 1 - change);
      if (to) place(to, change);
    }
  };
}
