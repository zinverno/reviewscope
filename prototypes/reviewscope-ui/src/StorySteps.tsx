import { useState } from "react";
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
  const [step, choose] = useState(0);
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
          {titles.map((title, i) => (
            <article className="step glass" key={title} data-current={step === i}>
              <button
                type="button"
                className="step-select"
                aria-expanded={step === i}
                aria-controls={`step-detail-${i}`}
                onClick={() => choose(i)}
              >
                <span className="step-no">0{i + 1}</span>
                <strong>{title}</strong>
                <span aria-hidden="true">↘</span>
              </button>
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
              <div hidden={step !== i} id={`step-detail-${i}`} className="step-detail">
                <p>{explanations[i]}</p>
                {i < 2 && <a className="inline-link" href={`#research?review=0${i + 1}`}>Исследовать отзывы <Icon name="arrow" size={15} /></a>}
                {i === 2 && (
                  <a className="inline-link" href="#changes">
                    Открыть изменения <Icon name="arrow" size={15} />
                  </a>
                )}
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
