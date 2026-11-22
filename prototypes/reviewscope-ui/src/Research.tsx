import type { Context } from "./navigation";
import { direct, inPeriod } from "./analysis";
import { edges, labels } from "./fixture";
import type { Review, Stance } from "./fixture";
import Graph from "./Graph";
import { Badge, Highlight, Icon, ReviewMeta, SectionTitle } from "./shared";

function Evidence({
  first,
  second,
  options,
  choose,
}: {
  first: Review;
  second?: Review;
  options: Review[];
  choose: (id: string) => void;
}) {
  const edge = second && direct(first.review_id, second.review_id);
  return (
    <section className="panel evidence" id="evidence" aria-label="Почему они связаны">
      <div className="eyebrow">
        <Icon name="link" size={14} /> Основания
      </div>
      <h2>Почему они связаны</h2>
      <label className="pair-selector">
        Сравнить #{first.review_id} с
        <select
          value={second?.review_id ?? ""}
          onChange={(e) => choose(e.target.value)}
          disabled={!options.length}
        >
          {!options.length && <option value="">Нет второго отзыва</option>}
          {options.map((r) => (
            <option key={r.review_id} value={r.review_id}>
              #{r.review_id} · {labels[r.stance]}
            </option>
          ))}
        </select>
      </label>
      <div className="evidence-content" aria-live="polite" aria-atomic="true">
        {[first, ...(second ? [second] : [])].map((r) => (
          <article className="excerpt" key={r.review_id}>
            <ReviewMeta review={r} />
            <blockquote>
              {r.text ? "«" : ""}
              {edge ? <Highlight review={r} /> : r.text || "Текст отсутствует."}
              {r.text ? "»" : ""}
            </blockquote>
            <div className="tags">
              <Badge stance={r.stance} />
              {r.aspect && <small>{r.aspect}</small>}
            </div>
          </article>
        ))}
        <div className="interpretation">
          <strong>
            <Icon name="link" size={15} />
            {edge?.type ?? (second ? "Нет прямой связи" : "Один отзыв")}
          </strong>
          <p>
            {edge?.reason ??
              (second
                ? "Для этой пары связь не задана. Путь через другие отзывы не подтверждает прямое отношение."
                : "Снимите фильтр, чтобы выбрать второй фрагмент.")}
          </p>
        </div>
        {(!first.text ||
          (second && !second.text) ||
          first.rating === null ||
          second?.rating === null) && (
          <p className="note">
            Неполные данные: в источнике отсутствует текст или оценка. Запись без текста исключена
            из доли жалоб.
          </p>
        )}
      </div>
    </section>
  );
}
export default function Research({
  context,
  update,
}: {
  context: Context;
  update: (patch: Partial<Context>) => void;
}) {
  const { period, id, query, stance, other } = context;
  const rows = inPeriod(period).filter(
    (r) =>
      (!stance || stance === r.stance) &&
      `${r.text} #${r.review_id}`
        .toLocaleLowerCase("ru")
        .includes(query.trim().toLocaleLowerCase("ru")),
  );
  const selected = rows.find((r) => r.review_id === id) ?? rows[0];
  const choices = rows.filter((r) => r !== selected);
  const peer =
    choices.find((r) => r.review_id === other) ??
    choices.find((r) => direct(selected.review_id, r.review_id)) ??
    choices[0];
  const ids = new Set(rows.map((r) => r.review_id));
  const links = edges.filter((e) => ids.has(e.a) && ids.has(e.b));
  function select(id: string) {
    update({ id, other: "" });
  }
  function reset() {
    update({ query: "", stance: "" });
  }
  return (
    <>
      <div className="filters">
        <label className="search">
          <Icon name="search" />
          <input
            aria-label="Поиск по тексту или номеру"
            placeholder="Найти фразу или номер…"
            value={query}
            onChange={(e) => update({ query: e.target.value })}
          />
        </label>
        <select
          aria-label="Позиция автора"
          value={stance}
          onChange={(e) => update({ stance: e.target.value as Stance | "" })}
        >
          <option value="">Все позиции</option>
          {Object.entries(labels).map(([k, v]) => (
            <option value={k} key={k}>
              {v}
            </option>
          ))}
        </select>
      </div>
      {selected ? (
        <div className="work-grid">
          <section className="review-list" aria-label="Список отзывов">
            <SectionTitle detail={String(rows.length)}>Отзывы</SectionTitle>
            <div className="rows">
              {rows.map((r) => (
                <button
                  type="button"
                  className="review-row"
                  key={r.review_id}
                  aria-pressed={selected === r}
                  onClick={() => select(r.review_id)}
                >
                  <ReviewMeta review={r} />
                  <span>{r.text || "Оценка без текста"}</span>
                  <Badge stance={r.stance} />
                </button>
              ))}
            </div>
          </section>
          <Graph
            rows={rows}
            links={links}
            selected={selected}
            peer={peer}
            onNode={select}
            onPair={(a, b) => {
              update({ id: a, other: b });
            }}
          />
          <Evidence
            first={selected}
            second={peer}
            options={choices}
            choose={(other) => update({ other })}
          />
        </div>
      ) : (
        <section className="panel empty">
          <Icon name="search" size={28} />
          <h2>Отзывы не найдены</h2>
          <p>Измените фразу или позицию автора.</p>
          <button type="button" className="button secondary" onClick={reset}>
            Сбросить фильтры
          </button>
        </section>
      )}
    </>
  );
}
