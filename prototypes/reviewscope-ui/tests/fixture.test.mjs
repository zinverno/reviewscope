import assert from "node:assert/strict";
import test from "node:test";
import { counts, direct, inPeriod, isIssue } from "../src/analysis.ts";
import { edges, reviews } from "../src/fixture.ts";

test("edges have explicit distinct endpoints in one period; paths do not create other edges", () => {
  const byId = new Map(reviews.map((r) => [r.review_id, r]));
  assert.equal(byId.size, reviews.length);
  const pairs = new Set();
  for (const e of edges) {
    const a = byId.get(e.a),
      b = byId.get(e.b);
    assert.ok(a && b && a !== b && a.period === b.period);
    assert.ok(a.text.includes(a.phrase) && b.text.includes(b.phrase));
    assert.equal(direct(e.b, e.a), e);
    pairs.add([e.a, e.b].sort().join("/"));
  }
  assert.equal(pairs.size, edges.length);
  assert.ok(direct("01", "02") && direct("02", "03"));
  assert.equal(direct("01", "03"), undefined);
  assert.equal(direct("02", "05").type, "Повтор текста");
  assert.equal(byId.get("02").text, byId.get("05").text);
});
test("counts use distinct review records with text; exclude other aspects and denials from numerator", () => {
  assert.deepEqual(
    inPeriod("sep")
      .filter(isIssue)
      .map((r) => r.review_id),
    ["01", "02", "05"],
  );
  assert.deepEqual(
    inPeriod("oct")
      .filter(isIssue)
      .map((r) => r.review_id),
    ["07"],
  );
  assert.deepEqual(counts("sep"), { hits: 3, total: 6, missing: 0, percent: 50 });
  assert.deepEqual(counts("oct"), { hits: 1, total: 5, missing: 1, percent: 20 });
  for (const r of reviews) assert.ok(r.text.includes(r.phrase));
});
