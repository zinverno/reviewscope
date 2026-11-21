import { useState } from "react";
import { isIssue } from "./analysis";
import { issue, labels, periods, type Period } from "./fixture";
import { fraction, own, places, project, selection, type PlaceId } from "./network";
import { Icon, PeriodPicker } from "./shared";

const modes = ["Одна точка", "Моя сеть", "Сравнение"] as const;
type Mode = (typeof modes)[number];
function Calculation({
  name,
  result,
  period,
}: {
  name: string;
  result: ReturnType<typeof selection>;
  period: Period;
}) {
  return (
    <section className="atlas-proof glass" aria-label="Основания расчёта">
      <div className="folio">
        {periods[period]} 2026 · {name}
      </div>
      <h3>{issue}</h3>
      <p className="ratio">
        <b>{fraction(result)}</b>{" "}
        <span>
          {result.hits}/{result.total} <small>жалоб / отзывов с текстом</small>
        </span>
      </p>
      {result.rows.some((r) => r.text) && (
        <blockquote>
          «{(result.rows.find(isIssue) ?? result.rows.find((r) => r.text))?.text}»
        </blockquote>
      )}
      <details>
        <summary>Состав расчёта · {result.rows.length} записей</summary>
        <p>Без текста: {result.missing}. Каждая текстовая запись учтена один раз.</p>
        <ul>
          {result.rows.map((r) => (
            <li key={r.id}>
              <small>
                {places.find((p) => p.id === r.place)?.name} · {r.id} · {labels[r.stance]}
              </small>
              {r.text || "Текст отсутствует"}
              <b>
                {isIssue(r)
                  ? "В числителе и знаменателе"
                  : r.text
                    ? "Только в знаменателе"
                    : "Исключена"}
              </b>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
export default function Atlas() {
  const [mode, setMode] = useState<Mode>("Одна точка");
  const [period, setPeriod] = useState<Period>("sep");
  const [point, setPoint] = useState<PlaceId>("arc");
  const [side, setSide] = useState<PlaceId | "all">("arc");
  function changeMode(next: Mode) {
    setMode(next);
    setSide(next === "Моя сеть" ? "all" : point);
  }
  function choose(p: PlaceId) {
    if (p !== "ray") setPoint(p);
    setSide(p);
  }
  const visible = mode === "Сравнение" ? places : own;
  const ids = side === "all" ? own.map((p) => p.id) : [side];
  const name = places.find((p) => p.id === side)?.name ?? "Вся сеть";
  const summary = selection(ids, period);
  function metric(id: PlaceId | "all", title: string) {
    const value = selection(id === "all" ? own.map((p) => p.id) : [id], period);
    return (
      <button
        type="button"
        key={id}
        aria-pressed={side === id}
        onClick={() => (id === "all" ? setSide("all") : choose(id))}
      >
        <span>{title}</span>
        <strong>{fraction(value)}</strong>{" "}
        <small>
          {value.hits}/{value.total}
        </small>
      </button>
    );
  }
  return (
    <section id="features" className="atlas story-section" aria-labelledby="atlas-title">
      <div className="wrap">
        <div className="folio">
          <span className="rule" />
          География отзывов
        </div>
        <div className="atlas-head">
          <h2 id="atlas-title">
            Одна точка. Вся сеть.
            <br />
            <em>Общая картина.</em>
          </h2>
          <p className="story-sub">
            Смотрите, где проблема повторяется, как меняются отзывы и чем отличаются точки.
          </p>
        </div>
        <div className="atlas-controls">
          <fieldset className="seg" aria-label="Масштаб сравнения">
            {modes.map((m) => (
              <button type="button" aria-pressed={mode === m} key={m} onClick={() => changeMode(m)}>
                {m}
              </button>
            ))}
          </fieldset>
          <PeriodPicker period={period} onChange={setPeriod} />
        </div>
        <div className="cartography">
          <div className="atlas-plane">
            {visible.map((p) => {
              const [x, y] = project(p);
              return (
                <button
                  type="button"
                  key={p.id}
                  style={{ left: `${(100 * x) / 615}%`, top: `${(100 * y) / 305}%` }}
                  className="pin"
                  aria-pressed={side === p.id}
                  aria-label={`На карте: ${p.name}`}
                  onClick={() => choose(p.id)}
                >
                  <i />
                  <span>{p.name}</span>
                </button>
              );
            })}
          </div>
          <span className="carto-caption">
            <Icon name="globe" size={14} /> Евразия · Демонстрационная схема
          </span>
        </div>
        <div className="atlas-data">
          <div className="atlas-index">
            <h3>
              {mode === "Сравнение"
                ? "Демонстрационное сравнение"
                : mode === "Моя сеть"
                  ? "Точки моей сети"
                  : "Выберите точку"}
            </h3>
            {mode === "Сравнение" && <p>Своя точка и точка другой компании · {periods[period]}</p>}
            <fieldset className="place-picker" aria-label="Список точек">
              {visible.map((p) => (
                <button
                  type="button"
                  key={p.id}
                  aria-pressed={side === p.id}
                  onClick={() => choose(p.id)}
                >
                  {p.name}
                  {p.id === "ray" ? " · другая компания" : ""}
                </button>
              ))}
            </fieldset>
            {mode !== "Одна точка" && (
              <div className="metrics">
                {mode === "Моя сеть" ? (
                  <>
                    {metric("all", "Вся сеть")}
                    {own.map((p) => metric(p.id, p.name))}
                  </>
                ) : (
                  <>
                    {metric(point, `Своя · ${places.find((p) => p.id === point)?.name}`)}
                    {metric("ray", "Другая · Луч")}
                  </>
                )}
              </div>
            )}
          </div>
          <Calculation
            key={`${side}-${period}-${mode}`}
            name={name}
            result={summary}
            period={period}
          />
        </div>
      </div>
    </section>
  );
}
