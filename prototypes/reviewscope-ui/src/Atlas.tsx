import { useEffect, useRef, type CSSProperties } from "react";
import { isIssue } from "./analysis";
import type { AtlasState, OwnId } from "./atlasState";
import { issue, labels, periods, type Period } from "./fixture";
import { fraction, mapFrame, own, places, type PlaceId } from "./network";
import { particleCenter, placeParticles } from "./pointTexture";
import { Icon, PeriodPicker } from "./shared";

type Result = ReturnType<typeof import("./network").selection>;

function Ratio({ result }: { result: Result }) {
  return (
    <p className="ratio">
      <b>{fraction(result)}</b>{" "}
      <span>
        {result.hits}/{result.total} <small>жалоб / отзывов с текстом</small>
      </span>
    </p>
  );
}

function Quote({ result }: { result: Result }) {
  const row = result.rows.find(isIssue) ?? result.rows.find((item) => item.text);
  if (!row) return null;
  return <blockquote>«{row.text}»</blockquote>;
}

function CalculationCard({
  name,
  result,
  period,
}: {
  name: string;
  result: Result;
  period: Period;
}) {
  return (
    <section className="atlas-proof glass compact-proof" aria-label={`Основания расчёта · ${name}`}>
      <div className="folio">
        {periods[period]} 2026 · {name}
      </div>
      <h3>{issue}</h3>
      <Ratio result={result} />
      <Quote result={result} />
    </section>
  );
}

