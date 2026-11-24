import { useEffect, useState } from "react";
import manifest from "./demo-signals/manifest.json";
import {
	PublicReviewDetail,
	sources,
	UnknownSelection,
	overviewAddress,
} from "./OverviewSources";
import overview from "./overview.synthetic.json";

type Numbers = Record<string, number>;
type Evidence = {
	heuristic_score: number;
	evidence_band: string;
	signals: string[];
	counter_signals: string[];
	components?: Numbers;
};
type Row = {
	id: string;
	version: string;
	rating: number | null;
	weight: Numbers;
	specificity: Evidence;
	relevance: Evidence;
	templated: Evidence;
	category: Evidence;
};
type Interval = {
	first: string;
	last: string;
	rated: number;
	excluded: number;
	ids: string[];
};
type Event = {
	date: string;
	score: number;
	severity: string;
	event: Interval;
	baseline: Interval;
	signals: string[];
	counter_signals: string[];
	observed?: number;
	expected?: number;
	mad?: number;
	z_score?: number;
	multiplier?: number | null;
	ratings?: Numbers;
	jsd?: number;
	dominant_shift?: string;
	baseline_dist?: Numbers;
	event_dist?: Numbers;
};
type Public = {
	dataset: string;
	place: string;
	run: string;
	as_of: string;
	reviews: Row[];
	coordinated: Evidence;
	coordinated_inputs: Numbers;
	bursts: Event[];
	rating_events: Event[];
	method: { config: { coordinated: { components: Numbers }; weight: Numbers } };
};
const files = import.meta.glob<{ default: Public }>("./demo-signals/p*.json");
const fmt = (n: number | null, precision = 4) =>
	n === null
		? "Н/Д"
		: n.toLocaleString("ru", { maximumFractionDigits: precision });
const weights: Record<string, string> = {
	specificity: "Специфичность",
	category_experience: "Опыт в категории",
	relevance: "Релевантность",
	recency: "Давность",
	quality: "Качество",
	neutral_quality: "Нейтральный порог",
	excess: "Превышение порога",
	duplicate_strength: "Сила повтора",
	templated_strength: "Сила шаблонности",
	coordinated_strength: "Сила координации",
	text_reuse: "Учтённое повторение текста",
	coordinated_residual: "Остаток координации",
	duplicate_charge: "Штраф за повтор",
	template_charge: "Штраф за шаблонность",
	residual_charge: "Штраф за остаток",
	penalty: "Сумма штрафов",
	before_clamp: "До ограничения",
	clamped: "После ограничения",
	weight: "Итоговый вес",
};
function Values({ items }: { items: Numbers }) {
	return (
		<dl>
			{Object.entries(items).map(([k, v]) => (
				<div key={k}>
					<dt>{weights[k] ?? k}</dt>
					<dd>{fmt(v, 8)}</dd>
				</div>
			))}
		</dl>
	);
}
function Reasons({
	signals,
	counter_signals,
}: {
	signals: string[];
	counter_signals: string[];
}) {
	return (
		<>
			<p>Основания · {signals.join(" · ") || "нет выделенных"}</p>
			<p>Контрсигналы · {counter_signals.join(" · ") || "нет выделенных"}</p>
		</>
	);
}
function Strength({ label, value }: { label: string; value: Evidence }) {
	return (
		<details>
			<summary>
				{label} · {fmt(value.heuristic_score)}/100 · {value.evidence_band}
			</summary>
			<Reasons {...value} />
			{value.components && <Values items={value.components} />}
		</details>
	);
}
export function signalHref(place: string, params: Record<string, string> = {}) {
	const entry = manifest.places.find((p) => p.id === place);
	return (
		"/cabinet/?" +
		new URLSearchParams({
			view: "public-signals",
			place,
			dataset: manifest.dataset,
			result: entry?.run ?? "",
			...params,
		})
	);
}
function RatingFilter({
	value,
	onSelect,
}: {
	value: string;
	onSelect: (v: string) => void;
}) {
	return (
		<label>
			Фильтр оценки
			<select value={value} onChange={(e) => onSelect(e.target.value)}>
				<option value="">Все оценки</option>
				{[1, 2, 3, 4, 5].map((n) => (
					<option key={n} value={n}>
						{n} ★
					</option>
				))}
				<option value="missing">Без оценки</option>
			</select>
		</label>
	);
}
function matches(r: number | null, v: string) {
	return !v || (r === null ? v === "missing" : String(r) === v);
}
const validRating = (v: string) =>
	["", "1", "2", "3", "4", "5", "missing"].includes(v);
