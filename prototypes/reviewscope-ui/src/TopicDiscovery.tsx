import { useEffect, useRef, useState } from "react";

type Props = {
	workspace: string;
	source: string;
	snapshot: string;
	search: string;
	api: <T>(path: string, init?: RequestInit) => Promise<T>;
	link: (changes: Record<string, string>) => string;
};
type Candidate = {
	id: string;
	term: string | null;
	content_key: string;
	support: number;
	versions: number;
	state: string;
};
type Member = {
	version_id: string;
	start: number;
	end: number;
	text_sha?: string;
};
type Journal = {
	id: string;
	action: string;
	reason: string;
	parents: string[];
	actor: string;
	created_at: string;
	aspect_id: string | null;
	aspect_version_id: string | null;
};
type Check = {
	id: string;
	prior: string | null;
	receipt: {
		state: string;
		N_searched?: number;
		hits?: Member[];
		version_id?: string;
		historical_period?: { N: number; known_dates: number; coverage: string };
	};
};
type Manifest = {
	project_id: string;
	workspace_id: string;
	source_id: string;
	snapshot_id: string;
	N_input: number;
	N_accepted: number;
	N_selected: number;
	N_unique_inputs: number;
	N_noise: number;
	N_clustered: number;
	N_blank: number;
	N_quarantine: number;
	N_confirmed_duplicate: number;
	status: string;
	decision_run: string | null;
	human_revision: string | null;
};
type Page = {
	id: string;
	digest: string;
	candidate_id: string | null;
	content_key: string;
	term: string | null;
	support: number;
	manifest: Manifest;
	total: number;
	rows: (Candidate | Member)[];
	events: Journal[] | null;
	children: string[] | null;
	representatives: Member[] | null;
	history: Check[] | null;
	evidence?: { text: string; version_id: string; snapshot_id: string };
};
type Options = {
	N_input: number;
	N_accepted: number;
	project_id: string;
	reason: string | null;
	results?: { id: string; digest: string }[];
};
type Definition = {
	name: string;
	aspect_id: string;
	version_id: string;
	archived: boolean;
};
const names: Record<string, string> = {
	unconfirmed: "Ожидает проверки",
	reject: "Отклонён",
	confirm: "Подтверждён",
	merge: "Объединение",
	split: "Разделение",
	HISTORY_NOT_AVAILABLE: "Не хватает дат публикации",
	HISTORY_PARTIAL: "Частичное покрытие истории",
	HISTORY_NOT_COMPARABLE: "Периоды или условия несопоставимы",
	HISTORY_CHECKED: "Проверен выбранный период",
	OBSERVED_IN_SELECTED_HISTORY: "Проблема наблюдалась в выбранной истории",
	ANALOGY_REJECTED: "Аналогия отклонена",
	INSUFFICIENT_SUPPORT: "Недостаточно разных текстов",
	NO_ELIGIBLE_TEXTS: "Нет подходящих текстов",
	ALL_NOISE: "Ни одна группа не достигла минимальной поддержки",
};
const getName = (x: string) => names[x] ?? x;

