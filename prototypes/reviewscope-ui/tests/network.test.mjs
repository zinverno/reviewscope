import assert from "node:assert/strict";
import test from "node:test";
import { counts } from "../src/analysis.ts";
import { fraction, own, places, selection } from "../src/network.ts";

test("network share weights all text rows and excludes the external company", () => {
  const result = selection(
    own.map((p) => p.id),
    "sep",
  );
  assert.deepEqual([result.hits, result.total, result.missing], [3, 10, 1]);
  assert.equal(fraction(result), "30%");
  assert.equal(selection(["ray"], "sep").total, 4);
  assert.equal(
    result.rows.some((r) => r.place === "ray"),
    false,
  );
  assert.equal(new Set(result.rows.map((r) => r.id)).size, result.rows.length);
  for (const p of places)
    for (const period of ["sep", "oct"]) {
      const { rows } = selection([p.id], period);
      assert.ok(rows.length >= 3 && rows.length <= 6);
      assert.ok(rows.every((r) => r.id.startsWith(p.id + "-" + period)));
    }
  assert.equal(counts("sep").percent, 50);
  assert.equal(counts("oct").percent, 20);
});
test("empty and ambiguous evidence retain distinct meanings", () => {
  const empty = selection(["cape"], "oct");
  assert.equal(empty.total, 0);
  assert.equal(empty.missing, 3);
  assert.equal(fraction(empty), "Нет данных");
  const mixed = selection(["arc"], "oct");
  assert.equal(mixed.total, 4);
  assert.equal(mixed.hits, 1);
  assert.equal(mixed.rows.filter((r) => r.stance === "unclear").length, 1);
});