const pageSize = 12;
function Pager({
	page,
	count,
	href,
}: {
	page: number;
	count: number;
	href: (p: number) => string;
}) {
	return (
		<nav aria-label="Страницы вкладов">
			<p>
				Страница {page} из {Math.max(1, Math.ceil(count / pageSize))}
			</p>
			{page > 1 && <a href={href(page - 1)}>Предыдущая страница</a>}{" "}
			{page * pageSize < count && (
				<a href={href(page + 1)}>Следующая страница</a>
			)}
		</nav>
	);
}
function validPage(page: string, count: number) {
	return (
		/^[1-9]\d*$/.test(page) &&
		Number(page) <= Math.max(1, Math.ceil(count / pageSize))
	);
}

export function PublicSignals({ search }: { search: string }) {
	const q = new URLSearchParams(search),
		place = q.get("place") ?? "",
		entry = manifest.places.find((p) => p.id === place);
	const [doc, set] = useState<Public | null>(),
		[source, setSource] = useState<
			Awaited<ReturnType<(typeof sources)[string]>>["default"] | null
		>();
	useEffect(() => {
		if (!entry) return;
		let live = true;
		Promise.all([
			files["./demo-signals/" + place + ".json"](),
			sources["./overview-sources/" + place + ".json"](),
		])
			.then(([a, b]) => {
				if (live) {
					set(a.default);
					setSource(b.default);
				}
			})
			.catch(() => {
				if (live) set(null);
			});
		return () => {
			live = false;
		};
	}, [entry, place]);
	if (
		!entry ||
		q.get("dataset") !== manifest.dataset ||
		q.get("result") !== entry.run ||
		[...q.keys()].some(
			(k) =>
				![
					"view",
					"place",
					"dataset",
					"result",
					"rating",
					"page",
					"cohort",
					"review",
					"version",
				].includes(k) || q.getAll(k).length !== 1,
		)
	)
		return <UnknownSelection />;
	if (doc === undefined) return <p role="status">Загрузка результата…</p>;
	if (
		!doc ||
		doc.run !== entry.run ||
		doc.place !== place ||
		doc.dataset !== manifest.dataset ||
		source?.place !== place ||
		source.result !== overview.places.find((p) => p.id === place)?.source_result
	)
		return <UnknownSelection />;
	const rating = q.get("rating") ?? "",
		cohort = q.get("cohort") ?? "";
	const events = [
		...doc.bursts.map((e) => ({ e, kind: "Всплеск", key: "burst-" + e.date })),
		...doc.rating_events.map((e) => ({
			e,
			kind: "Изменение оценок",
			key: "rating-" + e.date,
		})),
	];
	const groups = new Map(
		events.flatMap(
			({ e, key }) =>
				[
					["event-" + key, e.event.ids],
					["baseline-" + key, e.baseline.ids],
				] as const,
		),
	);
	if (!validRating(rating) || (cohort && !groups.has(cohort)))
		return <UnknownSelection />;
	const selected = doc.reviews.filter(
		(r) =>
			(!cohort || groups.get(cohort)?.includes(r.id)) &&
			matches(r.rating, rating),
	);
	const rated = selected.filter((r) => r.rating !== null);
	const den = rated.reduce((s, r) => s + r.weight.weight, 0);
	const raw = rated.length
		? rated.reduce((s, r) => s + (r.rating ?? 0), 0) / rated.length
		: null;
	const weighted = den
		? rated.reduce((s, r) => s + (r.rating ?? 0) * r.weight.weight, 0) / den
		: null;
	const pageText = q.get("page") ?? "1",
		page = Number(pageText);
	if (!validPage(pageText, selected.length)) return <UnknownSelection />;
	const href = (extra: Record<string, string> = {}) =>
		signalHref(place, { rating, cohort, page: String(page), ...extra });
	const visible = selected.slice((page - 1) * pageSize, page * pageSize);
	if (q.has("review") || q.has("version")) {
		const r = visible.find(
			(v) => v.id === q.get("review") && v.version === q.get("version"),
		);
		if (!r || source.reviews[r.id]?.version !== r.version)
			return <UnknownSelection />;
		return (
			<PublicReviewDetail
				r={source.reviews[r.id]}
				id={r.id}
				place={place}
				heading="Вклад записи"
				result={doc.run}
				back={href()}
				backLabel="Вернуться к вкладам"
			/>
		);
	}
	return (
		<section>
			<a href={overviewAddress(place, "one")}>Вернуться в обзор точки</a>
			<h2>Сигналы и оценки</h2>
			<p>Публичная синтетика · дата расчёта {doc.as_of}</p>
			<label>
				Точка расчёта
				<select
					value={place}
					onChange={(e) => location.assign(signalHref(e.target.value))}
				>
					{overview.places.map((p) => (
						<option value={p.id} key={p.id}>
							{p.name}
						</option>
					))}
				</select>
			</label>
			<RatingFilter
				value={rating}
				onSelect={(v) => location.assign(href({ rating: v, page: "1" }))}
			/>
			<h3>Рейтинг выборки</h3>
			<p>
				Среднее исходных оценок {fmt(raw)} ★ · взвешенное среднее{" "}
				{fmt(weighted)} ★
			</p>
			<p>
				Оценено {rated.length} из {selected.length}. Сумма весов {fmt(den)}.
				Изменение{" "}
				{raw !== null && weighted !== null
					? (weighted >= raw ? "+" : "") + fmt(weighted - raw)
					: "Н/Д · ZERO_DENOMINATOR"}
				.
			</p>
			<p>
				Средние относятся к выбранным записям. Веса зафиксированы в этом
				запуске. Официальный рейтинг площадки неизвестен.
			</p>
			{rating || cohort ? (
				<p>
					FILTER_NOT_COMPUTED · детекторы для подвыборки не пересчитывались.{" "}
					<a href={signalHref(place)}>Снять фильтры</a>
				</p>
			) : (
				<>
					<h3>События</h3>
					{!events.length && <p>Расчёт выполнен. Событий не выделено.</p>}
					{events.map(({ e, key, kind }) => (
						<details key={key}>
							<summary>
								{kind} · {e.date} · {e.severity}
							</summary>
							{e.observed !== undefined ? (
								<p>
									За день {e.observed}, медиана базы {e.expected}, MAD {e.mad},
									robust z {e.z_score}, множитель{" "}
									{e.multiplier ?? "не определён при медиане меньше 1"}. Оценки
									дня {JSON.stringify(e.ratings)}.
								</p>
							) : (
								<p>
									JSD {e.jsd} (натуральный логарифм). {e.dominant_shift}. Доли
									базы {JSON.stringify(e.baseline_dist)}, доли окна{" "}
									{JSON.stringify(e.event_dist)}.
								</p>
							)}
							<p>Сила сигнала {e.score}/100.</p>
							<Reasons {...e} />
							{(["event", "baseline"] as const).map((k) => (
								<p key={k}>
									<a href={href({ cohort: k + "-" + key, page: "1" })}>
										{k === "event" ? "Состав события" : "Состав базы"}
									</a>{" "}
									· {e[k].first} — {e[k].last} · записей {e[k].ids.length},
									оценено {e[k].rated}, исключено {e[k].excluded}.
								</p>
							))}
						</details>
					))}
					<Strength
						label="Эвристический индекс координации"
						value={doc.coordinated}
					/>
					<details>
						<summary>Компоненты и веса индекса</summary>
						{Object.entries(doc.coordinated_inputs).map(([k, v]) => (
							<p key={k}>
								{k} · {fmt(v, 8)} ×{" "}
								{doc.method.config.coordinated.components[k]}
							</p>
						))}
					</details>
				</>
			)}
			<h3>Вклад записи</h3>
			<p>
				Сила сигнала и уровень оснований описывают признаки текста и истории.
				Вероятность авторства или намерения не оценивалась.
			</p>
			{!selected.length && <p>В выборке нет записей.</p>}
			{visible.map((r) => (
				<details key={r.id}>
					<summary>
						{r.id} · {r.rating ?? "Н/Д"} ★ · вес {r.weight.weight}
					</summary>
					<p>
						Вклад в среднее:{" "}
						{r.rating === null || !den
							? "Н/Д"
							: fmt(r.weight.weight * r.rating) +
								" / " +
								fmt(den) +
								" = " +
								fmt((r.weight.weight * r.rating) / den, 8)}
						.
					</p>
					<a href={href({ review: r.id, version: r.version })}>
						Исходный отзыв {r.id}
					</a>
					<Strength label="Специфичность" value={r.specificity} />
					<Strength label="Шаблонность" value={r.templated} />
					<Strength label="Релевантность" value={r.relevance} />
					<Strength label="Опыт в категории" value={r.category} />
					<details>
						<summary>Расчёт веса</summary>
						<p>
							Из сигнала координации вычтена уже учтённая доля текстового
							повторения. Округление итогового веса до четырёх знаков.
						</p>
						<Values items={r.weight} />
						<Values items={doc.method.config.weight} />
					</details>
				</details>
			))}
			<Pager
				page={page}
				count={selected.length}
				href={(p) => href({ page: String(p) })}
			/>
		</section>
	);
}

