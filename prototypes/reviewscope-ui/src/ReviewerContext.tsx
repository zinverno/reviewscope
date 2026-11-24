import { useEffect, useState } from "react";
import {
	overviewAddress,
	PublicReviewDetail,
	UnknownSelection,
} from "./OverviewSources";
import {
	publicOrigin,
	reviewerDataset,
	reviewerHref,
	reviewerPlaces,
} from "./reviewerNavigation";

import {
	type Ref,
	type Place,
	type History,
	type Profile,
	type State,
	reviewerPageSize as size,
	readReviewerContext,
} from "./reviewerData";
function Pages({
	page,
	total,
	address,
}: {
	page: number;
	total: number;
	address: (n: number) => string;
}) {
	const pages = Math.max(1, Math.ceil(total / size));
	return (
		<nav aria-label="Страницы">
			<p>
				Страница {page} из {pages}.{" "}
				{page > 1 && <a href={address(page - 1)}>Предыдущая страница</a>}{" "}
				{page < pages && <a href={address(page + 1)}>Следующая страница</a>}
			</p>
		</nav>
	);
}

function CoordinateScheme({ places }: { places: Place[] }) {
	const points = places.flatMap((p) =>
		p.coordinates && p.geo === "valid"
			? [
					{
						id: p.id,
						name: p.name,
						lat: p.coordinates[0],
						lon: p.coordinates[1],
					},
				]
			: [],
	);
	if (!points.length) return <p>Подтверждённых координат для схемы нет.</p>;
	const latitudes = points.map((p) => p.lat),
		longitudes = points.map((p) => p.lon);
	const south = Math.min(...latitudes),
		north = Math.max(...latitudes),
		west = Math.min(...longitudes),
		east = Math.max(...longitudes);
	const scale = (v: number, min: number, max: number) =>
		min === max ? 0.5 : (v - min) / (max - min);
	return (
		<figure className="reviewer-plot">
			<figcaption>
				Координатная схема без подложки · точек {points.length}. По горизонтали
				долгота, по вертикали широта. Совпадающие координаты наложены.
			</figcaption>
			<svg
				viewBox="0 0 360 194"
				role="img"
				aria-label="Координаты точек из таблицы"
			>
				<path d="M50 12V159H340" fill="none" stroke="currentColor" />
				<text x="1" y="21">
					{north.toFixed(2)}°
				</text>
				<text x="1" y="154">
					{south.toFixed(2)}°
				</text>
				<text x="48" y="182">
					{west.toFixed(2)}°
				</text>
				<text x="345" y="182" textAnchor="end">
					{east.toFixed(2)}°
				</text>
				{points.map((p) => (
					<circle
						key={p.id}
						cx={57 + scale(p.lon, west, east) * 277}
						cy={151 - scale(p.lat, south, north) * 132}
						r="4"
					>
						<title>
							{p.name} · {p.lat}, {p.lon}
						</title>
					</circle>
				))}
			</svg>
		</figure>
	);
}

