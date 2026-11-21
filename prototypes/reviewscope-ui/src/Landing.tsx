import { useAtlasState } from "./atlasState";
import { AtlasDetails, AtlasStage } from "./Atlas";
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
                <span className="folio">Умная карта научных обзоров / 2026</span>
              </header>
              <div className="opening-grid">
                <div className="opening-copy">
                  <div className="eyebrow">Инструмент для авторов статей</div>
                  <h1>
                    Ваша работа
                    <br />
                    заслуживает быть <em>замеченной</em>
                  </h1>
                  <p>
                    Перестаньте гадать, кто может процитировать статью. Мы находим рецензентов по всей их научной истории и
                    показываем, где ваша тема уже вызывает интерес.
                  </p>
                </div>
                <ReviewBoard />
              </div>
            </div>
          </section>
          <AtlasStage state={atlas} />
        </div>
      </div>
      <AtlasDetails state={atlas} />
      <StorySteps />
    </main>
  );
}
