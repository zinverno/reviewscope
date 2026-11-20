import { useRef, useState } from "react";
import { dateLabel } from "./analysis";
import { labels, reviews, type Review } from "./fixture";
import { Badge, Highlight, Icon } from "./shared";
import { useCardBoard } from "./useCardBoard";

function ReviewCard({
  review: r,
  front,
  raise,
  menu,
  toggleMenu,
}: {
  review: Review;
  front: boolean;
  raise: () => void;
  menu: boolean;
  toggleMenu: () => void;
}) {
  const [auto, setAuto] = useState({ hover: false, focus: false });
  const [choice, setChoice] = useState<boolean | null>(null);
  const touch = useRef(false);
  const opened = choice ?? (auto.hover || auto.focus);
  const content = `quote-${r.review_id}`;
  return (
    <article
      className="review-slip glass"
      data-front={front}
      data-open={opened}
      onPointerEnter={(e) => {
        if (
          e.pointerType !== "touch" &&
          !e.currentTarget.parentElement?.hasAttribute("data-dragging")
        ) {
          setChoice((v) => (v === false ? null : v));
          setAuto((a) => ({ ...a, hover: true }));
          raise();
        }
      }}
      onPointerLeave={(e) => {
        if (!e.currentTarget.parentElement?.hasAttribute("data-dragging"))
          setAuto((a) => ({ ...a, hover: false }));
      }}
      onPointerDownCapture={(e) => {
        touch.current = e.pointerType === "touch";
        raise();
      }}
      onFocus={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) {
          raise();
          if (!touch.current && !e.currentTarget.parentElement?.hasAttribute("data-dragging")) {
            setChoice((v) => (v === false ? null : v));
            setAuto((a) => ({ ...a, focus: true }));
          }
        }
      }}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) {
          touch.current = false;
          setAuto((a) => ({ ...a, focus: false }));
        }
      }}
    >
      <div className="slip-top">
        <span className="folio">Отзыв · {dateLabel(r.date)}</span>
        <button
          type="button"
          className="grip"
          aria-label={`Переместить #${r.review_id}`}
          aria-expanded={menu}
          onClick={toggleMenu}
        >
          ⠿
        </button>
      </div>
      <button
        type="button"
        className="slip-trigger"
        aria-controls={content}
        aria-expanded={opened}
        aria-label={`#${r.review_id} · ${labels[r.stance]}`}
        onClick={() => setChoice(!opened)}
      >
        <strong>#{r.review_id}</strong>
        <Badge stance={r.stance} />
        <span className="chevron" aria-hidden="true">
          {opened ? "−" : "↘"}
        </span>
      </button>
      <div id={content} hidden={!opened} className="slip-contents">
        <blockquote>
          «<Highlight review={r} />»
        </blockquote>
        <a href={`#research?review=${r.review_id}`}>
          Разобрать отзыв <Icon name="arrow" size={15} />
        </a>
      </div>
    </article>
  );
}
export default function ReviewBoard() {
  const { board, move, reset } = useCardBoard();
  const [front, setFront] = useState(0),
    [menu, setMenu] = useState<number | null>(null);
  return (
    <div className="review-board">
      <div className="board-canvas" ref={board}>
        <svg className="threads" aria-hidden="true">
          <path />
          <path strokeDasharray="4 6" />
        </svg>
        {reviews.slice(0, 3).map((review, i) => (
          <ReviewCard
            key={review.review_id}
            review={review}
            front={i === front}
            raise={() => setFront(i)}
            menu={menu === i}
            toggleMenu={() => setMenu(menu === i ? null : i)}
          />
        ))}
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
          onClick={() => {
            reset.current();
            setMenu(null);
          }}
        >
          Сбросить расположение
        </button>
      </div>
      {menu !== null && (
        <fieldset className="moves">
          <legend>Переместить #{reviews[menu].review_id}</legend>
          {(
            [
              ["Влево", "←", -32, 0],
              ["Вверх", "↑", 0, -28],
              ["Вниз", "↓", 0, 28],
              ["Вправо", "→", 32, 0],
            ] as const
          ).map(([name, arrow, x, y]) => (
            <button
              key={name}
              type="button"
              aria-label={name}
              onClick={() => move.current(menu, x, y)}
            >
              {arrow}
            </button>
          ))}
          <button type="button" onClick={() => setMenu(null)}>
            Готово
          </button>
        </fieldset>
      )}
    </div>
  );
}