export function TopicDiscovery({
	workspace,
	source,
	snapshot,
	search,
	api,
	link,
}: Props) {
	const params = new URLSearchParams(search),
		result = params.get("topicResult") || "",
		candidate = params.get("topicCandidate") || "",
		history = params.get("topicHistory") || "",
		run = params.get("topicRun") || "",
		revision = params.get("topicRevision") || "",
		version = params.get("topicEvidence") || "",
		digest = params.get("topicDigest") || "",
		offset = Number(params.get("topicPage") || 0);
	const root = `/workspaces/${workspace}/snapshots/${snapshot}/topic-discovery`;
	const [view, setView] = useState<{
			options?: Options;
			page?: Page;
			defs: Definition[];
			snapshots: string[];
		}>(),
		[error, setError] = useState(""),
		[busy, setBusy] = useState(false),
		[refresh, setRefresh] = useState(0),
		[chosen, setChosen] = useState<string[]>([]),
		[reason, setReason] = useState(""),
		[mergeIds, setMergeIds] = useState<string[]>([]),
		[aspect, setAspect] = useState(""),
		[past, setPast] = useState(history);
	const live = useRef(false),
		generation = useRef(0),
		intent = useRef({ signature: "", key: "" });
	function href(changes: Record<string, string>) {
		return link({ topicPage: "", topicEvidence: "", ...changes });
	}
	function inspect(id: string) {
		return href({ topicCandidate: id, topicHistory: "" });
	}
	useEffect(() => {
		if (refresh) setError("");
		live.current = true;
		const reset = () => {
			++generation.current;
			setView(undefined);
			setError("");
			setBusy(false);
			setChosen([]);
			setMergeIds([]);
			setReason("");
			setAspect("");
		};
		const load = async () => {
			const n = ++generation.current;
			if (document.hidden) {
				reset();
				return;
			}
			try {
				const q = new URLSearchParams({ source_id: source });
				let page: Page | undefined, options: Options | undefined;
				if (result) {
					if (candidate) q.set("candidate_id", candidate);
					if (history) q.set("historical_snapshot", history);
					if (version) q.set("version", version);
					if (digest) q.set("digest", digest);
					q.set("offset", String(offset));
					page = await api<Page>(
						root + "/" + encodeURIComponent(result) + "?" + q,
					);
					if (
						page.id !== result ||
						page.candidate_id !== (candidate || null) ||
						page.manifest.workspace_id !== workspace ||
						page.manifest.source_id !== source ||
						page.manifest.snapshot_id !== snapshot ||
						page.manifest.decision_run !== (run || null) ||
						page.manifest.human_revision !== (revision || null) ||
						(version &&
							(page.evidence?.version_id !== version ||
								page.evidence?.snapshot_id !== (history || snapshot)))
					)
						throw Error();
				} else {
					if (run) q.set("decision_run", run);
					if (revision) q.set("human_revision", revision);
					options = await api<Options>(root + "?" + q);
				}
				let defs: Definition[] = [],
					snapshots: string[] = [];
				if (candidate && page) {
					defs = await api<Definition[]>(
						`/workspaces/${workspace}/projects/${page.manifest.project_id}/aspects?offset=0`,
					);
					const rows = await api<{ id: string; source_id: string }[]>(
						`/workspaces/${workspace}/snapshots?offset=0`,
					);
					snapshots = rows
						.filter((s) => s.source_id === source && s.id !== snapshot)
						.map((s) => s.id);
				}
				if (live.current && n === generation.current) {
					setView({ page, options, defs, snapshots });
					setError("");
				}
			} catch {
				if (live.current && n === generation.current) {
					reset();
					setError("Выбранная область или свидетельство недоступны.");
				}
			}
		};
		void load();
		const timer = window.setInterval(load, 10000);
		window.addEventListener("pagehide", reset);
		window.addEventListener("pageshow", load);
		window.addEventListener("focus", load);
		document.addEventListener("visibilitychange", load);
		return () => {
			live.current = false;
			++generation.current;
			clearInterval(timer);
			window.removeEventListener("pagehide", reset);
			window.removeEventListener("pageshow", load);
			window.removeEventListener("focus", load);
			document.removeEventListener("visibilitychange", load);
		};
	}, [
		api,
		root,
		source,
		snapshot,
		workspace,
		candidate,
		result,
		history,
		version,
		digest,
		offset,
		run,
		revision,
		refresh,
	]);
	async function write(
		path: string,
		fields: Record<string, unknown>,
		open = false,
	) {
		if (busy || !view) return;
		setBusy(true);
		setError("");
		const signature = JSON.stringify([path, fields]);
		if (intent.current.signature !== signature)
			intent.current = { signature, key: crypto.randomUUID() };
		const epoch = generation.current;
		try {
			const saved = await api<{ id: string }>(root + path, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					source_id: source,
					idempotency_key: intent.current.key,
					...fields,
				}),
			});
			if (!live.current || document.hidden || epoch !== generation.current)
				return;
			intent.current = { signature: "", key: "" };
			if (open) location.assign(href({ topicResult: saved.id }));
			else {
				setReason("");
				setChosen([]);
				setMergeIds([]);
				setRefresh((x) => x + 1);
			}
		} catch (e) {
			if (live.current && epoch === generation.current) {
				setView(undefined);
				setError(
					e instanceof Error
						? e.message
						: "Проверьте доступ и повторите операцию.",
				);
			}
		} finally {
			if (live.current) setBusy(false);
		}
	}
	const page = view?.page,
		m = page?.manifest,
		options = view?.options;
	const action = (
		kind: string,
		ids: string[],
		extra: Record<string, unknown> = {},
	) =>
		write("/" + result + "/actions", {
			action: kind,
			expected_open: ids,
			reason,
			...extra,
		});
	const check = (extra: Record<string, unknown> = {}) =>
		write("/" + result + "/history-checks", {
			candidate_id: candidate,
			historical_snapshot: history,
			...extra,
		});
	const terminal = page?.events?.some((e) => e.parents.includes(candidate));
	const showVersion = (v: string) =>
		href({ topicPage: String(offset), topicEvidence: v });
	return (
		<section>
			<h2>Кандидаты тем выбранного снимка</h2>
			{error && (
				<p role="alert">
					{error}{" "}
					<button type="button" onClick={() => setRefresh((x) => x + 1)}>
						Обновить
					</button>
				</p>
			)}
			{!view && !error && <p role="status">Проверка допуска…</p>}
			{options && (
				<>
					<p>
						Строк в поставке {options.N_input}, принятых версий{" "}
						{options.N_accepted}.
					</p>
					{run && (
						<p>
							Отбор по выбранному решению {run}. Машинный пропуск остаётся
							гипотезой.
						</p>
					)}
					{options.reason ? (
						<p>RESOURCE_LIMIT · {options.reason}. Обработано 0 версий.</p>
					) : (
						<>
							<button
								type="button"
								disabled={busy}
								onClick={() =>
									write(
										"",
										{
											decision_run: run || null,
											human_revision: revision || null,
										},
										true,
									)
								}
							>
								Исследовать {run ? "неясные исходы" : "все пригодные тексты"}
							</button>
							{options.results?.map((r) => (
								<p key={r.id}>
									<a href={href({ topicResult: r.id, topicDigest: r.digest })}>
										Сохранённый результат {r.id}
									</a>
								</p>
							))}
						</>
					)}
				</>
			)}
			{page && m && (
				<>
					<p>
						Сгруппировано {m.N_clustered} из {m.N_selected} выбранных версий.
						Различных текстов {m.N_unique_inputs}. Вне групп {m.N_noise}.
					</p>
					{m.status !== "COMPLETE" && <p>{getName(m.status)}</p>}
					<details>
						<summary>Состав поставки и параметры</summary>
						<pre>{JSON.stringify(m, null, 2)}</pre>
					</details>
					{!candidate ? (
						<>
							<ul>
								{(page.rows as Candidate[]).map((c) => (
									<li key={c.id}>
										{c.state === "unconfirmed" && (
											<input
												type="checkbox"
												aria-label={"Объединить " + (c.term || c.id)}
												checked={mergeIds.includes(c.id)}
												onChange={(e) =>
													setMergeIds(
														e.target.checked
															? [...mergeIds, c.id]
															: mergeIds.filter((x) => x !== c.id),
													)
												}
											/>
										)}
										<a href={inspect(c.id)}>
											{c.term || "Производный кандидат"}
										</a>{" "}
										· {getName(c.state)} · {c.support} разных текстов,{" "}
										{c.versions} версий
									</li>
								))}
							</ul>
							{mergeIds.length > 1 && (
								<>
									<label>
										Причина объединения{" "}
										<input
											value={reason}
											onChange={(e) => setReason(e.target.value)}
											maxLength={300}
										/>
									</label>
									<button
										type="button"
										disabled={busy || !reason.trim() || mergeIds.length > 8}
										onClick={() => action("merge", mergeIds)}
									>
										Объединить выбранные кандидаты
									</button>
								</>
							)}
						</>
					) : (
						<>
							<p>
								<a href={inspect("")}>Все кандидаты результата</a>
							</p>
							<h3>{page.term || "Производный кандидат"}</h3>
							<p>
								Поддержка — {page.support} разных текстов.{" "}
								{page.support < 3 && "INSUFFICIENT_SUPPORT"}
							</p>
							<p>
								Ключ состава <code>{page.content_key}</code>
							</p>
							{page.evidence && (
								<blockquote style={{ whiteSpace: "pre-wrap" }}>
									{page.evidence.text}
								</blockquote>
							)}
							{!history && (
								<>
									<h4>Репрезентативные версии</h4>
									<ul>
										{page.representatives?.map((x) => (
											<li key={x.version_id}>
												<a href={showVersion(x.version_id)}>{x.version_id}</a> [
												{x.start}, {x.end})
											</li>
										))}
									</ul>
									<details>
										<summary>Все версии ({page.total})</summary>
										{(page.rows as Member[]).map((x) => (
											<p key={x.version_id}>
												{!terminal && page.total <= 25 && (
													<input
														type="checkbox"
														aria-label={"В первую часть " + x.version_id}
														checked={chosen.includes(x.version_id)}
														onChange={(e) =>
															setChosen(
																e.target.checked
																	? [...chosen, x.version_id]
																	: chosen.filter((y) => y !== x.version_id),
															)
														}
													/>
												)}
												<a href={showVersion(x.version_id)}>{x.version_id}</a>
											</p>
										))}
									</details>
								</>
							)}
							{page.events?.map((e) => (
								<article key={e.id}>
									<strong>{getName(e.action)}</strong> · {e.reason} ·{" "}
									{e.created_at} · {e.actor}
									{e.parents
										.filter((p) => p !== candidate)
										.map((p) => (
											<p key={p}>
												<a href={inspect(p)}>Родительская редакция {p}</a>
											</p>
										))}
									{e.aspect_id && e.aspect_version_id && (
										<p>
											<a
												href={link({
													view: "aspects",
													project: m.project_id,
													aspect: e.aspect_id,
													aspectVersion: e.aspect_version_id,
												})}
											>
												Подтверждённая редакция определения
											</a>
										</p>
									)}
								</article>
							))}
							{page.children?.map((c) => (
								<p key={c}>
									<a href={inspect(c)}>Производная редакция {c}</a>
								</p>
							))}
							{!terminal && !history && (
								<fieldset disabled={busy}>
									<legend>Решение аналитика</legend>
									<label>
										Причина решения{" "}
										<input
											value={reason}
											onChange={(e) => setReason(e.target.value)}
											maxLength={300}
										/>
									</label>
									<button
										type="button"
										disabled={!reason.trim()}
										onClick={() => action("reject", [candidate])}
									>
										Отклонить кандидата
									</button>
									{page.total <= 25 && page.total > 1 && (
										<button
											type="button"
											disabled={
												!reason.trim() ||
												!chosen.length ||
												chosen.length === page.total
											}
											onClick={() =>
												action("split", [candidate], {
													parts: [
														chosen,
														(page.rows as Member[])
															.map((x) => x.version_id)
															.filter((x) => !chosen.includes(x)),
													],
												})
											}
										>
											Разделить отмеченные и остальные версии
										</button>
									)}
									{page.total > 25 && (
										<p>
											Разделение в кабинете доступно для состава до 25 версий.
										</p>
									)}
									<p>
										<a href={link({ view: "aspects", project: m.project_id })}>
											Сравнить с каталогом определений
										</a>
									</p>
									<label>
										Сохранённая версия{" "}
										<select
											value={aspect}
											onChange={(e) => setAspect(e.target.value)}
										>
											<option value="">Выберите определение</option>
											{view.defs
												.filter((d) => !d.archived)
												.map((d) => (
													<option key={d.version_id} value={d.version_id}>
														{d.name} · {d.version_id}
													</option>
												))}
										</select>
									</label>
									<button
										type="button"
										disabled={!aspect || !reason.trim()}
										onClick={() =>
											action("confirm", [candidate], {
												aspect_id: view.defs.find(
													(d) => d.version_id === aspect,
												)?.aspect_id,
												aspect_version_id: aspect,
											})
										}
									>
										Связать с определением
									</button>
									<details>
										<summary>Создать определение и подтвердить</summary>
										<form
											onSubmit={(e) => {
												e.preventDefault();
												const f = new FormData(e.currentTarget);
												void action("confirm", [candidate], {
													definition: {
														name: f.get("name"),
														definition: f.get("definition"),
														include: [f.get("include")],
														exclude: [f.get("exclude")],
													},
												});
											}}
										>
											{(
												["name", "definition", "include", "exclude"] as const
											).map((n, i) => (
												<label key={n}>
													{
														[
															"Название",
															"Границы проблемы",
															"Включающий пример",
															"Исключающий пример",
														][i]
													}
													<textarea
														name={n}
														required
														maxLength={[120, 2000, 500, 500][i]}
													/>
												</label>
											))}
											<button disabled={!reason.trim()} type="submit">
												Сохранить новое определение
											</button>
											<p>
												Для произвольного определения классификатор PR-22
												возвращает UNSUPPORTED_DEFINITION.
											</p>
										</form>
									</details>
								</fieldset>
							)}
							<h4>Проверка выбранной истории</h4>
							{!history && (
								<p>
									Исторический снимок ещё не выбран. Новизна для бизнеса не
									установлена.
								</p>
							)}
							<label>
								UUID исторического снимка этого источника{" "}
								<input
									list="topic-periods"
									value={past}
									onChange={(e) => setPast(e.target.value)}
								/>
							</label>
							<datalist id="topic-periods">
								{view.snapshots.map((s) => (
									<option key={s} value={s} />
								))}
							</datalist>
							{past && past !== history && (
								<a href={href({ topicHistory: past })}>
									Открыть выбранную историю
								</a>
							)}
							{history && (
								<>
									<button type="button" disabled={busy} onClick={() => check()}>
										Искать аналогии в истории
									</button>
									{page.history?.map((e) => (
										<article key={e.id}>
											<strong>{getName(e.receipt.state)}</strong>
											{e.receipt.historical_period && (
												<p>
													Просмотрено {e.receipt.N_searched} из{" "}
													{e.receipt.historical_period.N}, с датой{" "}
													{e.receipt.historical_period.known_dates}. Охват{" "}
													{e.receipt.historical_period.coverage}.
												</p>
											)}
											{e.receipt.hits?.length === 0 &&
												(e.receipt.N_searched ?? 0) > 0 && (
													<p>В проверенной истории кандидаты не найдены.</p>
												)}
											{e.receipt.hits?.map((hit) => (
												<p key={hit.version_id}>
													<a href={showVersion(hit.version_id)}>
														Историческая версия {hit.version_id}
													</a>{" "}
													{!page.history?.some((v) => v.prior === e.id) &&
														[true, false].map((accept) => (
															<button
																key={String(accept)}
																type="button"
																disabled={busy}
																onClick={() =>
																	check({
																		expected_check: e.id,
																		version: hit.version_id,
																		accept,
																	})
																}
															>
																{accept
																	? "Подтвердить аналогию"
																	: "Отклонить аналогию"}
															</button>
														))}
												</p>
											))}
										</article>
									))}
								</>
							)}
						</>
					)}
					{offset > 0 && (
						<a
							href={href({
								topicPage: String(Math.max(0, offset - (history ? 5 : 25))),
							})}
						>
							Назад по страницам
						</a>
					)}
					{((history && page.history?.length === 5) ||
						(!history && offset + 25 < page.total)) && (
						<a href={href({ topicPage: String(offset + (history ? 5 : 25)) })}>
							Следующая страница
						</a>
					)}
				</>
			)}
		</section>
	);
}