function CalculationDetails({ name, result }: { name: string; result: Result }) {
  return (
    <section className="atlas-proof glass" aria-label={`Состав расчёта · ${name}`}>
      <div className="folio">{name}</div>
      <details>
        <summary>Состав расчёта · {result.rows.length} записей</summary>
        <p>Без текста: {result.missing}. Каждая текстовая запись учтена один раз.</p>
        <ul>
          {result.rows.map((row) => (
            <li key={row.id}>
              <small>
                {places.find((place) => place.id === row.place)?.name} · {row.id} · {labels[row.stance]}
              </small>
              {row.text || "Текст отсутствует"}
              <b>
                {isIssue(row)
                  ? "В числителе и знаменателе"
                  : row.text
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

function openCalculationDetails() {
  const details = document.getElementById("atlas-breakdown");
  if (!details) return;
  details.querySelectorAll("details").forEach((node) => {
    node.open = true;
  });
  details.scrollIntoView({
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    block: "start",
  });
  details.focus({ preventScroll: true });
}

function NetworkBreakdown({ state }: { state: AtlasState }) {
  return (
    <dl className="network-breakdown" aria-label="Доли по точкам сети">
      {own.map((place) => {
        const part = state.breakdown[place.id as OwnId];
        return (
          <div key={place.id}>
            <dt>{place.name}</dt>
            <dd>
              {fraction(part)} · {part.hits}/{part.total}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export function AtlasStage({ state }: { state: AtlasState }) {
  const plane = useRef<HTMLDivElement>(null);
  const chooseRef = useRef(state.choosePlace);
  const pointerPick = useRef(false);
  const other = state.mode === "Сравнение" ? state.comparison : null;
  const measureKey = `${state.mode}|${state.period}|${state.side}|${state.point}`;
  chooseRef.current = state.choosePlace;

  useEffect(() => {
    window.dispatchEvent(new CustomEvent("story-measure", { detail: measureKey }));
  }, [measureKey]);

  useEffect(() => {
    const currentNode = plane.current;
    if (!currentNode) return;
    const node = currentNode;
    function pick(event: PointerEvent) {
      const box = node.getBoundingClientRect();
      const x = event.clientX - box.left;
      const y = event.clientY - box.top;
      let nearest: PlaceId | null = null;
      let distance = 44 * 44;
      for (const button of node.querySelectorAll<HTMLButtonElement>(".pin")) {
        const target = button.getBoundingClientRect();
        const dx = x - (target.left + target.width / 2 - box.left);
        const dy = y - (target.top + target.height / 2 - box.top);
        const candidate = dx * dx + dy * dy;
        if (candidate < distance) {
          distance = candidate;
          nearest = button.dataset.place as PlaceId;
        }
      }
      if (nearest) {
        pointerPick.current = true;
        chooseRef.current(nearest);
        window.dispatchEvent(new Event("map-selection"));
      }
    }
    node.addEventListener("pointerdown", pick, true);
    return () => node.removeEventListener("pointerdown", pick, true);
  }, []);

  return (
    <section
      className="atlas story-section"
      id="features"
      aria-labelledby="atlas-title"
      data-selected={state.selected}
      data-places={state.visibleIds.join(",")}
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
            {state.modes.map((modeName) => (
              <button
                type="button"
                key={modeName}
                aria-pressed={state.mode === modeName}
                onClick={() => state.changeMode(modeName)}
              >
                {modeName}
              </button>
            ))}
          </fieldset>
          <PeriodPicker period={state.period} onChange={state.changePeriod} />
        </div>
      </div>

      <div className="atlas-stage">
        <div className="cartography">
          <div
            className="atlas-plane"
            ref={plane}
            style={{ aspectRatio: `${mapFrame.width} / ${mapFrame.height}` }}
          >
            {state.visible.map((place) => {
              const center = particleCenter(place.id);
              return (
                <button
                  type="button"
                  key={place.id}
                  style={{
                    left: `${(100 * (center.x - mapFrame.left)) / mapFrame.width}%`,
                    top: `${(100 * center.y) / mapFrame.height}%`,
                    "--pin": placeParticles().get(place.id),
                  } as CSSProperties}
                  className="pin"
                  data-place={place.id}
                  data-particle={placeParticles().get(place.id)}
                  aria-pressed={state.mode === "Моя сеть" ? state.side === place.id : state.selected === place.id}
                  aria-label={`На карте: ${place.name}`}
                  onClick={() => {
                    if (pointerPick.current) {
                      pointerPick.current = false;
                      return;
                    }
                    state.choosePlace(place.id);
                    window.dispatchEvent(new Event("map-selection"));
                  }}
                >
                  <span>{place.name}</span>
                </button>
              );
            })}
          </div>
          <span className="carto-caption">
            <Icon name="globe" size={14} /> Евразия · Демонстрационная схема
          </span>
        </div>
      </div>

      <div className="wrap atlas-brief">
        {state.mode === "Моя сеть" && state.side !== "all" ? (
          <button type="button" className="network-total glass" onClick={state.backToNetwork}>
            <span>←</span> К итогу всей сети
          </button>
        ) : null}
        {state.mode === "Сравнение" ? (
          <p className="comparison-caption">Демонстрационное сравнение · {state.periodLabel}</p>
        ) : null}
        <button type="button" className="details-action glass" onClick={openCalculationDetails}>
          Состав расчёта
        </button>
        <CalculationCard
          key={`${state.side}-${state.period}-${state.mode}`}
          name={state.mode === "Сравнение" ? state.comparisonName : state.name}
          result={state.summary}
          period={state.period}
        />
        {other ? (
          <CalculationCard
            key={`other-${state.point}-${state.side}-${state.period}`}
            name={state.otherName}
            result={other}
            period={state.period}
          />
        ) : null}
      </div>
    </section>
  );
}

export function AtlasDetails({ state }: { state: AtlasState }) {
  const other = state.mode === "Сравнение" ? state.comparison : null;
  return (
    <section
      className="wrap atlas-details"
      id="atlas-breakdown"
      aria-label="Детали карты"
      tabIndex={-1}
    >
      {state.mode === "Моя сеть" && state.side === "all" ? <NetworkBreakdown state={state} /> : null}
      <CalculationDetails
        name={state.mode === "Сравнение" ? state.comparisonName : state.name}
        result={state.summary}
      />
      {other ? <CalculationDetails name={state.otherName} result={other} /> : null}
    </section>
  );
}

export default function Atlas({ state }: { state: AtlasState }) {
  return (
    <>
      <AtlasStage state={state} />
      <AtlasDetails state={state} />
    </>
  );
}
