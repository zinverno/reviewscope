import { useEffect, useRef, useState } from "react";
import { counts } from "./analysis";
import { labels, reviews } from "./fixture";
import { Highlight, Icon } from "./shared";

const titles = ["Найти похожее", "Проверить смысл", "Сравнить изменения"];
const explanations = [
  "В целевой системе эмбеддинги находят фрагменты-кандидаты, а реранкер уточняет релевантность. Здесь связь двух отзывов задана вручную.",
  "Модели решений будут различать позиции по конкретной проблеме. В примере метки заданы вручную: у #02 жалоба, у #03 отрицание. Основание можно проверить по словам автора.",
  "Каждая запись с текстом учитывается один раз. В октябре одна запись без текста исключена. Откройте отзывы периода, чтобы проверить числитель и знаменатель.",
];
export default function StorySteps() {
  const [hovered, setHovered] = useState<number | null>(null);
  const [focused, setFocused] = useState<number | null>(null);
  const firstTouch = useRef(false);
  useEffect(() => {
    function dismiss(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setHovered(null);
        setFocused(null);
      }
    }
    window.addEventListener("keydown", dismiss);
    return () => window.removeEventListener("keydown", dismiss);
  }, []);
  const sep = counts("sep"),
    oct = counts("oct");
  return (
    <section className="method story-section" id="how" aria-labelledby="method-title">
      <div className="wrap">
        <div className="folio">
          <span className="rule" />
          Пример проверки одной точки
        </div>
        <div className="method-head">
          <h2 id="method-title">
            От фразы — к <em>проверяемому изменению.</em>
          </h2>
        </div>
        <div className="steps-track">
          {titles.map((title, i) => {
            const back = hovered === i || focused === i;
            const id = `step-back-${i}`;
            return (
              <a
                key={title}
                className="step glass"
                href={i < 2 ? `#research?review=0${i + 1}` : "#changes"}
                aria-labelledby={`step-title-${i}`}
                aria-describedby={back ? id : undefined}
                onPointerEnter={(e) => {
                  if (e.pointerType !== "touch") setHovered(i);
                }}
                onPointerLeave={(e) => {
                  if (e.pointerType !== "touch") setHovered(null);
                }}
                onPointerDown={(e) => {
                  firstTouch.current = e.pointerType === "touch" && !back;
                }}
                onPointerCancel={() => {
                  firstTouch.current = false;
                }}
                onFocus={() => setFocused(i)}
                onBlur={() => setFocused(null)}
                onClick={(e) => {
                  if (firstTouch.current && e.detail > 0) {
                    e.preventDefault();
                    e.currentTarget.focus({ preventScroll: true });
                    setFocused(i);
                  }
                  firstTouch.current = false;
                }}
              >
                <span className="visually-hidden" id={`step-title-${i}`}>{title}</span>
                <div className="slip-face step-front" aria-hidden={back}>
                  <span className="step-no">0{i + 1}</span>
                  <strong>{title}</strong>
                </div>
                <div className="slip-face step-back" id={id} aria-hidden={!back}>
                  {i < 2 ? (
                    <div className="phrase-pair">
                      {reviews.slice(i, i + 2).map((r) => (
                        <p key={r.review_id}>
                          <small>
                            #{r.review_id} · {labels[r.stance]}
                          </small>{" "}
                          «<Highlight review={r} />»
                        </p>
                      ))}
                    </div>
                  ) : (
                    <div className="step-result">
                      <strong>
                        −{(sep.percent ?? 0) - (oct.percent ?? 0)} <small>п. п.</small>
                      </strong>
                      <p>
                        Сентябрь · {sep.hits}/{sep.total} = {sep.percent}%<br />
                        Октябрь · {oct.hits}/{oct.total} = {oct.percent}%
                      </p>
                    </div>
                  )}
                  <p className="step-detail">{explanations[i]}</p>
                  <span className="inline-link">
                    {i < 2 ? "Исследовать отзывы" : "Открыть изменения"}{" "}
                    <Icon name="arrow" size={15} />
                  </span>
                </div>
              </a>
            );
          })}
        </div>
      </div>
    </section>
  );
}