function MetricDetails({ profile, h }: { profile: Profile; h: History }) {
	const c = h.coverage;
	const metrics: [string, string, number][] = [
		["review_count", "Отзывы, шт.", c.total],
		[
			"active_period_days",
			"Дни между крайними датами, минимум 1 по legacy",
			c.date,
		],
		["category_count", "Различные категории, шт.", c.category],
		["city_count", "Различные города, шт.", c.city],
		["rating_mean", "Средняя оценка, ★", c.rating],
		[
			"rating_std",
			"Стандартное отклонение оценок, ★ (по совокупности)",
			c.rating,
		],
		["rating_entropy", "Нормированная энтропия оценок (0–1)", c.rating],
		["review_diversity", "Концентрация оценок (1 − энтропия)", c.rating],
		["review_consistency", "Согласованность оценок (0–1)", c.rating],
		["text_length_mean", "Средняя длина текста, символы", c.total],
		["specificity_mean", "Средняя конкретность, баллы из 100", c.total],
		["duplicate_ratio", "Доля отзывов с сигналом повтора (0–1)", c.total],
		["template_ratio", "Доля отзывов с сигналом шаблонности (0–1)", c.total],
	];
	const components: Record<string, string> = {
		count: "отзывов в категории",
		share: "доля категории с поправкой на глубину истории",
		active: "нормированный период в категории",
		diversity: "различные точки в категории",
		reviews: "отзывов в выбранной локальной группе",
		city_reviews: "отзывов в городе",
		region_reviews: "отзывов в остальной части региона",
		places: "различных точек",
		duration: "нормированный локальный компонент",
		category_experience: "опыт в категории",
		specificity_history: "конкретность текстов",
		review_consistency: "согласованность оценок",
		local_context: "локальный контекст",
		history_depth: "глубина истории",
	};
	const labels = {
		category: "Опыт в категории",
		local: "Индекс локального контекста",
		relevance: "Соответствие контексту точки",
	};
	return (
		<details>
			<summary>Метрики, метод и основания</summary>
			<p>
				Метрики рассчитаны на полной истории авторов выбранной точки в этом
				источнике. Для длины и конкретности legacy включает пустой текст в
				знаменатель. Текстовые доли зависят от состава выборки. Для
				согласованности и составного индекса требуются как минимум две известные
				оценки.
			</p>
			<table>
				<thead>
					<tr>
						<th>Метрика</th>
						<th>Значение</th>
						<th>Отзывов в расчёте</th>
					</tr>
				</thead>
				<tbody>
					{metrics.map(([key, label, n]) => (
						<tr key={key}>
							<th scope="row">{label}</th>
							<td>{profile.metrics[key] ?? "Н/Д · недостаточно данных"}</td>
							<td>
								{n} из {c.total}
							</td>
						</tr>
					))}
				</tbody>
			</table>
			{(["category", "local", "relevance"] as const).map((k) => {
				const score = profile.scores[k];
				return (
					<section key={k}>
						<h3>{labels[k]}</h3>
						<p>
							{score.value === null
								? "Н/Д · недостаточно данных"
								: score.value + " из 100 · эвристический индекс"}
							{score.value !== null &&
								" · уровень оснований " +
									({ low: "низкий", medium: "средний", high: "высокий" }[
										score.band.toLowerCase()
									] ?? score.band)}
						</p>
						<p>
							Основания — {score.signals.join(" · ") || "не выделены"}.
							Контрсигналы —{" "}
							{score.counter_signals.join(" · ") || "не выделены"}.
						</p>
						{score.value !== null && (
							<p>
								Компоненты метода ·{" "}
								{Object.entries(score.components)
									.map(([key, v]) => (components[key] ?? key) + " = " + v)
									.join(" · ") || "нет"}
							</p>
						)}
					</section>
				);
			})}
		</details>
	);
}

