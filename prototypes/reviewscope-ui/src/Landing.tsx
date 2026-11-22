import { useAtlasState } from "./atlasState";
import { AtlasStage } from "./Atlas";
import PointField from "./PointField";
import ReviewBoard from "./ReviewBoard";
import StorySteps from "./StorySteps";

type Props = {
  active: boolean;
};

export default function Landing({ active }: Props) {
  const atlas = useAtlasState();

  return (
    <main id="landing" tabIndex={-1} className="landing">
      <div className="story" id="story">
        <div className="story-stage">
          <PointField active={active} />
          <section className="opening" id="home" aria-label="Вступление">
            <div className="wrap opening-wrap">
              <header className="mast">
                <strong className="wordmark">
                  review<span>scope</span>
                </strong>
                <span className="folio">Интерактивный прототип · Синтетические данные</span>
              </header>
              <div className="opening-grid">
                <div className="opening-copy">
                  <div className="eyebrow">Исследование отзывов</div>
                  <h1>
                    За разными словами —
                    <br />
                    <em>общая проблема.</em>
                  </h1>
                  <p>
                    Находите повторяющиеся проблемы в отзывах. Проверяйте исходные фразы и сравнивайте изменения — в одной точке или по всей сети.
                  </p>
                </div>
                <ReviewBoard />
              </div>
            </div>
          </section>
          <AtlasStage state={atlas} />
          <StorySteps />
        </div>
      </div>
    </main>
  );
}
