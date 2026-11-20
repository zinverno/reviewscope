import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { inPeriod } from "./analysis";
import { issue, reviews } from "./fixture";
import type { Period } from "./fixture";
import Landing from "./Landing";
import { Periods } from "./Periods";
import { ImportPreview, ReportPreview } from "./Previews";
import Research from "./Research";
import { Brand, Icon, PeriodPicker } from "./shared";
import "./tokens.css";
import "./style.css";

const pages = { research: "Исследование", changes: "Изменения", import: "Импорт", report: "Отчёт" };
type Page = keyof typeof pages;
function readHash() {
  return window.location.hash.slice(1) || "home";
}
function App() {
  const [hash, setHash] = useState(readHash);
  const [menu, setMenu] = useState(false);
  const [matte, setMatte] = useState(false);
  const [period, setPeriod] = useState<Period>("sep");
  const [id, setId] = useState("01");
  const initial = useRef(true);
  const page = Object.hasOwn(pages, hash) ? (hash as Page) : null;
  useEffect(() => {
    function change() {
      setHash(readHash());
      setMenu(false);
    }
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  useEffect(() => {
    const section = hash === "features" || hash === "how";
    const el = document.getElementById(section ? hash : "main");
    if (section) el?.scrollIntoView();
    else {
      window.scrollTo(0, 0);
      if (!initial.current) el?.focus({ preventScroll: true });
    }
    initial.current = false;
    document.title = `${page ? pages[page] : "Исследование отзывов"} · ReviewScope`;
  }, [page, hash]);
  function changePeriod(p: Period) {
    if (p !== period) {
      setPeriod(p);
      setId(inPeriod(p)[0].review_id);
    }
  }
  function open(id: string) {
    const r = reviews.find((r) => r.review_id === id);
    if (r) {
      setId(id);
      setPeriod(r.period);
      window.location.hash = "research";
    }
  }
  const nav = page
    ? Object.entries(pages)
    : [
        ["features", "Возможности"],
        ["how", "Как работает"],
      ];
  return (
    <div className={`app${matte ? " matte" : ""}`}>
      <button
        type="button"
        className="skip"
        onClick={() => document.getElementById("main")?.focus()}
      >
        К содержимому
      </button>
      <header className="top">
        <div className="wrap header-inner">
          <Brand />
          <nav
            aria-label="Основная навигация"
            id="site-nav"
            className={`nav${menu ? " opened" : ""}`}
          >
            {nav.map(([to, label]) => (
              <a
                href={`#${to}`}
                key={to}
                aria-current={hash === to ? "page" : undefined}
                onClick={() => setMenu(false)}
              >
                {label}
              </a>
            ))}
          </nav>
          <div className="header-actions">
            {page ? (
              <a href="#home" className="nav-cta">
                <Icon name="back" size={14} /> На главную
              </a>
            ) : (
              <a href="#research" className="nav-cta">
                Открыть демо <Icon name="external" size={15} />
              </a>
            )}
            <button
              type="button"
              className="menu-button"
              aria-label={menu ? "Закрыть меню" : "Открыть меню"}
              aria-controls="site-nav"
              aria-expanded={menu}
              onClick={() => setMenu(!menu)}
            >
              <Icon name={menu ? "close" : "menu"} />
            </button>
          </div>
        </div>
      </header>
      {page ? (
        <main tabIndex={-1} id="main" className="workspace wrap">
          <div className="eyebrow">
            <span className="status-dot" /> Демо · Одна точка
          </div>
          <div className="page-heading">
            <div>
              <h1>{pages[page]}</h1>
              <p>
                {page === "research"
                  ? issue
                  : page === "changes"
                    ? "Динамика по двум синтетическим периодам"
                    : page === "import"
                      ? "Предпросмотр будущей формы"
                      : "Предпросмотр будущего экспорта"}
              </p>
            </div>
            {page !== "import" && <PeriodPicker period={period} onChange={changePeriod} />}
          </div>
          {page === "research" ? (
            <Research key={period} id={id} setId={setId} period={period} />
          ) : page === "changes" ? (
            <Periods period={period} open={open} />
          ) : page === "import" ? (
            <ImportPreview />
          ) : (
            <ReportPreview period={period} />
          )}
        </main>
      ) : (
        <Landing open={open} />
      )}
      <footer className="wrap foot">
        <span>
          ReviewScope <span className="foot-slash">/</span> Интерактивный прототип
        </span>
        <button type="button" aria-pressed={matte} onClick={() => setMatte(!matte)}>
          Матовые панели <span aria-hidden="true" className="switch" />
        </button>
      </footer>
    </div>
  );
}
createRoot(document.getElementById("root") as HTMLElement).render(<App />);
