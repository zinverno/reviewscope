import { useState } from "react";
import { counts, inPeriod, isIssue } from "./analysis";
import { issue, periods } from "./fixture";
import type { Period } from "./fixture";
import { Icon } from "./shared";

const states = ["Пустая форма", "Прогресс", "Ошибка"] as const;
export function ImportPreview() {
  const [view, setView] = useState<(typeof states)[number]>("Пустая форма");
  return (
    <section className="panel preview">
      <div className="eyebrow">Макет импорта · PR-13</div>
      <h2>Загрузка отзывов</h2>
      <p>В прототипе недоступно. Переключайте состояния формы на синтетическом примере.</p>
      <fieldset className="seg" aria-label="Состояние формы">
        {states.map((s) => (
          <button type="button" key={s} aria-pressed={view === s} onClick={() => setView(s)}>
            {s}
          </button>
        ))}
      </fieldset>
      <div className="import-box" role="status">
        <Icon name="upload" size={32} />
        {view === "Пустая форма" ? (
          <>
            <h3>Файл ещё не выбран</h3>
            <button type="button" disabled className="button secondary">
              Выбрать CSV / JSON
            </button>
          </>
        ) : view === "Прогресс" ? (
          <>
            <h3>Проверка столбцов · шаг 2 из 3</h3>
            <progress value="2" max="3" aria-label="Демонстрационный прогресс" />
          </>
        ) : (
          <>
            <h3>Отсутствует код точки</h3>
            <p>Добавьте столбец source_place_id или укажите привязку точки для всего файла.</p>
          </>
        )}
      </div>
      <p className="note">
        Этапы: выбор файла → проверка схемы → сверка охвата. Поля примера: источник, код точки и
        отзыва, текст, оценка, даты публикации и выгрузки.
      </p>
    </section>
  );
}
export function ReportPreview({ period }: { period: Period }) {
  const { hits, total, missing } = counts(period);
  const example = inPeriod(period).find(isIssue);
  return (
    <section className="panel preview report">
      <div className="eyebrow">Макет клиентского отчёта</div>
      <h2>{issue}</h2>
      <p className="report-date">Демонстрационная точка · {periods[period]} 2026</p>
      <h3>Изменение и основания</h3>
      <p>
        В {hits} из {total} текстовых отзывов есть жалоба на выдачу. Записей без текста: {missing}.
      </p>
      {example && (
        <blockquote>
          «{example.text}»<cite>Отзыв #{example.review_id}</cite>
        </blockquote>
      )}
      <button type="button" disabled className="button secondary">
        <Icon name="file" /> Скачать PDF
      </button>
      <p className="note">Экспорт и передача клиенту в прототипе недоступны.</p>
    </section>
  );
}
