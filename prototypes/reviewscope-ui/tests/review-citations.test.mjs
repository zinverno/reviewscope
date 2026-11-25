import assert from "node:assert/strict";
import test from "node:test";
import { citationBounds, quote } from "../src/reviewCitations.ts";

test("original code-point intervals preserve emoji, combining marks, CRLF and the selected occurrence", () => {
	const original = Array.from("🍒e\u0301\r\ne\u0301🍒");
	const { valid, spans } = citationBounds(original, [
		["0", "3"],
		["5", "7"],
	]);
	assert(valid);
	assert.deepEqual(spans, [
		{ start: 0, end: 3 },
		{ start: 5, end: 7 },
	]);
	assert.equal(quote(original, spans[0]), "🍒e\u0301");
	assert.equal(quote(original, spans[1]), "e\u0301");
	assert.equal(quote(original, { start: 2, end: 6 }), "\u0301\r\ne");
	for (const form of [
		[
			["0", "2"],
			["1", "4"],
		],
		[["0", "9"]],
		[["", "1"]],
		[["3", "5"]],
		[["-1", "2"]],
	])
		assert.equal(citationBounds(original, form).valid, false);
	assert.deepEqual(
		citationBounds(original, [
			["", ""],
			["", ""],
		]),
		{ valid: true, spans: [] },
	);
});
