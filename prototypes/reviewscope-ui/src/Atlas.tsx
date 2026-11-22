import { useRef, type MouseEvent } from "react";
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
  onDetails,
}: {
  name: string;
  result: Result;
  period: Period;
  onDetails?: (event: MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <section className="atlas-proof glass compact-proof" aria-label={`Основания расчёта · ${name}`}>
      <div className="folio">
        {periods[period]} 2026 · {name}
      </div>
      <h3>{issue}</h3>
      <Ratio result={result} />
      <Quote result={result} />
      {onDetails && (
        <button type="button" className="details-action" onClick={onDetails}>
          Состав расчёта
        </button>
      )}
    </section>
  );
}

function CalculationDetails({ name, result }: { name: string; result: Result }) {
  return (
    <section className="atlas-proof glass" aria-label={`Состав расчёта · ${name}`}>
      <div className="folio">{name}</div>
      <h3>Исходные записи · {result.rows.length}</h3>
      <Ratio result={result} />
      <p>Без текста: {result.missing}. Каждая текстовая запись учтена один раз.</p>
      <ul>
        {result.rows.map((row) => (
          <li key={row.id}>
            <small>
              {places.find((place) => place.id === row.place)?.name} · {row.id} ·{" "}
              {labels[row.stance]}
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
    </section>
  );
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
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const other = state.mode === "Сравнение" ? state.comparison : null;
  function openDetails(event: MouseEvent<HTMLButtonElement>) {
    trigger.current = event.currentTarget;
    dialog.current?.showModal();
    dialog.current?.focus({ preventScroll: true });
  }

  function selectPin(event: MouseEvent<HTMLButtonElement>) {
    const clicked = event.currentTarget;
    const buttons = Array.from(
      clicked.parentElement?.querySelectorAll<HTMLButtonElement>(".pin") ?? [],
    );
    // Neighbouring touch targets overlap on small maps. Resolve a completed click by centre.
    const distance = (button: HTMLButtonElement) => {
      const box = button.getBoundingClientRect();
      return Math.hypot(
        event.clientX - box.left - box.width / 2,
        event.clientY - box.top - box.height / 2,
      );
    };
    const target =
      event.detail && clicked === event.target
        ? buttons.reduce(
            (best, button) => (distance(button) < distance(best) ? button : best),
            clicked,
          )
        : clicked;
    state.choosePlace(target.dataset.place as PlaceId);
  }

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
                  }}
                  className="pin"
                  onClick={selectPin}
                  data-place={place.id}
                  data-particle={placeParticles().get(place.id)}
                  aria-pressed={
                    state.mode === "Моя сеть"
                      ? state.side === place.id
                      : state.selected === place.id
                  }
                  aria-label={`На карте: ${place.name}`}
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
        <CalculationCard
          onDetails={openDetails}
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
      <dialog
        className="atlas-details"
        ref={dialog}
        tabIndex={-1}
        aria-labelledby="proof-title"
        onClose={() => trigger.current?.focus({ preventScroll: true })}
      >
        <header>
          <h2 id="proof-title">Состав расчёта</h2>
          <button type="button" onClick={() => dialog.current?.close()}>
            Закрыть
          </button>
        </header>
        <p>{state.periodLabel} 2026</p>
        {state.mode === "Моя сеть" && state.side === "all" && <NetworkBreakdown state={state} />}
        <CalculationDetails
          name={other ? state.comparisonName : state.name}
          result={state.summary}
        />
        {other && <CalculationDetails name={state.otherName} result={other} />}
      </dialog>
    </section>
  );
}
