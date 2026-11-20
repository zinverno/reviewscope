import { useState } from "react";
import { inPeriod } from "./analysis";
import { labels } from "./fixture";
import type { Edge, Review } from "./fixture";
import { Icon } from "./shared";

const positions = [
  [64, 70],
  [204, 121],
  [315, 211],
  [320, 57],
  [91, 231],
  [212, 287],
];
export default function Graph({
  rows,
  links,
  selected,
  peer,
  onNode,
  onPair,
}: {
  rows: Review[];
  links: Edge[];
  selected: Review;
  peer?: Review;
  onNode: (id: string) => void;
  onPair: (a: string, b: string) => void;
}) {
  const [list, setList] = useState(false);
  const all = inPeriod(selected.period);
  const pos = (id: string) => positions[all.findIndex((r) => r.review_id === id)];
  const active = (e: Edge) =>
    (e.a === selected.review_id && e.b === peer?.review_id) ||
    (e.b === selected.review_id && e.a === peer?.review_id);
  return (
    <section className="panel graph-panel" aria-label="Прямые связи">
      <div className="graph-head">
        <h2>Карта связей</h2>
        <fieldset className="seg" aria-label="Вид связей">
          <button
            type="button"
            aria-label="Граф"
            title="Граф"
            aria-pressed={!list}
            onClick={() => setList(false)}
          >
            <Icon name="graph" />
          </button>
          <button
            type="button"
            aria-label="Список связей"
            title="Список связей"
            aria-pressed={list}
            onClick={() => setList(true)}
          >
            <Icon name="list" />
          </button>
        </fieldset>
      </div>
      {list ? (
        <div className="edge-list">
          {links.length === 0 && <p>В этой выборке нет прямых связей.</p>}
          {links.map((e) => (
            <button
              type="button"
              key={e.a + e.b}
              aria-pressed={active(e)}
              onClick={() => onPair(e.a, e.b)}
            >
              <strong>
                #{e.a} ↔ #{e.b}
              </strong>
              <span>{e.type}</span>
            </button>
          ))}
        </div>
      ) : (
        <div className="graph-field">
          <svg viewBox="0 0 390 330" aria-label="Прямые рёбра графа" className="graph-svg">
            <title>Прямые связи между выбранными отзывами</title>
            {links.map((e) => {
              const [x, y] = pos(e.a),
                [tx, ty] = pos(e.b);
              const length = Math.hypot(tx - x, ty - y);
              const angle = (Math.atan2(ty - y, tx - x) * 180) / Math.PI;
              return (
                <g key={e.a + e.b} transform={`translate(${x} ${y}) rotate(${angle})`}>
                  <line
                    x1="0"
                    y1="0"
                    x2={length}
                    y2="0"
                    className={active(e) ? "edge selected" : "edge"}
                  />
                  <foreignObject x="19" y="-10" width={Math.max(1, length - 38)} height="20">
                    <button
                      type="button"
                      aria-label={`Связь ${e.a} и ${e.b} — ${e.type}`}
                      aria-pressed={active(e)}
                      className="edge-hit"
                      onClick={() => onPair(e.a, e.b)}
                    />
                  </foreignObject>
                </g>
              );
            })}
          </svg>
          {rows.map((r) => {
            const [x, y] = pos(r.review_id);
            return (
              <button
                type="button"
                className={`graph-node ${r.review_id === peer?.review_id ? "peer" : ""}`}
                style={{ left: `${(x / 390) * 100}%`, top: `${(y / 330) * 100}%` }}
                aria-label={`Узел ${r.review_id} — ${labels[r.stance]}`}
                aria-pressed={r === selected}
                key={r.review_id}
                onClick={() => onNode(r.review_id)}
              >
                <b>{r.review_id}</b>
                <span>{labels[r.stance]}</span>
              </button>
            );
          })}
        </div>
      )}
      <div className="graph-key">
        <span className="line-key" /> Прямая связь{" "}
        <span>
          {rows.length} узлов · {links.length} связей
        </span>
      </div>
    </section>
  );
}
