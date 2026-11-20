import type { Aspect, Period, Stance } from "./fixture.ts";
import { summarize } from "./analysis.ts";

export const places = [
  { id: "arc", name: "Арка", lon: 49.1, lat: 55.8 },
  { id: "line", name: "Линия", lon: 82.9, lat: 55.0 },
  { id: "cape", name: "Мыс", lon: 135.1, lat: 48.5 },
  { id: "ray", name: "Луч", lon: 76.9, lat: 43.2 },
] as const;
export type PlaceId = (typeof places)[number]["id"];
export const own = places.slice(0, 3);
type Entry = readonly [Stance, Aspect | null, string];
const snapshots: Record<PlaceId, Record<Period, Entry[]>> = {
  arc: {
    sep: [
      ["complaint", "Выдача", "Готовый пакет ждал за кассой вместе со мной."],
      ["complaint", "Выдача", "Собранный заказ отдали только после напоминания."],
      ["denial", "Выдача", "Пакет забрал сразу."],
    ],
    oct: [
      ["complaint", "Выдача", "После сигнала ещё десять минут искали мой пакет."],
      ["context", "Упаковка", "Крышка держалась плотно."],
      ["denial", "Выдача", "Заказ уже ждал у стойки."],
      ["unclear", "Выдача", "Пришлось ждать, причину не знаю."],
    ],
  },
  line: {
    sep: [
      ["context", "Упаковка", "Коробка доехала целой."],
      ["complaint", "Приготовление", "На кухне задержали суп."],
      ["denial", "Выдача", "Готовое отдали без очереди."],
      ["missing", null, ""],
      ["unclear", "Выдача", "Не понял, где задержался заказ."],
    ],
    oct: [
      ["complaint", "Выдача", "Упакованный заказ держали за стойкой."],
      ["denial", "Выдача", "Подошёл и сразу получил пакет."],
      ["context", "Упаковка", "Ничего не пролилось."],
    ],
  },
  cape: {
    sep: [
      ["complaint", "Выдача", "За готовым заказом пришлось обратиться дважды."],
      ["denial", "Выдача", "У стойки ждать не пришлось."],
      ["unclear", "Выдача", "Очередь была, а был ли готов заказ — неясно."],
    ],
    oct: [
      ["missing", null, ""],
      ["missing", null, ""],
      ["missing", null, ""],
    ],
  },
  ray: {
    sep: [
      ["complaint", "Выдача", "Готовый обед долго не отдавали."],
      ["context", "Упаковка", "Пакет удобный."],
      ["denial", "Выдача", "Заказ сразу передали в руки."],
      ["complaint", "Приготовление", "Долго пекли лепёшку."],
    ],
    oct: [
      ["complaint", "Выдача", "Коробка стояла готовой, но выдачу задержали."],
      ["complaint", "Выдача", "Пришлось ждать после сообщения о готовности."],
      ["context", "Упаковка", "Заказ аккуратно сложили."],
    ],
  },
};
export function selection(ids: readonly PlaceId[], period: Period) {
  const rows = ids.flatMap((place) =>
    snapshots[place][period].map(([stance, aspect, text], i) => ({
      id: `${place}-${period}-${i + 1}`,
      place,
      stance,
      aspect,
      text,
    })),
  );
  return { rows, ...summarize(rows) };
}
export const fraction = (value: ReturnType<typeof selection>) =>
  value.percent === null
    ? "Нет данных"
    : `${new Intl.NumberFormat("ru", { maximumFractionDigits: 1 }).format(value.percent)}%`;
// Same projection and viewBox as the local Natural Earth illustration.
export function project({ lon, lat }: { lon: number; lat: number }) {
  return [(lon - 75) * Math.cos((0.58 * lat * Math.PI) / 180) * 3.05 + 280, (80 - lat) * 3.05 + 10];
}
