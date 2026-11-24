import { useEffect, useState } from "react";
import type { FormEvent } from "react";

type Row = {
	position: number;
	version_id: string;
	identity_id: string;
	review_id: string | null;
	version_no: number;
	version_batch_id: string;
	location_name: string | null;
	place_key: string;
	rating: number | null;
	raw_rating: string | boolean | null;
	rating_scale: number[] | null;
	time_kind: string | null;
	event_value: string | null;
	time_precision: string | null;
	utc_offset: string | null;
	event_basis: string | null;
	excerpt: string | null;
	full_text: string | null;
	source_url: string | null;
};
type Result = {
	snapshot_id: string;
	source_id: string;
	source_label: string;
	batch_id: string;
	coverage: string;
	covered_from: string | null;
	covered_until: string | null;
	historical: boolean;
	total: number;
	offset: number;
	limit: number;
	rows: Row[];
};
type Target = {
	source_id: string;
	source_label: string;
	location_id: string;
	location_name: string | null;
};
type Props = {
	workspace: string;
	snapshot: string;
	search: string;
	targets: Target[];
	api: (path: string) => Promise<Result>;
	link: (changes: Record<string, string>) => string;
};
export const evidenceFields = [
	"source_id",
	"location_id",
	"rating",
	"has_text",
	"has_date",
	"first_day",
	"last_day",
	"position",
] as const;
const coverage: Record<string, string> = {
	unknown: "неизвестно",
	partial: "частичное",
	full: "полное по паспорту",
};
const basis: Record<string, string> = {
	published: "публикация",
	submitted: "отправка",
	edited: "редактирование",
	unknown: "неизвестно",
};
function flags(name: string, label: string, params: URLSearchParams) {
	return (
		<label>
			{label}
			<select name={name} defaultValue={params.get(name) ?? ""}>
				<option value="">Любое</option>
				<option value="true">Есть</option>
				<option value="false">Нет</option>
			</select>
		</label>
	);
}
function eventTime(row: Row) {
	if (!row.event_value) return "Дата неизвестна";
	return (
		row.event_value.replace("T", " ") +
		(row.time_kind === "local" ? " · местное время, пояс неизвестен" : "")
	);
}
export function ReviewEvidence({
	workspace,
	snapshot,
	search,
	targets,
	api,
	link,
}: Props) {
	const params = new URLSearchParams(search);
	const selected = params.get("review");
	const [state, setState] = useState<{
		key: string;
		result?: Result;
		detail?: Row;
		error?: string;
	}>();
	const filters = new URLSearchParams();
	for (const name of evidenceFields) {
		const value = params.get(name);
		if (value) filters.set(name, value);
	}
	filters.set("offset", params.get("offset") || "0");
	filters.set("limit", "25");
	const query = filters.toString();
	const base = `/workspaces/${workspace}/snapshots/${snapshot}/reviews`;
	const key = base + "?" + query + "#" + (selected ?? "");
	useEffect(() => {
		let live = true;
		setState(undefined);
		Promise.all([
			api(base + "?" + query),
			selected
				? api(base + "/" + encodeURIComponent(selected) + "?" + query)
				: Promise.resolve(null),
		])
			.then(([result, detail]) => {
				if (live) setState({ key, result, detail: detail?.rows[0] });
			})
			.catch((e: Error) => {
				if (live) setState({ key, error: e.message });
			});
		return () => {
			live = false;
		};
	}, [api, base, query, selected, key]);
	const matching = state?.key === key ? state : undefined;
	const result = matching?.result,
		detail = matching?.detail;
	const targetSource = result?.source_id ?? params.get("source");
	const uniqueSources = [
		...new Map(targets.map((t) => [t.source_id, t])).values(),
	];
	function submit(e: FormEvent<HTMLFormElement>) {
		e.preventDefault();
		const data = new FormData(e.currentTarget);
		const changes: Record<string, string> = { offset: "", review: "" };
		for (const name of evidenceFields)
			changes[name] = String(data.get(name) ?? "");
		location.assign(link(changes));
	}
	return (
		<section className="evidence">
			{params.get("fromSignals") === "1" && (
				<p>
					<a
						href={link({
							view: "signals",
							review: "",
							fromSignals: "",
							source_id: "",
						})}
					>
						Вернуться к сигналам запуска
					</a>
				</p>
			)}
			<a href={link({ view: "", review: "" })}>Вернуться к снимку</a>
			<p>
				<a href={link({ view: "overview" })}>Обзор этой выборки</a>
			</p>
			<h2>Отзывы и доказательства</h2>
			{matching?.error ? (
				<p role="alert">Снимок или версия недоступны. {matching.error}</p>
			) : !result ? (
				<p role="status">Загрузка отзывов…</p>
			) : (
				<>
					<p>
						{result.historical ? "Исторический снимок" : "Снимок"} ·{" "}
						{result.source_label} · Покрытие {coverage[result.coverage]}.{" "}
						{result.covered_from || result.covered_until
							? `Период ${result.covered_from ?? "неизвестно"} — ${result.covered_until ?? "неизвестно"}.`
							: "Период покрытия неизвестен."}
					</p>
					<details open={!selected}>
						<summary>Фильтры отзывов</summary>
						<form onSubmit={submit}>
							<div className="pair">
								<label>
									Источник отзывов
									<select
										name="source_id"
										defaultValue={params.get("source_id") ?? ""}
									>
										<option value="">Источник снимка</option>
										{uniqueSources.map((t) => (
											<option key={t.source_id} value={t.source_id}>
												{t.source_label}
											</option>
										))}
									</select>
								</label>
								<label>
									Точка
									<select
										name="location_id"
										defaultValue={params.get("location_id") ?? ""}
									>
										<option value="">Все точки</option>
										{targets
											.filter((t) => t.source_id === targetSource)
											.map((t) => (
												<option key={t.location_id} value={t.location_id}>
													{t.location_name ?? t.location_id}
												</option>
											))}
									</select>
								</label>
								<label>
									Дата с
									<input
										type="date"
										name="first_day"
										defaultValue={params.get("first_day") ?? ""}
									/>
								</label>
								<label>
									Дата по
									<input
										type="date"
										name="last_day"
										defaultValue={params.get("last_day") ?? ""}
									/>
								</label>
								<label>
									Оценка
									<select
										name="rating"
										defaultValue={params.get("rating") ?? ""}
									>
										<option value="">Все оценки</option>
										{[1, 2, 3, 4, 5].map((n) => (
											<option key={n}>{n}</option>
										))}
									</select>
								</label>
								<label>
									Позиция в поставке
									<input
										type="number"
										min="1"
										max="10000"
										name="position"
										defaultValue={params.get("position") ?? ""}
									/>
								</label>
								{flags("has_text", "Наличие текста", params)}
								{flags("has_date", "Наличие даты", params)}
							</div>
							<p>
								Границы периода включительны. Моменты времени сравниваются по
								дате UTC.
							</p>
							<button type="submit">Применить фильтры</button>
						</form>
					</details>
					<p aria-live="polite">Отзывов в выборке: {result.total}</p>
					{detail && (
						<article
							aria-label="Доказательство отзыва"
							className="evidence-detail"
						>
							<h3>
								Отзыв {detail.review_id ?? `в позиции ${detail.position}`} ·
								версия {detail.version_no}
							</h3>
							<p>
								{detail.location_name ?? detail.place_key} · {eventTime(detail)}{" "}
								· {basis[detail.event_basis ?? "unknown"]}
							</p>
							<h4>Полный текст</h4>
							<p>Точный фрагмент не размечен.</p>
							{detail.full_text ? (
								<blockquote>{detail.full_text}</blockquote>
							) : (
								<p>Текст отсутствует.</p>
							)}
							<dl>
								{Object.entries({
									"Внешний ID": detail.review_id,
									"Точка источника": detail.place_key,
									Позиция: detail.position,
									"Оценка источника": detail.raw_rating,
									"Шкала источника": detail.rating_scale?.join("–"),
									"Оценка 1–5": detail.rating,
									"Точность времени": detail.time_precision,
									"Исходное смещение UTC": detail.utc_offset,
									Идентичность: detail.identity_id,
									Версия: detail.version_id,
									"Поставка версии": detail.version_batch_id,
									"Поставка снимка": result.batch_id,
									Снимок: result.snapshot_id,
								}).map(([label, value]) => (
									<div key={label}>
										<dt>{label}</dt>
										<dd>{value == null ? "Неизвестно" : String(value)}</dd>
									</div>
								))}
							</dl>
							{detail.source_url ? (
								<p>
									<a
										href={detail.source_url}
										target="_blank"
										rel="noopener noreferrer"
									>
										Открыть проверенную ссылку источника
									</a>
								</p>
							) : (
								<p>Внешняя ссылка для этой выдачи недоступна.</p>
							)}
							<a href={link({ review: "" })}>К списку с фильтрами</a>
						</article>
					)}
					{result.rows.length ? (
						<ol className="evidence-rows">
							{result.rows.map((row) => (
								<li key={row.version_id}>
									<a
										aria-current={
											selected === row.version_id ? "true" : undefined
										}
										href={link({ review: row.version_id })}
									>
										Позиция {row.position} ·{" "}
										{row.review_id ?? "без внешнего ID"} · версия{" "}
										{row.version_no}
									</a>
									<p>{row.excerpt ?? "Текст отсутствует"}</p>
									<small>
										{row.location_name ?? row.place_key} · {eventTime(row)} ·
										Оценка {row.rating ?? "неизвестна"}
									</small>
								</li>
							))}
						</ol>
					) : (
						<p>Подтверждённых отзывов по выбранным условиям нет.</p>
					)}
					<nav aria-label="Страницы отзывов">
						{result.offset > 0 && (
							<a
								href={link({
									offset: String(Math.max(0, result.offset - result.limit)),
									review: "",
								})}
							>
								Предыдущая страница
							</a>
						)}{" "}
						{result.offset + result.limit < result.total && (
							<a
								href={link({
									offset: String(result.offset + result.limit),
									review: "",
								})}
							>
								Следующая страница
							</a>
						)}
					</nav>
				</>
			)}
		</section>
	);
}
