export type Span = { start: number; end: number };

export function quote(chars: string[], { start, end }: Span) {
	return chars.slice(start, end).join("");
}

export function citationBounds(chars: string[], bounds: string[][]) {
	const spans = bounds
		.filter((pair) => pair.some(Boolean))
		.map(([start, end]) => ({
			start: start === "" ? NaN : Number(start),
			end: end === "" ? NaN : Number(end),
		}));
	const valid =
		spans.length <= 2 &&
		spans.every(
			(s) =>
				Number.isInteger(s.start) &&
				Number.isInteger(s.end) &&
				s.start >= 0 &&
				s.end <= chars.length &&
				s.start < s.end &&
				quote(chars, s).trim().length > 0,
		) &&
		(spans.length !== 2 ||
			spans[0].end <= spans[1].start ||
			spans[1].end <= spans[0].start);
	return { spans, valid };
}
