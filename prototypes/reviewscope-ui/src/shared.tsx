import type { ReactNode } from "react";
import { dateLabel } from "./analysis";
import { labels, periods } from "./fixture";
import type { Period, Review, Stance } from "./fixture";

const paths = {
  arrow: "M4 12h15m-6-6 6 6-6 6",
  external: "M6 18 18 6M7 6h11v11",
  back: "m13 5-7 7 7 7",
  link: "m9 15 6-6m-5-2 2-2a4 4 0 0 1 6 6l-2 2m-2 4-2 2a4 4 0 0 1-6-6l2-2",
  search: "M16 16l5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  graph:
    "M5 17 11 6l8 9M5 17l14-2M5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4m6-11a2 2 0 1 0 0-4 2 2 0 0 0 0 4m8 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4",
  list: "M8 6h12M8 12h12M8 18h12M4 6h.1M4 12h.1M4 18h.1",
  file: "M5 3h9l5 5v13H5zM14 3v6h5M8 13h8m-8 4h6",
  upload: "M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5",
  globe: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3c-6 5-6 13 0 18 6-5 6-13 0-18",
  menu: "M3 6h18M3 12h18M3 18h18",
  close: "m5 5 14 14M19 5 5 19",
} as const;
export function Icon({ name, size = 18 }: { name: keyof typeof paths; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
export function Brand() {
  return (
    <a href="#home" className="brand" aria-label="ReviewScope — на главную">
      <img src="./scope.svg" width="32" height="32" alt="" />
      ReviewScope
    </a>
  );
}
export function Highlight({ review }: { review: Review }) {
  const i = review.phrase ? review.text.indexOf(review.phrase) : -1;
  return i < 0 ? (
    review.text || "В источнике есть только оценка."
  ) : (
    <>
      {review.text.slice(0, i)}
      <mark>{review.phrase}</mark>
      {review.text.slice(i + review.phrase.length)}
    </>
  );
}
export function Badge({ stance }: { stance: Stance }) {
  return <span className={`badge ${stance}`}>{labels[stance]}</span>;
}
export function ReviewMeta({ review }: { review: Review }) {
  return (
    <span className="review-meta">
      <b>#{review.review_id}</b>
      <span>·</span>
      <time dateTime={review.date}>{dateLabel(review.date)}</time>
      <span className="stars">
        {review.rating === null ? "Без оценки" : `★ ${review.rating} / 5`}
      </span>
    </span>
  );
}
export function PeriodPicker({
  period,
  onChange,
}: {
  period: Period;
  onChange: (p: Period) => void;
}) {
  return (
    <fieldset className="seg periods" aria-label="Демонстрационный период">
      {(["sep", "oct"] as const).map((p) => (
        <button type="button" key={p} aria-pressed={p === period} onClick={() => onChange(p)}>
          {periods[p]}
        </button>
      ))}
    </fieldset>
  );
}
export function SectionTitle({ children, detail }: { children: ReactNode; detail?: string }) {
  return (
    <div className="section-title">
      <h2>{children}</h2>
      {detail && <span className="mono">{detail}</span>}
    </div>
  );
}
