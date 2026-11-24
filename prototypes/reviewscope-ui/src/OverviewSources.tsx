import { useEffect, useState } from "react";
import demo from "./overview.synthetic.json";

export const sources = import.meta.glob<{ default: Sources }>(
	"./overview-sources/*.json",
);
const pageSize = 12;
type Review = {
	version: string;
	text: string | null;
	rating: number | null;
	date: string | null;
	source: string | null;
	source_url: string | null;
	reviewer_id: string;
	reviewer_name: string | null;
};
type Sources = {
	place: string;
	snapshot: string;
	result: string;
	topics: Record<string, string[]>;
	terms: Record<string, Record<string, string[]>>;
	reviews: Record<string, Review>;
};

export function overviewAddress(
	place: string,
	ranking: string,
	selected: Record<string, string> = {},
) {
	return (
		"/cabinet/?" +
		new URLSearchParams({
			view: "legacy",
			place,
			ranking,
			dataset: demo.snapshot,
			result: demo.run,
			...selected,
		})
	);
}

export function UnknownSelection() {
	return (
		<p role="alert">
			Область или выборка публичного демо недоступна.{" "}
			<a href="/cabinet/?view=legacy">Открыть публичную синтетику</a>
		</p>
	);
}

export function OverviewSources({
	place,
	ranking,
	query,
	digest,
}: {
	place: string;
	ranking: string;
	query: URLSearchParams;
	digest: string;
}) {
	const [data, setData] = useState<Sources | null>();
	useEffect(() => {
		let current = true;
		sources["./overview-sources/" + place + ".json"]()
			.then(({ default: doc }) => {
				if (current) setData(doc);
			})
			.catch(() => {
				if (current) setData(null);
			});
		return () => {
			current = false;
		};
	}, [place]);
	if (data === undefined) return <p role="status">Загрузка состава выборки…</p>;
	if (
		!data ||
		data.place !== place ||
		data.snapshot !== demo.snapshot ||
		data.result !== digest
	)
		return <UnknownSelection />;
	const group = query.get("group") ?? "",
		item = query.get("item") ?? "";
	const ids =
		group === "topic"
			? Object.hasOwn(data.topics, item) && data.topics[item]
			: Object.hasOwn(data.terms, group) &&
				Object.hasOwn(data.terms[group], item) &&
				data.terms[group][item];
	const pageText = query.get("page") ?? "1",
		page = Number(pageText);
	if (
		!ids ||
		!/^[1-9]\d*$/.test(pageText) ||
		page > Math.max(1, Math.ceil(ids.length / pageSize))
	)
		return <UnknownSelection />;
	const visible = ids.slice((page - 1) * pageSize, page * pageSize);
	const id = query.get("review");
	const address = (n: number, more: Record<string, string> = {}) =>
		overviewAddress(place, ranking, { group, item, page: String(n), ...more });
	const heading = group === "topic" ? "Тема " + item : "Термин «" + item + "»";
	if (query.has("review") || query.has("version")) {
		if (!id || !visible.includes(id) || !Object.hasOwn(data.reviews, id))
			return <UnknownSelection />;
		const r = data.reviews[id];
		if (!r.version || query.get("version") !== r.version)
			return <UnknownSelection />;
		return (
			<PublicReviewDetail
				r={r}
				id={id}
				place={place}
				heading={heading}
				result={demo.run}
				back={address(page)}
				backLabel={`Вернуться к составу · страница ${page}`}
			/>
		);
	}
	const cohort =
		group === "emerging"
			? "Отзывы за последние 30 дней относительно последней даты точки."
			: group === "positive"
				? "Выборка оценок 4–5 ★."
				: group === "negative"
					? "Выборка оценок 1–2 ★."
					: group.startsWith("star-")
						? "Выборка с оценкой " + group.slice(5) + " ★."
						: "Выборка всей точки.";
	return (
		<section>
			<a href={overviewAddress(place, ranking)}>Вернуться в обзор точки</a>
			<h2>{heading}</h2>
			<p>
				{group === "topic"
					? "Состав темы по результату кластеризации."
					: cohort +
						" Показаны тексты, содержащие этот термин по правилам вычислителя."}
			</p>
			<p>
				Отзывов в выборке: {ids.length}. Страница {page} из{" "}
				{Math.max(1, Math.ceil(ids.length / pageSize))}.
			</p>
			<ol start={(page - 1) * pageSize + 1}>
				{visible.map((rid) => (
					<li key={rid}>
						<a
							href={address(page, {
								review: rid,
								version: data.reviews[rid].version,
							})}
						>
							Отзыв {rid}
						</a>{" "}
						· {data.reviews[rid].rating ?? "оценка неизвестна"}
					</li>
				))}
			</ol>
			<nav aria-label="Страницы выборки">
				{page > 1 && <a href={address(page - 1)}>Предыдущая страница</a>}{" "}
				{page * pageSize < ids.length && (
					<a href={address(page + 1)}>Следующая страница</a>
				)}
			</nav>
		</section>
	);
}

export function PublicReviewDetail({
	r,
	id,
	place,
	heading,
	result,
	back,
	backLabel,
}: {
	r: Review;
	id: string;
	place: string;
	heading: string;
	result: string;
	back: string;
	backLabel: string;
}) {
	return (
		<article>
			<a href={back}>{backLabel}</a>
			<h2>Исходный отзыв {id}</h2>
			<p>
				{heading} · публичная синтетика · точка {place}
			</p>
			<p>
				Оценка: {r.rating ?? "неизвестно"} · Дата: {r.date ?? "неизвестно"}
			</p>
			<p className="source-text">{r.text ?? "Текст отсутствует"}</p>
			<p>Точный фрагмент не размечен.</p>
			<details>
				<summary>Версия и источник</summary>
				<p>Версия публичного отзыва {r.version}</p>
				<p>
					Набор {demo.snapshot} · результат {result}
				</p>
				<p>
					Источник: {r.source ?? "неизвестно"} · Исходный URL:{" "}
					{r.source_url ?? "отсутствует"}
				</p>
				<p>
					Автор в демо: {r.reviewer_name ?? "имя неизвестно"} · {r.reviewer_id}
				</p>
			</details>
		</article>
	);
}
