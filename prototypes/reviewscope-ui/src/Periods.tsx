import { counts, inPeriod, isIssue } from "./analysis";
import { issue, periods } from "./fixture";
import type { Period } from "./fixture";
import { Highlight, Icon, ReviewMeta, SectionTitle } from "./shared";

export function Periods({ period, open }: { period: Period; open: (id: string) => void }) {
  const result = counts(period);
  const delta = (counts("oct").percent ?? 0) - (counts("sep").percent ?? 0);
  const rows = inPeriod(period);
  return (
    <div className="change-grid">
      <section className="panel change-summary">
        <div className="eyebrow">Доля жалоб · {periods[period]}</div>
        <h2>{issue}</h2>
        <p className="big-number">
          {result.percent} <small>%</small>
        </p>
        <p>
          <b>
            {result.hits} из {result.total}
          </b>{" "}
          отзывов с текстом содержат жалобу на выдачу.
        </p>
        <div className="data-bar" aria-hidden="true">
          {rows
            .filter((r) => r.text)
            .map((r) => (
              <i className={isIssue(r) ? "hit" : ""} key={r.review_id} />
            ))}
        </div>
        <p className="note">
          Одна точка, 2026 год. Каждый отзыв учитывается один раз. Отрицания, приготовление и
          неясные случаи остаются в знаменателе. Без текста: {result.missing} из {rows.length}.
        </p>
        <div className="comparison">
          <b>
            {delta < 0 ? "−" : "+"}
            {Math.abs(delta)} п. п.
          </b>
          <span>
            Октябрь относительно сентября
            <br />
            {counts("sep").percent}% → {counts("oct").percent}%
          </span>
        </div>
      </section>
      <section className="panel change-evidence">
        <SectionTitle detail={periods[period]}>Отзывы периода</SectionTitle>
        {rows.map((r) => (
          <button
            type="button"
            className="source-row"
            key={r.review_id}
            onClick={() => open(r.review_id)}
          >
            <ReviewMeta review={r} />
            <span>
              <Highlight review={r} />
            </span>
            <small>
              {isIssue(r)
                ? "В числителе"
                : r.text
                  ? "Только в знаменателе"
                  : "Исключён: нет текста"}{" "}
              <Icon name="external" size={14} />
            </small>
          </button>
        ))}
      </section>
    </div>
  );
}
