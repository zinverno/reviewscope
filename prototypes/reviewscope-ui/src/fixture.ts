export type Period = "sep" | "oct";
export type Stance = "complaint" | "denial" | "unclear" | "context" | "missing";
export type Aspect = "Выдача" | "Приготовление" | "Упаковка";
export type Relation = "Одна проблема" | "Общая тема" | "Повтор текста";
export interface Review {
  review_id: string;
  period: Period;
  date: string;
  rating: number | null;
  aspect: Aspect | null;
  stance: Stance;
  text: string;
  phrase: string;
}
export interface Edge {
  a: string;
  b: string;
  type: Relation;
  reason: string;
}
export const periods: Record<Period, string> = { sep: "Сентябрь", oct: "Октябрь" };
export const labels: Record<Stance, string> = {
  complaint: "Жалоба",
  denial: "Отрицание",
  unclear: "Неоднозначно",
  context: "Контекст",
  missing: "Нет текста",
};
export const issue = "Долгая выдача готового заказа";
export const reviews: Review[] = [
  {
    review_id: "01",
    period: "sep",
    date: "2026-09-03",
    rating: 2,
    aspect: "Выдача",
    stance: "complaint",
    text: "Заказ давно был готов, но у стойки ждали ещё сорок минут.",
    phrase: "у стойки ждали ещё сорок минут",
  },
  {
    review_id: "02",
    period: "sep",
    date: "2026-09-08",
    rating: 3,
    aspect: "Выдача",
    stance: "complaint",
    text: "Пакет простоял за стойкой полчаса, прежде чем его отдали.",
    phrase: "Пакет простоял за стойкой полчаса",
  },
  {
    review_id: "03",
    period: "sep",
    date: "2026-09-12",
    rating: 5,
    aspect: "Выдача",
    stance: "denial",
    text: "Думал, придётся ждать, но готовый заказ отдали сразу.",
    phrase: "готовый заказ отдали сразу",
  },
  {
    review_id: "04",
    period: "sep",
    date: "2026-09-15",
    rating: 3,
    aspect: "Приготовление",
    stance: "complaint",
    text: "Пиццу долго готовили, но выдали сразу.",
    phrase: "долго готовили",
  },
  {
    review_id: "05",
    period: "sep",
    date: "2026-09-23",
    rating: 2,
    aspect: "Выдача",
    stance: "complaint",
    text: "Пакет простоял за стойкой полчаса, прежде чем его отдали.",
    phrase: "Пакет простоял за стойкой полчаса",
  },
  {
    review_id: "06",
    period: "sep",
    date: "2026-09-28",
    rating: null,
    aspect: "Выдача",
    stance: "unclear",
    text: "Ждал у стойки. Не знаю, был ли уже собран заказ.",
    phrase: "Не знаю, был ли уже собран заказ",
  },
  {
    review_id: "07",
    period: "oct",
    date: "2026-10-02",
    rating: 3,
    aspect: "Выдача",
    stance: "complaint",
    text: "Готовый пакет лежал на полке. Пришлось дважды просить его выдать.",
    phrase: "Пришлось дважды просить его выдать",
  },
  {
    review_id: "08",
    period: "oct",
    date: "2026-10-07",
    rating: 5,
    aspect: "Выдача",
    stance: "denial",
    text: "На табло загорелся номер. Ждать выдачи не пришлось.",
    phrase: "Ждать выдачи не пришлось",
  },
  {
    review_id: "09",
    period: "oct",
    date: "2026-10-11",
    rating: 5,
    aspect: "Упаковка",
    stance: "context",
    text: "Упаковали всё правильно, сервис понравился.",
    phrase: "Упаковали всё правильно",
  },
  {
    review_id: "10",
    period: "oct",
    date: "2026-10-16",
    rating: 2,
    aspect: "Приготовление",
    stance: "complaint",
    text: "Суп ещё варили, когда я пришёл за заказом.",
    phrase: "Суп ещё варили",
  },
  {
    review_id: "11",
    period: "oct",
    date: "2026-10-20",
    rating: 3,
    aspect: "Выдача",
    stance: "unclear",
    text: "В очереди стояли долго. На каком этапе задержали — не понял.",
    phrase: "На каком этапе задержали — не понял",
  },
  {
    review_id: "12",
    period: "oct",
    date: "2026-10-24",
    rating: 4,
    aspect: null,
    stance: "missing",
    text: "",
    phrase: "",
  },
];
export const edges: Edge[] = [
  {
    a: "01",
    b: "02",
    type: "Одна проблема",
    reason:
      "Оба автора описывают ожидание после того, как заказ был готов. Длительность и слова разные, этап обслуживания совпадает.",
  },
  {
    a: "02",
    b: "03",
    type: "Общая тема",
    reason:
      "Оба отзыва о выдаче готового заказа. В отзыве 03 ожидание явно отрицается, поэтому в число жалоб он не входит.",
  },
  {
    a: "02",
    b: "05",
    type: "Повтор текста",
    reason:
      "Тексты совпадают дословно. Это две отдельные записи. По совпадению нельзя установить авторство или намерение.",
  },
  {
    a: "03",
    b: "04",
    type: "Общая тема",
    reason: "Авторы упоминают быструю выдачу. В отзыве 04 жалоба относится к приготовлению пиццы.",
  },
  {
    a: "07",
    b: "08",
    type: "Общая тема",
    reason:
      "В обоих фрагментах заказ уже готов. Автор 07 ждал его выдачи, а автор 08 прямо отрицает задержку.",
  },
  {
    a: "07",
    b: "11",
    type: "Общая тема",
    reason:
      "Оба автора упоминают ожидание. Только в отзыве 07 ясно, что задержали уже готовый заказ.",
  },
];
