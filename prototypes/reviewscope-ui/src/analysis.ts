import { edges, reviews } from "./fixture.ts";
import type { Period, Review } from "./fixture.ts";

export const inPeriod = (period: Period) => reviews.filter((r) => r.period === period);
export const isIssue = (r: Review) =>
  Boolean(r.text.trim()) && r.aspect === "Выдача" && r.stance === "complaint";
export function counts(period: Period) {
  const rows = inPeriod(period);
  const total = rows.filter((r) => r.text.trim()).length;
  const hits = rows.filter(isIssue).length;
  return {
    hits,
    total,
    missing: rows.length - total,
    percent: total ? (100 * hits) / total : null,
  };
}
export function direct(a: string, b: string) {
  return edges.find((e) => (e.a === a && e.b === b) || (e.a === b && e.b === a));
}
export function dateLabel(date: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).format(new Date(date));
}
