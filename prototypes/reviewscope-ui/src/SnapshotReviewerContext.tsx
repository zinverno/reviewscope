import { useEffect, useState } from "react";
import {
	EvidenceDetail,
	evidenceFields,
	type EvidenceResult,
} from "./ReviewEvidence";

type Member = {
	version_id: string;
	location_id: string;
	location_name: string | null;
	position: number;
	day: string | null;
	rating: number | null;
	has_text: boolean;
	specificity: number | null;
};
type Page = {
	workspace_id: string;
	method: string;
	status: "computed" | "NO_REVIEWER_ID" | "AUTHOR_CONTEXT_NOT_ADMITTED";
	snapshot_id: string;
	source_id: string;
	anchor_version_id: string;
	manifest_sha256: string;
	offset: number;
	limit: number;
	total: number | null;
	metrics: {
		places: number;
		dated: number;
		first_day: string | null;
		last_day: string | null;
		rated: number;
		rating_mean: number | null;
		rating_counts: Record<string, number>;
		with_text: number;
	} | null;
	availability: Record<
		string,
		{
			status: "computed" | "insufficient_data" | "not_available";
			eligible: number | null;
			total: number | null;
			reason: string | null;
		}
	>;
	rows: Member[];
};
type Props = {
	workspace: string;
	snapshot: string;
	search: string;
	api: <T>(path: string, init?: RequestInit) => Promise<T>;
	link: (updates: Record<string, string>) => string;
};
export function privateUuid(value: string | null) {
	return (
		value !== null &&
		/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value)
	);
}
const fields: Record<string, string> = {
	places: "Места",
	observed_dates: "Даты публикации или отправки",
	ratings: "Оценки",
	specificity: "Конкретность текста",
	category: "Категория",
	geography: "Координаты",
	relevance: "Соответствие контексту точки",
};
const labels = {
	computed: "рассчитано",
	insufficient_data: "недостаточно данных",
	not_available: "недоступно",
};
const size = 25;
export function SnapshotReviewerContext({
	workspace,
	snapshot,
	search,
	api,
	link,
}: Props) {
	const [state, set] = useState<{
		doc: Page;
		evidence: EvidenceResult | null;
	} | null>();
	const q = new URLSearchParams(search);
	const version = q.get("review"),
		source = q.get("contextSource"),
		member = q.get("authorVersion");
	const rawPage = q.get("authorPage") ?? "1",
		page = Number(rawPage),
		offset = (page - 1) * size;
	const allowed = [
		"view",
		"workspace",
		"source",
		"snapshot",
		"run",
		"review",
		"contextSource",
		"contextFrom",
		"authorPage",
		"authorVersion",
		"project",
		"delivery",
		"offset",
		"signalPlace",
		"signalRating",
		"signalPage",
		"fromSignals",
		...evidenceFields,
	];
	const valid =
		privateUuid(version) &&
		privateUuid(source) &&
		privateUuid(snapshot) &&
		(!q.has("authorVersion") || privateUuid(member)) &&
		/^[1-9]\d{0,2}$/.test(rawPage) &&
		(!q.has("source") || source === q.get("source")) &&
		(!q.has("source_id") || source === q.get("source_id")) &&
		[...q.keys()].every((k) => allowed.includes(k) && q.getAll(k).length === 1);
	useEffect(() => {
		const controller = new AbortController();
		let revision = 0,
			timer: ReturnType<typeof setTimeout>;
		const refresh = async () => {
			clearTimeout(timer);
			const current = ++revision;
			try {
				if (!valid || !source || !version) throw new Error();
				const base =
					"/workspaces/" + workspace + "/snapshots/" + snapshot + "/reviews/";
				const init = {
					signal: AbortSignal.any([
						controller.signal,
						AbortSignal.timeout(12000),
					]),
				};
				const doc = await api<Page>(
					base +
						encodeURIComponent(version) +
						"/reviewer-context?" +
						new URLSearchParams({
							source_id: source,
							offset: String(offset),
							limit: String(size),
						}),
					init,
				);
				if (
					doc.workspace_id !== workspace ||
					doc.snapshot_id !== snapshot ||
					doc.source_id !== source ||
					doc.anchor_version_id !== version ||
					doc.method !== "snapshot-reviewer-context-v1" ||
					!doc.availability ||
					doc.offset !== offset ||
					doc.limit !== size ||
					![
						"computed",
						"NO_REVIEWER_ID",
						"AUTHOR_CONTEXT_NOT_ADMITTED",
					].includes(doc.status) ||
					(doc.status === "computed"
						? !doc.metrics ||
							doc.total === null ||
							(offset > 0 && offset >= doc.total)
						: doc.rows.length > 0 ||
							doc.metrics !== null ||
							doc.total !== null ||
							offset !== 0)
				)
					throw new Error();
				let evidence: EvidenceResult | null = null;
				if (member) {
					if (!doc.rows.some((r) => r.version_id === member)) throw new Error();
					evidence = await api<EvidenceResult>(
						base +
							encodeURIComponent(member) +
							"?source_id=" +
							encodeURIComponent(source),
						init,
					);
					if (
						evidence.snapshot_id !== snapshot ||
						evidence.source_id !== source ||
						evidence.rows.length !== 1 ||
						evidence.rows[0].version_id !== member
					)
						throw new Error();
				}
				if (!controller.signal.aborted && current === revision) {
					set({ doc, evidence });
					timer = setTimeout(refresh, 5000);
				}
			} catch {
				if (!controller.signal.aborted && current === revision) set(null);
			}
		};
		const activate = () => {
			set(undefined);
			void refresh();
		};
		const hide = () => {
			++revision;
			clearTimeout(timer);
			set(undefined);
		};
		const onVisibility = () => {
			if (document.hidden) hide();
			else activate();
		};
		void refresh();
		document.addEventListener("visibilitychange", onVisibility);
		window.addEventListener("pagehide", hide);
		window.addEventListener("focus", activate);
		window.addEventListener("pageshow", activate);
		return () => {
			controller.abort();
			clearTimeout(timer);
			document.removeEventListener("visibilitychange", onVisibility);
			window.removeEventListener("pagehide", hide);
			window.removeEventListener("focus", activate);
			window.removeEventListener("pageshow", activate);
		};
	}, [api, workspace, snapshot, source, version, member, offset, valid]);
	const back = link({
		view: "reviews",
		contextSource: "",
		contextFrom: "",
		...(q.get("contextFrom") === "signals"
			? { fromSignals: "1", source_id: source ?? "" }
			: {}),
		authorPage: "",
		authorVersion: "",
	});
	const anchor = (
		<nav>
			<a href={back}>Вернуться к исходному отзыву</a>
			{q.get("contextFrom") === "signals" && (
				<>
					{" "}
					·{" "}
					<a
						href={link({
							view: "signals",
							review: "",
							contextSource: "",
							contextFrom: "",
							authorPage: "",
							authorVersion: "",
						})}
					>
						Вернуться к сигналам запуска
					</a>
				</>
			)}
		</nav>
	);
	if (state === undefined)
		return <p role="status">Проверяем допуск к авторскому контексту…</p>;
	if (!state)
		return (
			<section>
				{anchor}
				<p role="alert">Снимок, версия или допуск к истории недоступны.</p>
			</section>
		);
	const { doc, evidence } = state;
	if (evidence)
		return (
			<EvidenceDetail detail={evidence.rows[0]} result={evidence}>
				<a href={link({ authorVersion: "" })}>Вернуться к истории отзывов</a>
			</EvidenceDetail>
		);
	return (
		<section>
			{anchor}
			<h2>История отзывов в снимке</h2>
			{doc.status !== "computed" || !doc.metrics ? (
				<p>
					{doc.status} ·{" "}
					{doc.status === "NO_REVIEWER_ID"
						? "У записи отсутствует разрешённый идентификатор автора. Профиль недоступен."
						: "Для этого источника и поставки отсутствует отдельный допуск к истории автора."}
				</p>
			) : (
				<>
					<p>
						В предоставленном снимке найдено {doc.total} отзывов этого автора ·
						различных точек {doc.metrics.places}.
					</p>
					{doc.total === 1 && <p>В снимке один отзыв этого автора.</p>}
					<p>
						Наблюдаемые даты ·{" "}
						{doc.metrics.first_day && doc.metrics.last_day
							? doc.metrics.first_day + " — " + doc.metrics.last_day
							: "неизвестны"}
						. Известных дат {doc.metrics.dated}/{doc.total}.
					</p>
					<p>
						Средняя оценка после приведения к шкале 1–5 ★ ·{" "}
						{doc.metrics.rating_mean?.toLocaleString("ru", {
							maximumFractionDigits: 3,
						}) ?? "Н/Д"}
						. Оценок {doc.metrics.rated}/{doc.total}. С текстом{" "}
						{doc.metrics.with_text}/{doc.total}.
					</p>
					<details>
						<summary>Распределение оценок и метод</summary>
						<p>
							{Object.entries(doc.metrics.rating_counts)
								.map(([r, n]) => r + " ★ · " + n)
								.join(", ") || "Н/Д · нет оценок на известной шкале"}
						</p>
						<p>
							Расчёт по принятым версиям одного источника в этом снимке. История
							за пределами предоставленных данных неизвестна.
						</p>
						<p>
							Метод {doc.method} · контрольная сумма состава{" "}
							{doc.manifest_sha256} · снимок {doc.snapshot_id} · исходная версия{" "}
							{doc.anchor_version_id}.
						</p>
					</details>
					<h3>Места, о которых оставлены отзывы</h3>
					<ol start={offset + 1}>
						{doc.rows.map((r) => (
							<li key={r.version_id}>
								<a href={link({ authorVersion: r.version_id })}>
									Исходная версия · позиция {r.position}
								</a>{" "}
								· {r.location_name ?? "Название точки неизвестно"} ·{" "}
								{r.day ?? "Дата неизвестна"} · оценка {r.rating ?? "Н/Д"} ·
								конкретность текста{" "}
								{r.specificity == null ? "Н/Д" : r.specificity + " из 100"}
							</li>
						))}
					</ol>
					<nav aria-label="Страницы истории">
						<p>
							Страница {page} из{" "}
							{Math.max(1, Math.ceil((doc.total ?? 0) / size))}.{" "}
							{offset > 0 && (
								<a href={link({ authorPage: String(page - 1) })}>
									Предыдущая страница
								</a>
							)}{" "}
							{offset + size < (doc.total ?? 0) && (
								<a href={link({ authorPage: String(page + 1) })}>
									Следующая страница
								</a>
							)}
						</p>
					</nav>
				</>
			)}
			<details>
				<summary>Пригодность данных для метрик</summary>
				<ul>
					{Object.entries(doc.availability).map(([name, a]) => (
						<li key={name}>
							{fields[name] ?? name} · {labels[a.status]} ·{" "}
							{a.eligible ?? "Н/Д"}/{a.total ?? "Н/Д"} отзывов{" "}
							{a.reason && " · " + a.reason}
						</li>
					))}
				</ul>
			</details>
			<p>
				CATEGORY_METADATA_UNAVAILABLE · категории точек отсутствуют.
				GEO_METADATA_UNAVAILABLE · координаты точек отсутствуют. Локальный
				контекст и карта недоступны.
			</p>
		</section>
	);
}