export function PublicReviewers({ search }: { search: string }) {
	const [data, set] = useState<State | null>();
	useEffect(() => {
		let live = true;
		readReviewerContext(search)
			.then((v) => {
				if (live) set(v);
			})
			.catch(() => {
				if (live) set(null);
			});
		return () => {
			live = false;
		};
	}, [search]);
	if (data === undefined)
		return <p role="status">Загружаем авторский контекст…</p>;
	if (!data) return <UnknownSelection />;
	const q = new URLSearchParams(search),
		place = data.index.place;
	const from = publicOrigin(q.get("from") ?? "", place),
		listPage = Number(q.get("authorsPage") ?? 1);
	const href = (updates: Record<string, string>) =>
		reviewerHref(place, { ...Object.fromEntries(q), ...updates });
	const list = reviewerHref(place, { authorsPage: String(listPage), from });
	const { author, history: h, profile, primary, member, index } = data;
	if (primary && member)
		return (
			<PublicReviewDetail
				r={primary}
				id={member.id}
				place={member.place}
				heading="История отзывов"
				result={
					reviewerPlaces.find((p) => p.id === member.place)!.source_result
				}
				back={href({ evidence: "", evidenceVersion: "", evidencePlace: "" })}
				backLabel="Вернуться к истории отзывов"
			/>
		);
	const placeName = (id: string) =>
		h?.places.find((p) => p.id === id)?.name ?? id;
	const evidence = (target: string, r: Ref) =>
		href({ evidence: r.id, evidenceVersion: r.version, evidencePlace: target });
	const back = from ? "/cabinet/?" + from : overviewAddress(place, "one");
	if (!q.has("review"))
		return (
			<section>
				<a href={back}>Вернуться к исходному экрану</a>
				<h2>Авторы этой точки</h2>
				<p>
					Публичный набор · точка {place}. Из {index.total} отзывов с известным
					автором {index.known}, без установленного автора {index.missing}.
					Различных авторов {index.authors.length}.
				</p>
				{index.authors.length ? (
					<ol start={(listPage - 1) * size + 1}>
						{index.authors
							.slice((listPage - 1) * size, listPage * size)
							.map((a) => (
								<li key={JSON.stringify([a.source, a.id])}>
									<a
										href={href({
											review: a.anchor.id,
											version: a.anchor.version,
										})}
									>
										{a.source} · {a.id}
									</a>{" "}
									· отзывов в точке {a.count}, в предоставленной истории{" "}
									{a.total}
								</li>
							))}
					</ol>
				) : (
					<p>Известных авторов в этой точке нет.</p>
				)}
				<Pages
					page={listPage}
					total={index.authors.length}
					address={(n) => href({ authorsPage: String(n) })}
				/>
			</section>
		);
	if (!author || !h || !profile)
		return (
			<section>
				<a href={back}>Вернуться к исходному экрану</a>
				<p>
					NO_REVIEWER_ID · идентификатор автора или его источник неизвестен.
					История недоступна.
				</p>
			</section>
		);
	const c = h.coverage,
		page = Number(q.get("page") ?? 1);
	const coverage: [string, number][] = [
		["Даты", c.date],
		["Оценки", c.rating],
		["Тексты", c.text],
		["Категории", c.category],
		["Города", c.city],
		["Проверенные координаты", c.coordinates],
	];
	const reasons: Record<string, string> = {
		missing: "Координаты отсутствуют",
		invalid: "Недопустимая пара координат",
		conflict: "Конфликт координат точки",
	};
	return (
		<section className="reviewer-context">
			<nav>
				<a href={list}>К списку авторов точки</a>
				{from && (
					<>
						{" "}
						· <a href={back}>Вернуться к исходному экрану</a>
					</>
				)}
			</nav>
			<h2>История отзывов</h2>
			<p>
				В предоставленных данных найдено {c.total} отзывов · источник{" "}
				{author.source} · синтетический автор {author.id}. В выбранной точке{" "}
				{placeName(place)} — {author.count}.
			</p>
			{c.total === 1 && (
				<p>
					В наборе один отзыв этого автора. Истории для сравнения периодов
					недостаточно.
				</p>
			)}
			<p>
				Наблюдаемые даты ·{" "}
				{h.first && h.last ? h.first + " — " + h.last : "неизвестны"}. Отзывы
				описывают только предоставленный набор. Координаты относятся к местам
				отзывов.
			</p>
			<p>
				{coverage
					.map(([label, n]) => label + " " + n + "/" + c.total)
					.join(" · ")}
				.
			</p>
			<p>
				Категории ·{" "}
				{h.categories.map(([k, n]) => k + " (" + n + ")").join(", ") || "Н/Д"}.
				Без категории {c.total - c.category}.
			</p>
			<p>
				Города ·{" "}
				{h.cities.map(([k, n]) => k + " (" + n + ")").join(", ") || "Н/Д"}. Без
				города {c.total - c.city}.
			</p>
			<MetricDetails profile={profile} h={h} />
			<h3>Места, о которых оставлены отзывы</h3>
			<CoordinateScheme places={h.places} />
			<table>
				<thead>
					<tr>
						<th>Точка и категория</th>
						<th>Отзывов</th>
						<th>Широта, долгота</th>
					</tr>
				</thead>
				<tbody>
					{h.places.map((p) => (
						<tr key={p.id}>
							<th scope="row">
								<a href={evidence(p.id, p.anchor)}>{p.name ?? p.id}</a>
								<br />
								{p.category ?? "Категория неизвестна"}
							</th>
							<td>{p.count}</td>
							<td>{p.coordinates?.join(", ") ?? reasons[p.geo] ?? "Н/Д"}</td>
						</tr>
					))}
				</tbody>
			</table>
			<section aria-label="История отзывов">
				<h3>Исходные записи</h3>
				{h.reviews.length ? (
					<ol start={(page - 1) * size + 1}>
						{h.reviews.slice((page - 1) * size, page * size).map((r) => (
							<li key={r.version}>
								<a href={evidence(r.place, r)}>Исходный отзыв {r.id}</a> ·{" "}
								{placeName(r.place)} · {r.date ?? "Дата неизвестна"}
							</li>
						))}
					</ol>
				) : (
					<p>В выбранной истории нет отзывов.</p>
				)}
				<Pages
					page={page}
					total={h.reviews.length}
					address={(n) => href({ page: String(n) })}
				/>
			</section>
			<details>
				<summary>Состав результата</summary>
				<p>
					Набор {reviewerDataset} · результат {index.run} · точка {place} ·
					исходная версия {q.get("version")}.
				</p>
			</details>
		</section>
	);
}
