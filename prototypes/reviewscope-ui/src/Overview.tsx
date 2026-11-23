import { useEffect, useState } from "react";
import {
	OverviewSources,
	UnknownSelection,
	overviewAddress,
} from "./OverviewSources";
import demo from "./overview.synthetic.json";
import { evidenceFields } from "./ReviewEvidence";

type Pair = { numerator: number; denominator: number };
type Counts = Record<string, Pair>;
type Projection = {
	id: string;
	status: string;
	reason: string | null;
	sha256: string | null;
	metrics: {
		counts: Counts;
		eligibility: Counts;
		rating_distribution: Counts | null;
	} | null;
	context: {
		source_id: string;
		batch_id: string;
		coverage: string | null;
		covered_from: string | null;
		covered_until: string | null;
		delivery_exclusions: Counts;
		metric_exclusions: Counts;
	} | null;
};
type Props = {
	workspace: string;
	snapshot: string;
	search: string;
	canRead: boolean;
	api: <T>(path: string) => Promise<T>;
	link: (changes: Record<string, string>) => string;
};
const labels: Record<string, string> = {
	observations: "Принятые отзывы",
	quarantined: "В карантине",
	confirmed_duplicates: "Точные повторы позиций",
	usable_text: "Пригодный текст",
	known_rating: "Известная оценка",
	comparable_date: "Сопоставимая дата",
	unknown_author: "Автор неизвестен",
	missing_reviewer: "Отсутствует автор",
	missing_text: "Отсутствует текст",
	conflicting_id: "Конфликт идентификаторов",
	missing_rating: "Отсутствует оценка",
	date_missing: "Отсутствует дата",
	missing_review_id: "Нет внешнего ID",
	invalid_field_type: "Недопустимый тип поля",
	invalid_json_item: "Элемент JSON не является записью",
};
function fraction(p: Pair) {
	return p.denominator
		? `${p.numerator} из ${p.denominator} · ${((100 * p.numerator) / p.denominator).toLocaleString("ru", { maximumFractionDigits: 1 })}%`
		: "Н/Д · 0 из 0, выборка пуста";
}
function Measures({
	title,
	values,
	stars = false,
}: {
	title: string;
	values: Counts;
	stars?: boolean;
}) {
	return (
		<>
			<h3>{title}</h3>
			{Object.keys(values).length ? (
				<table>
					<thead>
						<tr>
							<th>Показатель</th>
							<th>Доля</th>
						</tr>
					</thead>
					<tbody>
						{Object.entries(values).map(([k, v]) => (
							<tr key={k}>
								<th scope="row">{stars ? k + " ★" : (labels[k] ?? k)}</th>
								<td>{fraction(v)}</td>
							</tr>
						))}
					</tbody>
				</table>
			) : (
				<p>Проверка не зарегистрировала причин в этом классе.</p>
			)}
		</>
	);
}
export function SnapshotOverview({
	workspace,
	snapshot,
	search,
	canRead,
	api,
	link,
}: Props) {
	const params = new URLSearchParams(search);
	const source = params.get("source"),
		run = params.get("run");
	const selector = new URLSearchParams();
	if (source) selector.set("source_id", source);
	if (run) selector.set("run_id", run);
	const filters = new URLSearchParams();
	for (const k of evidenceFields) {
		const v = params.get(k);
		if (v) filters.set(k, v);
	}
	const base = `/workspaces/${workspace}/snapshots/${snapshot}`;
	const summary = base + "/summary-metrics?" + selector;
	const reviewQuery = filters.toString();
	const [result, setResult] = useState<Projection | null>(),
		[total, setTotal] = useState<number | null>(null);
	useEffect(() => {
		let live = true;
		api<Projection>(summary)
			.then((v) => {
				if (live) setResult(v);
			})
			.catch(() => {
				if (live) setResult(null);
			});
		if (canRead && reviewQuery)
			api<{ total: number }>(base + "/reviews?limit=1&" + reviewQuery)
				.then((v) => {
					if (live) setTotal(v.total);
				})
				.catch(() => {
					if (live) setTotal(null);
				});
		return () => {
			live = false;
		};
	}, [api, base, summary, reviewQuery, canRead]);
	const m = result?.metrics,
		ctx = result?.context;
	return (
		<section>
			<a href={link({ view: "" })}>Статус проверки целостности</a>
			<h2>Канонический снимок</h2>
			{result === undefined ? (
				<p role="status">Загрузка сводки…</p>
			) : !result ? (
				<p role="status">Сводка для выбранной области недоступна.</p>
			) : (
				<>
					{result.status !== "succeeded" ? (
						<>
							<p role="status">
								Выбранная проверка не опубликовала результат. {result.status}{" "}
								{result.reason}
							</p>
							<a href={link({ run: "" })}>
								Посмотреть последнюю успешную сводку
							</a>
						</>
					) : (
						<>
							{ctx && (
								<p>
									Источник {ctx.source_id} · Покрытие{" "}
									{
										{
											partial: "частичное",
											full: "полное по паспорту",
											unknown: "неизвестно",
										}[ctx.coverage ?? "unknown"]
									}
									. Период по паспорту:{" "}
									{ctx.covered_from ?? "начало неизвестно"} —{" "}
									{ctx.covered_until ?? "конец неизвестен"}.
								</p>
							)}
							{!ctx && (
								<p>
									Контекст поставки отсутствует в этой версии проекции. Период и
									покрытие неизвестны.
								</p>
							)}
							{reviewQuery ? (
								<>
									<p>
										Отзывов по фильтру: {total ?? "Н/Д"}. Детальные показатели
										для этой выборки не рассчитаны (FILTER_NOT_COMPUTED).
									</p>
									<a
										href={link(
											Object.fromEntries(evidenceFields.map((k) => [k, ""])),
										)}
									>
										Сбросить фильтры
									</a>
								</>
							) : m ? (
								<>
									<p className="overview-count">
										<strong>{m.counts.observations.numerator}</strong> принятых
										отзывов из {m.counts.observations.denominator} позиций
										поставки
									</p>
									<Measures title="Позиции поставки" values={m.counts} />
									<Measures
										title="Качество принятых записей"
										values={m.eligibility}
									/>
									{m.rating_distribution ? (
										<Measures
											title="Звёзды среди известных оценок"
											values={m.rating_distribution}
											stars
										/>
									) : (
										<p>
											Распределение звёзд недоступно: шкала 1–5 не подтверждена.
										</p>
									)}
									{ctx && (
										<details>
											<summary>Причины исключений и предупреждения</summary>
											<Measures
												title="По всем позициям"
												values={ctx.delivery_exclusions}
											/>
											<Measures
												title="По принятым отзывам"
												values={ctx.metric_exclusions}
											/>
										</details>
									)}
									<nav aria-label="Фильтры обзора">
										<a href={link({ has_text: "true" })}>Только с текстом</a> ·{" "}
										<a href={link({ rating: "5" })}>Только 5 звёзд</a>
									</nav>
								</>
							) : (
								<p>Числовая проекция для этой проверки отсутствует.</p>
							)}
							<details>
								<summary>Происхождение результата</summary>
								<p>snapshot-summary-1 · accepted_in_delivery</p>
								<p>
									Снимок {snapshot} · запуск {result.id} · SHA-256{" "}
									{result.sha256}
								</p>
							</details>
						</>
					)}
				</>
			)}
			{canRead && (
				<p>
					<a href={link({ view: "reviews" })}>Открыть отзывы этой выборки</a>
				</p>
			)}
			<h3>Темы и динамика</h3>
			<p>
				ANALYSIS_NOT_AVAILABLE. Worker проверяет целостность и пригодность. Для
				сравнения периодов и выделения тем в этом снимке нет аналитического
				результата.
			</p>
			<a href="/cabinet/?view=legacy">
				Открыть отдельный обзор публичной синтетики
			</a>
		</section>
	);
}
export function LegacyOverview({ search }: { search: string }) {
	const q = new URLSearchParams(search),
		id = q.get("place") ?? "p1";
	const valid = [
		"view",
		"place",
		"ranking",
		"dataset",
		"result",
		"group",
		"item",
		"page",
		"review",
		"version",
	];
	const scoped = [
		"dataset",
		"result",
		"group",
		"item",
		"page",
		"review",
		"version",
	].some((k) => q.has(k));
	const selection = ["group", "item", "page", "review", "version"].some((k) =>
		q.has(k),
	);
	const ranking = q.get("ranking") ?? "one";
	if (
		[...q.keys()].some((k) => !valid.includes(k) || q.getAll(k).length !== 1) ||
		!["one", "five"].includes(ranking) ||
		(scoped &&
			(q.get("dataset") !== demo.snapshot || q.get("result") !== demo.run)) ||
		(selection && (!q.has("place") || !q.get("group") || !q.get("item")))
	)
		return <UnknownSelection />;
	const metric = ranking === "five" ? "five_star_share" : "one_star_share";
	const rankLabel = ranking === "five" ? "Доля 5 ★" : "Доля 1 ★";
	const address = overviewAddress;
	const place = demo.places.find((p) => p.id === id);
	if (!place) return <UnknownSelection />;
	if (selection)
		return (
			<OverviewSources
				key={id}
				place={id}
				ranking={ranking}
				query={q}
				digest={place.source_result}
			/>
		);
	function terms(group: string, values: (string | number)[][]) {
		return values.length
			? values.map(([term], i) => (
					<span key={term}>
						{i > 0 && " · "}
						<a
							href={address(id, ranking, {
								group,
								item: String(term),
								page: "1",
							})}
						>
							{term}
						</a>
					</span>
				))
			: "Нет выделенных терминов";
	}
	return (
		<section>
			<a href="/cabinet/">Вернуться в кабинет</a>
			<h2>Публичная синтетика · DuckDB</h2>
			<p>
				Упакованное демо: {demo.places.length} точек. Каждая подборка относится
				к записям из этого файла.
			</p>
			<label>
				Точка демо
				<select
					value={id}
					onChange={(e) => location.assign(address(e.target.value, ranking))}
				>
					{demo.places.map((p) => (
						<option key={p.id} value={p.id}>
							{p.name}
						</option>
					))}
				</select>
			</label>
			{place ? (
				<>
					<h3>{place.name}</h3>
					<p className="overview-count">
						<strong>{place.count}</strong> отзывов · {place.rated} с оценкой
					</p>
					<p>
						Среднее по исходным оценкам:{" "}
						{place.raw_rating?.toLocaleString("ru") ?? "Н/Д"}. Официальный
						рейтинг площадки неизвестен.
					</p>
					<h3>Темы</h3>
					{place.topics.length ? (
						<ul>
							{place.topics.map((t) => (
								<li key={t.id}>
									<a
										href={address(id, ranking, {
											group: "topic",
											item: String(t.id),
											page: "1",
										})}
									>
										Тема {t.id} · {t.count} отзывов
									</a>{" "}
									— {t.phrases.join(" · ") || "Без выделенных фраз"}
								</li>
							))}
						</ul>
					) : (
						<p>Ядро не выделило тем.</p>
					)}
					<h3>Ключевые слова</h3>
					<p>{terms("keywords", place.keywords)}</p>
					<h3>Новые термины</h3>
					<p>{terms("emerging", place.emerging)}</p>
					<details>
						<summary>Тональность по оценке и ключевые слова по звёздам</summary>
						<p>
							Положительная группа (4–5 ★): {terms("positive", place.positive)}
						</p>
						<p>
							Отрицательная группа (1–2 ★): {terms("negative", place.negative)}
						</p>
						<p>
							Группы определяются оценкой автора. У отзывов с 3 ★ и без оценки
							здесь нет тональной группы.
						</p>
						{Object.entries(place.by_rating).map(([k, v]) => (
							<p key={k}>
								{k} ★ · {terms("star-" + k, v)}
							</p>
						))}
					</details>
				</>
			) : (
				<p role="alert">Точка отсутствует в публичном демо.</p>
			)}
			<h3>Discover · все точки демо</h3>
			<label>
				Ранжировать по
				<select
					value={ranking}
					onChange={(e) => location.assign(address(id, e.target.value))}
				>
					<option value="one">Доля 1 ★</option>
					<option value="five">Доля 5 ★</option>
				</select>
			</label>
			<table>
				<thead>
					<tr>
						<th>Точка</th>
						<th>Отзывов</th>
						<th>{rankLabel}</th>
					</tr>
				</thead>
				<tbody>
					{[...demo.places]
						.sort(
							(a, b) =>
								(b[metric] ?? -1) - (a[metric] ?? -1) ||
								a.id.localeCompare(b.id),
						)
						.map((p) => (
							<tr key={p.id}>
								<td>
									<a href={address(p.id, ranking)}>{p.name}</a>
								</td>
								<td>{p.count}</td>
								<td>
									{p[metric] == null
										? "Н/Д"
										: p[metric].toLocaleString("ru", {
												maximumFractionDigits: 1,
											}) + "%"}
								</td>
							</tr>
						))}
				</tbody>
			</table>
			<details>
				<summary>Метод и происхождение</summary>
				<p>
					Темы и термины рассчитаны офлайн прежним ядром по сохранённым
					векторам. Новые термины сравнивают последние 30 дней с предшествующими
					90 в данных точки. При равных весах порядок терминов алфавитный.
				</p>
				<p>
					{demo.contract} · {demo.producer}
				</p>
				<p>
					SHA-256 снимка {demo.snapshot} · результат {demo.run}
				</p>
			</details>
		</section>
	);
}
