import { Activity, useState } from "react";
import { createRoot } from "react-dom/client";
import { issue } from "./fixture";
import Landing from "./Landing";
import { fresh, useNavigation } from "./navigation";
import { Periods } from "./Periods";
import { ImportPreview, ReportPreview } from "./Previews";
import Research from "./Research";
import { Brand, Icon, PeriodPicker } from "./shared";
import "./tokens.css";
import "./style.css";
import "./scenes.css";

const pages = { research: "Исследование", changes: "Изменения", import: "Импорт", report: "Отчёт" };
function App() {
  const { visit, navigate, update } = useNavigation();
  const [menuKey, setMenu] = useState(""),
    [matte, setMatte] = useState(false);
  const page = Object.hasOwn(pages, visit.page) ? (visit.page as keyof typeof pages) : null;
  const menu = menuKey === visit.key;
  const { period } = visit.context;
  return (
    <div className={`app${matte ? " matte" : ""}`}>
      <button
        type="button"
        className="skip"
        onClick={() => document.getElementById(page ? "main" : "landing")?.focus()}
      >
        К содержимому
      </button>
      {page && (
        <header className="top">
          <div className="wrap header-inner">
            <Brand />
            <nav
              id="site-nav"
              className={`nav${menu ? " opened" : ""}`}
              aria-label="Основная навигация"
            >
              {Object.entries(pages).map(([to, label]) => (
                <a
                  key={to}
                  onClick={() => setMenu("")}
                  href={`#${to}`}
                  aria-current={page === to ? "page" : undefined}
                >
                  {label}
                </a>
              ))}
            </nav>
            <div className="header-actions">
              <a href="#home" className="nav-cta">
                <Icon name="back" size={14} /> На главную
              </a>
              <button
                type="button"
                className="menu-button"
                aria-label={menu ? "Закрыть меню" : "Открыть меню"}
                aria-controls="site-nav"
                aria-expanded={menu}
                onClick={() => setMenu(menu ? "" : visit.key)}
              >
                <Icon name={menu ? "close" : "menu"} />
              </button>
            </div>
          </div>
        </header>
      )}
      <Activity mode={page ? "hidden" : "visible"}>
        <Landing active={!page} />
      </Activity>
      {page && (
        <main key={visit.key} tabIndex={-1} id="main" className="workspace wrap route-in">
          <div className="eyebrow">
            <span className="status-dot" />
            Интерактивный прототип · Синтетические данные
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
            {page !== "import" && (
              <PeriodPicker
                period={period}
                onChange={(p) => {
                  if (p !== period) update(fresh(p));
                }}
              />
            )}
          </div>
          {page === "research" ? (
            <Research context={visit.context} update={update} />
          ) : page === "changes" ? (
            <Periods period={period} open={(id) => navigate(`#research?review=${id}`)} />
          ) : page === "import" ? (
            <ImportPreview />
          ) : (
            <ReportPreview period={period} />
          )}
        </main>
      )}
      <footer className="wrap foot">
        <span>Интерактивный прототип · Синтетические данные</span>
        <button type="button" aria-pressed={matte} onClick={() => setMatte(!matte)}>
          Матовые панели <span aria-hidden="true" className="switch" />
        </button>
      </footer>
    </div>
  );
}
createRoot(document.getElementById("root") as HTMLElement).render(<App />);
