import { useEffect, useRef, useState } from "react";
import { dateLabel } from "./analysis";
import { labels, reviews } from "./fixture";
import { Badge, Highlight, Icon } from "./shared";
import { useCardBoard } from "./useCardBoard";

const cards = reviews.slice(0, 3);
export default function ReviewBoard() {
  const { board, move, reset } = useCardBoard();
  const [shown, show] = useState<number | null>(null),
    [front, raise] = useState(0),
    [moving, setMoving] = useState(false);
  const touch = useRef(false),
    tapWasOpen = useRef(false),
    hover = useRef<number | null>(null);
  useEffect(() => {
    function dismiss(e: KeyboardEvent) {
      if (e.key === "Escape") {
        show(null);
        setMoving(false);
      }
    }
    window.addEventListener("keydown", dismiss);
    return () => window.removeEventListener("keydown", dismiss);
  }, []);
  return (
    <div className="review-board">
      <div className="board-canvas" ref={board} data-move={moving || undefined}>
        <svg className="threads" aria-hidden="true">
          <path />
          <path strokeDasharray="4 6" />
        </svg>
        {cards.map((r, i) => {
          const back = shown === i;
          return (
            <article
              className="review-slip"
              key={r.review_id}
              data-front={front === i}
              data-open={back}
            >
              <a
                className="slip-shell glass"
                draggable={false}
                href={`#research?review=${r.review_id}`}
                aria-label={`Открыть отзыв #${r.review_id}`}
                aria-describedby={`face-${r.review_id}-${back ? "back" : "front"}`}
                onPointerEnter={(e) => {
                  if (e.pointerType !== "touch" && !board.current?.hasAttribute("data-pending")) {
                    hover.current = i;
                    show(i);
                    raise(i);
                  }
                }}
                onPointerLeave={(e) => {
                  if (e.pointerType !== "touch") {
                    hover.current = null;
                    if (
                      !board.current?.hasAttribute("data-pending") &&
                      document.activeElement !== e.currentTarget
                    )
                      show((v) => (v === i ? null : v));
                  }
                }}
                onPointerDownCapture={(e) => {
                  touch.current = e.pointerType === "touch";
                  tapWasOpen.current = back;
                  raise(i);
                }}
                onFocus={() => {
                  if (!touch.current && !board.current?.hasAttribute("data-pending")) {
                    show(i);
                    raise(i);
                  }
                }}
                onBlur={() => {
                  touch.current = false;
                  if (hover.current !== i && !board.current?.hasAttribute("data-pending"))
                    show((v) => (v === i ? null : v));
                }}
                onClick={(e) => {
                  if (moving) {
                    e.preventDefault();
                    raise(i);
                    show(i);
                    return;
                  }
                  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                  if (
                    e.detail > 0 &&
                    touch.current &&
                    !tapWasOpen.current &&
                    !(e.target as Element).closest(".slip-arrow")
                  ) {
                    e.preventDefault();
                    show(i);
                  }
                }}
              >
                <span className="visually-hidden">Открыть отзыв #{r.review_id}</span>
                <span
                  className="slip-face face-front"
                  aria-hidden={back}
                  id={`face-${r.review_id}-front`}
                >
                  <span className="folio">Отзыв · {dateLabel(r.date)}</span>
                  <span className="slip-title">
                    <strong>#{r.review_id}</strong>
                    <Badge stance={r.stance} />
                  </span>
                </span>
                <span
                  className="slip-face face-back"
                  aria-hidden={!back}
                  id={`face-${r.review_id}-back`}
                >
                  «<Highlight review={r} />»
                </span>
                <span className="slip-arrow" aria-hidden="true">
                  <Icon name="external" size={18} />
                </span>
              </a>
            </article>
          );
        })}
      </div>
      <div className="board-tools">
        <span>
          <i /> Одна проблема
        </span>
        <span>
          <i /> Общая тема
        </span>
        <button
          type="button"
          aria-pressed={moving}
          aria-controls="move-options"
          onClick={() => setMoving(!moving)}
        >
          {moving ? "Готово" : "Перемещение"}
        </button>
        <button type="button" onClick={() => reset.current()}>
          Сбросить расположение
        </button>
      </div>
      {moving && (
        <fieldset className="moves" id="move-options">
          <legend>Перемещение карточки</legend>
          <select
            aria-label="Карточка для перемещения"
            value={front}
            onChange={(e) => {
              const i = Number(e.target.value);
              raise(i);
              show(i);
            }}
          >
            {cards.map((r, i) => (
              <option key={r.review_id} value={i}>
                #{r.review_id} · {labels[r.stance]}
              </option>
            ))}
          </select>
          {(
            [
              ["Влево", "←", -32, 0],
              ["Вверх", "↑", 0, -28],
              ["Вниз", "↓", 0, 28],
              ["Вправо", "→", 32, 0],
            ] as const
          ).map(([label, symbol, dx, dy]) => (
            <button
              type="button"
              key={label}
              aria-label={label}
              onClick={() => move.current(front, dx, dy)}
            >
              {symbol}
            </button>
          ))}
        </fieldset>
      )}
    </div>
  );
}
