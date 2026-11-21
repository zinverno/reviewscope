import { useEffect, useRef, useState } from "react";
import { isIssue } from "./analysis";
import { issue, labels, periods, type Period } from "./fixture";
import { fraction, mapFrame, own, places, selection, type PlaceId } from "./network";
import { particleCenter, placeParticles } from "./pointTexture";
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
  const visibleIds = visible.map((p) => p.id);
  const selected = mode === "Одна точка" ? point : mode === "Сравнение" ? side : "";
  const ids = side === "all" ? own.map((p) => p.id) : [side];
  const name = places.find((p) => p.id === side)?.name ?? "Вся сеть";
  const summary = selection(ids, period);
  const comparison = mode === "Сравнение";
  const ownName = places.find((p) => p.id === point)?.name;
  const plane = useRef<HTMLDivElement>(null),
    chooseRef = useRef(choose),
    pointerPick = useRef(false);
  chooseRef.current = choose;
  useEffect(() => {
    document.getElementById("features")?.dispatchEvent(new Event("map-selection"));
    const node = plane.current;
    if (!node) return;
    function pick(event: PointerEvent) {
      if (!node) return;
      const box = node.getBoundingClientRect(),
        x = event.clientX - box.left,
        y = event.clientY - box.top;
      let nearest: PlaceId | null = null,
        distance = 44 * 44;
      for (const button of node.querySelectorAll<HTMLButtonElement>(".pin")) {
        const target = button.getBoundingClientRect(),
          dx = x - (target.left + target.width / 2 - box.left),
          dy = y - (target.top + target.height / 2 - box.top),
          candidate = dx * dx + dy * dy;
        if (candidate < distance) {
          distance = candidate;
          nearest = button.dataset.place as PlaceId;
        }
      }
      if (nearest) {
        pointerPick.current = true;
        chooseRef.current(nearest);
      }
    }
    node.addEventListener("pointerdown", pick, true);
    return () => node.removeEventListener("pointerdown", pick, true);
  });
  return (
    <section
      id="features"
      className="atlas story-section"
      aria-labelledby="atlas-title"
      data-selected={selected}
      data-places={visibleIds.join(",")}
    >
      <div className="wrap atlas-heading">
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
      </div>
      <div className="atlas-stage">
        <div className="cartography">
          <div
            className="atlas-plane"
            ref={plane}
            style={{ aspectRatio: `${mapFrame.width} / ${mapFrame.height}` }}
          >
            {visible.map((p) => {
              const center = particleCenter(p.id);
              return (
                <button
                  type="button"
                  key={p.id}
                  style={{
                    left: `${(100 * (center.x - mapFrame.left)) / mapFrame.width}%`,
                    top: `${(100 * center.y) / mapFrame.height}%`,
                  }}
                  className="pin"
                  data-place={p.id}
                  data-particle={placeParticles().get(p.id)}
                  aria-pressed={mode === "Моя сеть" ? side === p.id : selected === p.id}
                  aria-label={`На карте: ${p.name}`}
                  onClick={() => {
                    if (pointerPick.current) {
                      pointerPick.current = false;
                      return;
                    }
                    choose(p.id);
                  }}
                >
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
          {mode === "Моя сеть" && side !== "all" && (
            <button type="button" className="network-total" onClick={() => setSide("all")}>
              ← К итогу всей сети
            </button>
          )}
          {comparison && (
            <p className="comparison-caption">Демонстрационное сравнение · {periods[period]}</p>
          )}
          <Calculation
            key={`${side}-${period}-${mode}`}
            name={
              comparison
                ? side === "ray"
                  ? "Другая компания · Луч"
                  : `Своя точка · ${name}`
                : name
            }
            result={summary}
            period={period}
          />
          {comparison && (
            <Calculation
              key={`other-${point}-${side}-${period}`}
              name={side === "ray" ? `Своя точка · ${ownName}` : "Другая компания · Луч"}
              result={selection([side === "ray" ? point : "ray"], period)}
              period={period}
            />
          )}
          {mode === "Моя сеть" && side === "all" && (
            <dl className="network-breakdown" aria-label="Доли по точкам сети">
              {own.map((p) => {
                const part = selection([p.id], period);
                return (
                  <div key={p.id}>
                    <dt>{p.name}</dt>
                    <dd>
                      {fraction(part)} · {part.hits}/{part.total}
                    </dd>
                  </div>
                );
              })}
            </dl>
          )}
        </div>
      </div>
    </section>
  );
}
