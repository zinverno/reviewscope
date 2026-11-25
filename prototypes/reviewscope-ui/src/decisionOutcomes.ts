export const outcomes = {
	complaint: "Жалоба",
	mixed: "Смешанная позиция с явной жалобой",
	denial: "Отрицание проблемы",
	positive: "Положительная позиция",
	neutral: "Нейтральное упоминание",
	unclear: "Позиция неясна",
	not_mentioned: "Аспект не упомянут",
	topic_uncertain: "Неясно, относится ли текст к аспекту",
	excluded_blank: "Нет текста",
};
export type Outcome = keyof typeof outcomes;
