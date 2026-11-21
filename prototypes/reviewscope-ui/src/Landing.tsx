import PointField from "./PointField";
import Atlas from "./Atlas";
import ReviewBoard from "./ReviewBoard";
import StorySteps from "./StorySteps";
import { Brand } from "./shared";

export default function Landing({ active }: { active: boolean }) {
  return (
    <main id="landing" tabIndex={-1} className="landing">
      <PointField active={active} />
      <section className="opening">
        <div className="wrap opening-wrap">
          <div className="mast">
            <Brand />
            <span className="folio">Интерактивный прототип · Синтетические данные</span>
          </div>
          <div className="opening-grid">
            <div className="opening-copy">
              <div className="folio">
                <span className="rule" />
                Исследование отзывов
              </div>
              <h1>
                За разными
                <br />
                словами —<br />
                <em>общая проблема.</em>
              </h1>
              <p className="story-sub">
                Находите повторяющиеся проблемы в отзывах. Проверяйте исходные фразы и сравнивайте
                изменения — в одной точке или по всей сети.
              </p>
              <p className="opening-hint">
                <span aria-hidden="true">↗</span>{" "}
                <span className="mouse-hint">
                  Наведите для чтения. Потяните карточку или нажмите для исследования.
                </span>
                <span className="tap-hint">
                  Коснитесь для чтения. Второе касание открывает отзыв.
                </span>
              </p>
            </div>
            <ReviewBoard />
          </div>
          <a className="next-scene" href="#features">
            К точкам на карте <span aria-hidden="true">↓</span>
          </a>
        </div>
      </section>
      <Atlas />
      <StorySteps />
    </main>
  );
}
