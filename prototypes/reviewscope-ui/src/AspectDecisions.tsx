import { useEffect, useRef, useState } from "react";
import { type Api, Definition, type Detail, type Frozen } from "./Aspects";

const outcomes = {
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
type Outcome = keyof typeof outcomes;
type SavedRun = { id: string; status: string };
type Catalog = {
	definition: Detail;
	runs: SavedRun[];
	profile: string;
	supported_definition: boolean;
};
type Counts = {
	N_input: number;
	N_accepted: number;
	N_eligible: number;
	N_evaluated: number;
	N_not_evaluated: number;
	N_quarantine: number;
	N_confirmed_duplicate: number;
	N_excluded: number;
	N_mentioned: number;
	N_complaints: number;
	N_uncertain: number;
	N_topic_uncertain: number;
	N_mentioned_unclear: number;
};
type Row = {
	version_id: string;
	outcome: Outcome;
	score: number | null;
	reason: string | null;
	start: number | null;
	end: number | null;
	counter_start: number | null;
	counter_end: number | null;
	text_sha256: string;
	content_sha256: string;
};
type Excerpt = { start: number; end: number; length: number; text: string };
type Page = SavedRun & {
	compute_state: "COMPLETE" | "NOT_COMPUTED";
	stage: string;
	reason: string | null;
	total: number | null;
	processed: number;
	definition: Frozen;
	profile: string;
	threshold_set: string;
	profile_sha256: string | null;
	rows_sha256: string | null;
	manifest_sha256: string | null;
	artifact_sha256: string | null;
	summary: Counts | null;
	filtered_total: number | null;
	rows: Row[];
	evidence: Excerpt | null;
	counter_context: Excerpt | null;
};
type Props = {
	workspace: string;
	source: string;
	snapshot: string;
	search: string;
	api: Api;
	link: (changes: Record<string, string>) => string;
};
function ratio(n: number, d: number) {
	return d
		? `${n}/${d} (${(n / d).toLocaleString("ru-RU", { style: "percent", maximumFractionDigits: 1 })})`
		: `${n}/0 · нет знаменателя`;
}
function Summary({ counts: c }: { counts: Counts }) {
	return (
		<section aria-label="Итог оценки">
			<h3>Модель выделила жалобы в {c.N_complaints} отзывах</h3>
			<p>
				Среди всех пригодных отзывов — {ratio(c.N_complaints, c.N_eligible)}.
				Среди упоминаний аспекта — {ratio(c.N_complaints, c.N_mentioned)}.
			</p>
			<p>
				Принято {c.N_accepted}, пригодно {c.N_eligible}, обработано{" "}
				{c.N_evaluated}, без текста {c.N_excluded}, не обработано{" "}
				{c.N_not_evaluated}.
			</p>
			<p>
				Неясных решений {c.N_uncertain}. Из них тема неясна в{" "}
				{c.N_topic_uncertain}, позиция при упоминании неясна в{" "}
				{c.N_mentioned_unclear}.
			</p>
			<details>
				<summary>Покрытие и состав поставки</summary>
				<p>
					Обработано от пригодных — {ratio(c.N_evaluated, c.N_eligible)}.
					Определённых решений от пригодных —{" "}
					{ratio(c.N_evaluated - c.N_uncertain, c.N_eligible)}.
				</p>
				<p>
					Входных позиций {c.N_input}, в карантине {c.N_quarantine},
					подтверждённых повторных позиций {c.N_confirmed_duplicate}.
				</p>
			</details>
		</section>
	);
}
export function AspectDecisions({
	workspace,
	source,
	snapshot,
	search,
	link,
	api,
}: Props) {
	const query = new URLSearchParams(search);
	const aspect = query.get("aspect") || "",
		version = query.get("aspectVersion") || "";
	const run = query.get("decisionRun") || "",
		member = query.get("decisionReview") || "";
	const filter = query.get("decisionOutcome") || "",
		offset = Number(query.get("decisionOffset") || 0);
	const route = `/workspaces/${workspace}/snapshots/${snapshot}/decisions`;
	const [catalog, setCatalog] = useState<Catalog>();
	const [page, setPage] = useState<Page>();
	const [error, setError] = useState(""),
		[notice, setNotice] = useState(""),
		[busy, setBusy] = useState(false);
	const [reload, setReload] = useState(0);
	const key = useRef<string | null>(null);
	useEffect(() => {
		let live = true,
			timer: ReturnType<typeof setTimeout>;
		const control = new AbortController();
		const clear = () => {
			setCatalog(undefined);
			setPage(undefined);
		};
		if (reload > 0) clear();
		async function read() {
			try {
				if (
					!aspect ||
					!version ||
					!Number.isInteger(offset) ||
					offset < 0 ||
					offset > 10000 ||
					(filter && !Object.hasOwn(outcomes, filter)) ||
					(!run && (member || filter || offset))
				)
					throw new Error("Выбранный запуск или версия недоступны.");
				const init = { signal: control.signal };
				let active = false;
				if (!run) {
					const binding = new URLSearchParams({
						source_id: source,
						aspect_id: aspect,
						version_id: version,
					});
					const data = await api<Catalog>(`${route}?${binding}`, init);
					if (live) setCatalog(data);
				} else {
					const args = new URLSearchParams({
						source_id: source,
						offset: String(offset),
					});
					if (filter) args.set("outcome", filter);
					if (member) args.set("version_id", member);
					const data = await api<Page>(`${route}/${run}?${args}`, init);
					if (
						data.id !== run ||
						data.definition.aspect_id !== aspect ||
						data.definition.version_id !== version ||
						(member &&
							(data.rows.length !== 1 ||
								data.rows[0].version_id !== member ||
								!data.evidence))
					)
						throw new Error("Результат недоступен в выбранной области.");
					if (live) setPage(data);
					active = ["queued", "running"].includes(data.status);
				}
				if (live) {
					setError("");
					timer = setTimeout(read, active ? 1000 : 5000);
				}
			} catch (e) {
				if (live) {
					clear();
					setError((e as Error).message);
				}
			}
		}
		void read();
		const refresh = () => {
			clear();
			setReload((n) => n + 1);
		};
		const restored = (e: PageTransitionEvent) => {
			if (e.persisted) refresh();
		};
		window.addEventListener("pagehide", clear);
		window.addEventListener("focus", refresh);
		window.addEventListener("pageshow", restored);
		return () => {
			live = false;
			control.abort();
			clearTimeout(timer);
			window.removeEventListener("pagehide", clear);
			window.removeEventListener("focus", refresh);
			window.removeEventListener("pageshow", restored);
		};
	}, [
		api,
		source,
		route,
		run,
		aspect,
		version,
		member,
		filter,
		offset,
		reload,
	]);
	const fresh = {
		decisionRun: "",
		decisionReview: "",
		decisionOutcome: "",
		decisionOffset: "",
	};
	async function submit() {
		setBusy(true);
		setNotice("");
		const address = location.href;
		try {
			if (run) {
				await api(`/workspaces/${workspace}/runs/${run}/cancel`, {
					method: "POST",
				});
				setReload((n) => n + 1);
			} else if (
				catalog?.supported_definition &&
				!catalog.definition.archived
			) {
				key.current ??= crypto.randomUUID();
				const job = await api<SavedRun>(route, {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						source_id: source,
						aspect_id: aspect,
						version_id: version,
						idempotency_key: key.current,
						profile: catalog.profile,
					}),
				});
				if (address === location.href)
					location.assign(link({ ...fresh, decisionRun: job.id }));
			}
		} catch (e) {
			if (address === location.href) {
				setNotice((e as Error).message);
				setReload((n) => n + 1);
			}
		} finally {
			setBusy(false);
		}
	}
	const definition = catalog?.definition ?? page?.definition;
	const row = member ? page?.rows[0] : null;
	const text = page?.evidence;
	return (
		<section className="evidence" style={{ overflowWrap: "anywhere" }}>
			<h2>Оценка аспекта по всему снимку</h2>
			{notice && <p role="alert">{notice}</p>}
			{error && (
				<p role="alert">
					{error}{" "}
					<button type="button" onClick={() => setReload((n) => n + 1)}>
						Повторить чтение
					</button>
				</p>
			)}
			{!definition && !error && <p role="status">Проверка доступа…</p>}
			{definition && (
				<>
					{page?.compute_state === "COMPLETE" && page.summary && (
						<Summary counts={page.summary} />
					)}
					<p>
						Экспериментальный профиль требует ручной проверки. В отложенной
						авторской синтетической выборке модель нашла 4 из 32 размеченных
						жалоб, включая 0 из 6 жалоб на очередь и 3 из 8 на выдачу готового
						заказа. Из 5 решений «жалоба» верными были 4. Порог качества не
						пройден.
					</p>
					{page && page.compute_state !== "COMPLETE" && (
						<p role="status">
							Полного результата пока нет. Состояние {page.status} ·{" "}
							{page.reason || page.stage}. Обработано {page.processed}, всего{" "}
							{page.total ?? "пока неизвестно"}.
						</p>
					)}
					<p>
						Снимок {snapshot} · Источник {source} · Профиль{" "}
						{catalog?.profile ?? page?.profile}
					</p>
					<Definition value={definition} />
					{catalog && (
						<>
							{!catalog.supported_definition && (
								<p role="status">
									Эта версия определения не поддерживается профилем. Доступен
									просмотр исходных отзывов.
								</p>
							)}
							<button
								type="button"
								disabled={
									busy ||
									catalog.definition.archived ||
									!catalog.supported_definition
								}
								onClick={submit}
							>
								Оценить все пригодные отзывы
							</button>
							{catalog.runs.length > 0 && (
								<details>
									<summary>Последние запуски этой версии</summary>
									<ul>
										{catalog.runs.map((r) => (
											<li key={r.id}>
												<a href={link({ ...fresh, decisionRun: r.id })}>
													{r.id} · {r.status}
												</a>
											</li>
										))}
									</ul>
								</details>
							)}
						</>
					)}
					{page && (
						<>
							{["queued", "running"].includes(page.status) && (
								<button type="button" disabled={busy} onClick={submit}>
									Отменить оценку
								</button>
							)}
							<a href={link(fresh)}>К запускам этой версии</a>
							{page.compute_state === "COMPLETE" && (
								<>
									<details>
										<summary>Происхождение результата</summary>
										<p>
											Запуск {run} · Набор порогов {page.threshold_set}
										</p>
										<p>
											SHA-256 профиля {page.profile_sha256}, снимка{" "}
											{page.manifest_sha256}, строк {page.rows_sha256},
											артефакта {page.artifact_sha256}.
										</p>
									</details>
									{row && text ? (
										<article aria-label="Решение по версии отзыва">
											<h3>{outcomes[row.outcome]}</h3>
											<p>Версия {row.version_id}</p>
											{row.start !== null ? (
												<p>
													Контекст модели [{row.start}, {row.end}). Показан
													текст [{text.start}, {text.end}).
												</p>
											) : (
												<p>
													Модель не указала область текста. Показано начало
													версии.
												</p>
											)}
											<blockquote style={{ whiteSpace: "pre-wrap" }}>
												{text.text || "Текст отсутствует."}
											</blockquote>
											{page.counter_context && (
												<>
													<p>
														Контекст модели с другой позицией [
														{row.counter_start}, {row.counter_end}). Показан
														текст [{page.counter_context.start},{" "}
														{page.counter_context.end}).
													</p>
													<blockquote style={{ whiteSpace: "pre-wrap" }}>
														{page.counter_context.text}
													</blockquote>
												</>
											)}
											<details>
												<summary>Основание и проверка текста</summary>
												<p>
													Код решения {row.reason}. Оценка модели{" "}
													{row.score ?? "нет"}. SHA-256 текста {row.text_sha256}
													. Длина версии {text.length} символов Unicode.
												</p>
											</details>
											<a
												href={link({
													view: "reviews",
													review: row.version_id,
													source,
												})}
											>
												Открыть исходную версию отзыва
											</a>
											{" · "}
											<a href={link({ decisionReview: "" })}>
												К списку решений
											</a>
										</article>
									) : (
										<>
											<label>
												Исход решения{" "}
												<select
													value={filter}
													onChange={(e) =>
														location.assign(
															link({
																decisionOutcome: e.target.value,
																decisionOffset: "",
																decisionReview: "",
															}),
														)
													}
												>
													<option value="">Все исходы</option>
													{Object.entries(outcomes).map(([k, v]) => (
														<option key={k} value={k}>
															{v}
														</option>
													))}
												</select>
											</label>
											<p>
												Отзывов с выбранным исходом во всём снимке —{" "}
												{page.filtered_total}.
											</p>
											<ul aria-label="Решения по отзывам">
												{page.rows.map((r) => (
													<li key={r.version_id}>
														<a href={link({ decisionReview: r.version_id })}>
															{outcomes[r.outcome]} · {r.version_id}
														</a>
													</li>
												))}
											</ul>
											{offset > 0 && (
												<a
													href={link({
														decisionOffset: String(Math.max(0, offset - 25)),
													})}
												>
													Предыдущие 25
												</a>
											)}{" "}
											{offset + 25 < (page.filtered_total ?? 0) && (
												<a href={link({ decisionOffset: String(offset + 25) })}>
													Следующие 25
												</a>
											)}
										</>
									)}
								</>
							)}
						</>
					)}
					<p>
						<a href={link({ view: "aspects", project: definition.project_id })}>
							Открыть определение аспекта
						</a>
						{" · "}
						<a href={link({ view: "reviews", source, review: "" })}>
							Отзывы снимка
						</a>
					</p>
				</>
			)}
		</section>
	);
}