type Metric = {
	value: number | null;
	reason: string | null;
	eligible: number;
	total: number;
	availability: string;
};
type Result = {
	workspace_id: string;
	snapshot_id: string;
	source_id: string;
	run_id: string;
	coverage: string;
	parameters: { as_of: string };
	observations: { numerator: number; denominator: number };
	quarantined: { numerator: number };
	confirmed_duplicates: { numerator: number };
	places: Record<
		string,
		{
			raw: Metric;
			events: Metric;
			weighted: Metric;
			templated: Metric;
			coordinated: Metric;
			relevance: Metric;
		}
	>;
	reviews: {
		version_id: string;
		place: string;
		rating: number | null;
		rating_exclusion: string | null;
		author_present: boolean;
		specificity: Evidence | null;
	}[];
};
type Selection = {
	runs: { id: string; status: string }[];
	result: Result | null;
	artifact_sha256: string | null;
};
export const signalKind = "snapshot_explainable_signals_v1";
export function CanonicalSignals({
	workspace,
	snapshot,
	source,
	run,
	job,
	busy,
	start,
	api,
	link,
	search,
}: {
	workspace: string;
	snapshot: string;
	source: string;
	run: string;
	job?: { status: string; reason: string | null; job_kind?: string } | null;
	busy: boolean;
	start: () => void;
	api: <T>(path: string) => Promise<T>;
	link: (changes: Record<string, string>) => string;
	search: string;
}) {
	const [selection, set] = useState<Selection | null>();
	const q = new URLSearchParams(search);
	const query = new URLSearchParams({ source_id: source });
	if (run && job?.status === "succeeded") query.set("run_id", run);
	const path =
		"/workspaces/" + workspace + "/snapshots/" + snapshot + "/signals?" + query;
	useEffect(() => {
		let live = true;
		api<Selection>(path)
			.then((v) => {
				if (live) set(v);
			})
			.catch(() => {
				if (live) set(null);
			});
		return () => {
			live = false;
		};
	}, [api, path]);
	const back = link({
		view: "overview",
		run: "",
		signalPlace: "",
		signalRating: "",
		signalPage: "",
	});
	const doc = selection?.result;
	const allowed = selection && (!run || job?.job_kind === signalKind);
	const place = q.get("signalPlace") ?? Object.keys(doc?.places ?? {})[0] ?? "";
	const rating = q.get("signalRating") ?? "",
		pageText = q.get("signalPage") ?? "1",
		page = Number(pageText);
	const rows =
		doc?.reviews.filter(
			(r) => r.place === place && matches(r.rating, rating),
		) ?? [];
	const rated = rows.flatMap((r) => (r.rating === null ? [] : [r.rating]));
	const ok =
		![
			"dataset",
			"result",
			"cohort",
			"group",
			"review",
			"location_id",
			"first_day",
			"last_day",
			"has_date",
			"has_text",
			"position",
			"rating",
		].some((k) => q.has(k)) &&
		(!doc ||
			(doc.source_id === source &&
				doc.snapshot_id === snapshot &&
				doc.run_id === run &&
				doc.workspace_id === workspace &&
				(!place || Object.hasOwn(doc.places, place)))) &&
		validRating(rating) &&
		validPage(pageText, rows.length);
	const metrics = doc?.places[place];
	return (
		<section>
			<a href={back}>Вернуться в обзор снимка</a>
			<h2>Сигналы и оценки</h2>
			{selection === undefined ? (
				<p role="status">Проверяем допуск снимка…</p>
			) : !allowed || !ok ? (
				<p role="alert">
					Сигналы недоступны для выбранной области или запуска.
				</p>
			) : (
				<>
					<label>
						Запуск сигналов
						<select
							value={run}
							onChange={(e) =>
								location.assign(
									link({
										run: e.target.value,
										signalPlace: "",
										signalPage: "",
										signalRating: "",
									}),
								)
							}
						>
							<option value="">Выберите запуск</option>
							{selection.runs.map((j) => (
								<option value={j.id} key={j.id}>
									{j.id} · {j.status}
								</option>
							))}
						</select>
					</label>
					<button
						type="button"
						disabled={
							busy || job?.status === "queued" || job?.status === "running"
						}
						onClick={start}
					>
						Рассчитать сигналы снимка
					</button>
					{run && (
						<p role="status">
							Состояние запуска · {job?.status} {job?.reason}
						</p>
					)}
					{!doc ? (
						<p>Результат сигналов для просмотра отсутствует.</p>
					) : (
						<>
							<p>
								Принято {doc.observations.numerator} из{" "}
								{doc.observations.denominator}. Карантин{" "}
								{doc.quarantined.numerator}, подтверждённые повторы{" "}
								{doc.confirmed_duplicates.numerator}. Дата расчёта{" "}
								{doc.parameters.as_of}.
							</p>
							<label>
								Точка расчёта
								<select
									value={place}
									onChange={(e) =>
										location.assign(
											link({ signalPlace: e.target.value, signalPage: "" }),
										)
									}
								>
									{Object.keys(doc.places).map((k) => (
										<option key={k}>{k}</option>
									))}
								</select>
							</label>
							<RatingFilter
								value={rating}
								onSelect={(v) =>
									location.assign(link({ signalRating: v, signalPage: "" }))
								}
							/>
							<p>
								Среднее исходных оценок{" "}
								{fmt(
									rated.length
										? rated.reduce((s, n) => s + n, 0) / rated.length
										: null,
								)}{" "}
								★ · оценено {rated.length} из {rows.length}.
							</p>
							<p>
								Распределение оценок ·{" "}
								{[1, 2, 3, 4, 5]
									.map((n) => n + " ★ — " + rated.filter((v) => v === n).length)
									.join(" · ")}
							</p>
							{metrics && (
								<>
									<p>
										Взвешенное среднее Н/Д · {metrics.weighted.reason}.
										Компоненты веса и вклад в модельное среднее недоступны.
									</p>
									<p>
										Охват{" "}
										{doc.coverage === "unknown" ? "неизвестен" : doc.coverage}.
										Временные и рейтинговые события ·{" "}
										{rating ? "FILTER_NOT_COMPUTED" : metrics.events.reason}.
									</p>
									<details>
										<summary>Доступность сигналов</summary>
										{(
											[
												"weighted",
												"events",
												"templated",
												"relevance",
												"coordinated",
											] as const
										).map((k) => (
											<p key={k}>
												{k} · {metrics[k].availability} · {metrics[k].eligible}/
												{metrics[k].total} · {metrics[k].reason}
											</p>
										))}
									</details>
								</>
							)}
							{!rows.length && <p>В выборке нет записей.</p>}
							{rows.slice((page - 1) * pageSize, page * pageSize).map((r) => (
								<article key={r.version_id}>
									<p>
										Оценка {r.rating ?? "Н/Д"} {r.rating_exclusion} · автор{" "}
										{r.author_present ? "указан" : "неизвестен"}.
									</p>
									{r.specificity ? (
										<Strength label="Специфичность" value={r.specificity} />
									) : (
										<p>Специфичность Н/Д · нет текста.</p>
									)}
									<a
										href={link({
											view: "reviews",
											source_id: source,
											review: r.version_id,
											fromSignals: "1",
										})}
									>
										Исходная версия {r.version_id}
									</a>
								</article>
							))}
							<Pager
								page={page}
								count={rows.length}
								href={(n) => link({ signalPage: String(n) })}
							/>
							<details>
								<summary>Источник результата</summary>
								<p>
									Снимок {snapshot} · запуск {run} · SHA-256{" "}
									{selection.artifact_sha256}
								</p>
							</details>
						</>
					)}
				</>
			)}
		</section>
	);
}
