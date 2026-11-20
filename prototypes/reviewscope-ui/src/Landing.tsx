import { reviews } from "./fixture";
import { Badge, Highlight, Icon, ReviewMeta } from "./shared";

function QuoteScene({ open }: { open: (id: string) => void }) {
  const shown = reviews.slice(0, 3);
  return (
    <section className="quote-scene" aria-label="Связи трёх синтетических отзывов">
      <svg
        className="hero-links"
        viewBox="0 0 560 420"
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <g fill="none" stroke="#78a9c9" strokeWidth="1.5">
          <path d="M103 85 C45 194 60 222 184 230 S367 216 383 169" />
          <path d="M472 197 C552 293 452 365 277 365" />
        </g>
        <g fill="#9acaf0">
          <circle cx="77" cy="153" r="4" />
          <circle cx="493" cy="268" r="4" />
        </g>
      </svg>
      {shown.map((r, i) => (
        <button
          type="button"
          className={`hero-quote quote-${i}`}
          key={r.review_id}
          onClick={() => open(r.review_id)}
          aria-label={`Открыть отзыв ${r.review_id}`}
        >
          <ReviewMeta review={r} />
          <span className="quote-words">
            «<Highlight review={r} />»
          </span>
          <span className="quote-bottom">
            <Badge stance={r.stance} />
            <Icon name="external" size={13} />
          </span>
        </button>
      ))}
      <span className="scene-caption">
        <span className="line-key" /> Прямые связи
      </span>
    </section>
  );
}
export default function Landing({ open }: { open: (id: string) => void }) {
  return (
    <main id="main" tabIndex={-1}>
      <section className="hero">
        <div className="hero-inner wrap">
          <div className="hero-copy">
            <div className="eyebrow">
              <span className="status-dot" /> Исследование отзывов
            </div>
            <h1>
              За разными
              <br />
              словами —<br />
              <em>общая проблема.</em>
            </h1>
            <p>
              Находите повторяющиеся проблемы в отзывах. Проверяйте исходные фразы и смотрите, что
              меняется со временем.
            </p>
            <div className="hero-actions">
              <a className="button primary" href="#research">
                Исследовать демо <Icon name="arrow" />
              </a>
              <span className="demo-caption">Синтетические данные</span>
            </div>
          </div>
          <QuoteScene open={open} />
        </div>
      </section>
      <section className="features wrap" id="features" aria-labelledby="features-title">
        <div className="map-scene">
          <div className="map-title">
            <Icon name="globe" size={16} /> Евразия
          </div>
          <img
            src="./eurasia.svg"
            alt="Точечная схема береговых очертаний Европы и Азии"
            width="615"
            height="305"
          />
          <span className="map-marker marker-one" aria-hidden="true" />
          <span className="map-marker marker-two" aria-hidden="true" />
          <span className="map-note">Демонстрационная схема</span>
        </div>
        <div className="feature-copy">
          <div className="eyebrow">
            <span className="small-rule" /> Возможности
          </div>
          <h2 id="features-title">
            От общей картины
            <br />
            до <em>конкретной фразы.</em>
          </h2>
          <p>
            Исследуйте одну точку: откройте отзыв, сравните его с соседним и проверьте, говорят ли
            авторы об одном и том же.
          </p>
          <a className="text-link" href="#research">
            Открыть карту связей <Icon name="arrow" size={16} />
          </a>
        </div>
      </section>
      <section className="how wrap" id="how">
        <h2>Как работает исследование</h2>
        <ol>
          <li>
            <b>Выберите отзыв</b>
            <p>Найдите жалобу, отрицание или случай с неполными данными.</p>
          </li>
          <li>
            <b>Проверьте пару</b>
            <p>Прочитайте фрагменты и объяснение прямой связи.</p>
          </li>
          <li>
            <b>Сравните периоды</b>
            <p>Откройте тексты, из которых складывается доля жалоб.</p>
          </li>
        </ol>
        <a className="button secondary" href="#changes">
          Посмотреть изменения <Icon name="arrow" size={16} />
        </a>
      </section>
    </main>
  );
}
