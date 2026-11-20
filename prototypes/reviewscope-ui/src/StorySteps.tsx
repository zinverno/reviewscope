import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { counts } from "./analysis";
import { labels, reviews } from "./fixture";
import { Highlight, Icon } from "./shared";

const titles = ["Найти похожее", "Проверить смысл", "Сравнить изменения"];
const explanations = [
  "В целевой системе эмбеддинги находят фрагменты-кандидаты, а реранкер уточняет релевантность. Здесь связь двух отзывов задана вручную.",
  "Модели решений будут различать позиции по конкретной проблеме. В примере метки заданы вручную: у #02 жалоба, у #03 отрицание. Основание можно проверить по словам автора.",
  "Каждая запись с текстом учитывается один раз. В октябре одна запись без текста исключена. Откройте отзывы периода, чтобы проверить числитель и знаменатель.",
];
export default function StorySteps({ active }: { active: boolean }) {
  const section = useRef<HTMLElement>(null),
    track = useRef<HTMLDivElement>(null);
  const [step, setStep] = useState(0),
    [running, run] = useState(false);
  const played = useRef(false);
  const [visible, show] = useState(false),
    [allowed, allow] = useState(false);
  useEffect(() => {
    const host = section.current;
    if (!host || !active) return;
    const media = matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => allow(!media.matches && !document.hidden);
    update();
    media.addEventListener("change", update);
    document.addEventListener("visibilitychange", update);
    const observer = new IntersectionObserver(([entry]) => show(entry.isIntersecting), {
      threshold: 0.12,
    });
    observer.observe(host);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [active]);
  useEffect(() => {
    if (!active || !allowed || !visible) {
      run(false);
      return;
    }
    if (!played.current) {
      played.current = true;
      setStep(0);
      run(true);
    }
  }, [active, allowed, visible]);
  useEffect(() => {
    if (!running || !active || !visible || !allowed) return;
    const timer = window.setTimeout(() => {
      if (step === 2) run(false);
      else setStep(step + 1);
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [active, running, visible, allowed, step]);
  const choose = useCallback((i: number) => {
    played.current = true;
    run(false);
    setStep(i);
  }, []);
  useLayoutEffect(() => {
    const host = track.current;
    if (!host || !running) return;
    function locate() {
      if (!host) return;
      const target = host.querySelectorAll("button")[step],
        box = host.getBoundingClientRect(),
        point = target.getBoundingClientRect();
      host.style.setProperty("--cursor-x", `${point.left - box.left + point.width * 0.77}px`);
      host.style.setProperty("--cursor-y", `${point.top - box.top + 36}px`);
    }
    locate();
    const observer = new ResizeObserver(locate);
    observer.observe(host);
    return () => observer.disconnect();
  }, [step, running]);
  const sep = counts("sep"),
    oct = counts("oct");
  return (
    <section className="method story-section" ref={section} id="how" aria-labelledby="method-title">
      <div className="wrap">
        <div className="folio">
          <span className="rule" />
          Основания вывода
        </div>
        <div className="method-head">
          <h2 id="method-title">
            От фразы — к <em>проверяемому изменению.</em>
          </h2>
          <button
            type="button"
            className="replay"
            onClick={() => {
              played.current = true;
              setStep(0);
              run(visible && allowed);
            }}
          >
            ↻ Повторить
          </button>
        </div>
        <div className="steps-track" ref={track}>
          {titles.map((title, i) => (
            <article
              className="step glass"
              key={title}
              data-current={step === i}
              onPointerDown={() => choose(i)}
              onPointerEnter={(e) => {
                if (e.pointerType !== "touch") choose(i);
              }}
              onFocus={() => choose(i)}
            >
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
                {i === 2 && (
                  <a className="inline-link" href="#changes">
                    Открыть изменения <Icon name="arrow" size={15} />
                  </a>
                )}
              </div>
            </article>
          ))}
          <svg
            className="demo-pointer"
            data-running={running && active && allowed && visible}
            viewBox="0 0 28 32"
            aria-hidden="true"
          >
            <path d="M3 2 24 16 14 18 10 28Z" />
          </svg>
        </div>
      </div>
    </section>
  );
}
