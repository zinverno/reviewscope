import { useEffect, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import "./tokens.css";
import "./cabinet.css";
import { SnapshotOverview, LegacyOverview } from "./Overview";
import { CanonicalGraph, LegacyGraph, demoGraphHref } from "./DuplicateGraph";
import { ReviewEvidence, evidenceFields } from "./ReviewEvidence";

type Space = { id: string; name: string; role: string };
type Session = { principal_id: string; workspaces: Space[] };
type Target = {
	project_id: string;
	project_name: string;
	source_id: string;
	source_label: string;
	location_id: string;
	location_name: string | null;
};
type Position = {
	position: number;
	status: string;
	reasons: string[];
	warnings: string[];
	related_position: number | null;
};
type Draft = {
	id: string;
	snapshot_id: string | null;
	fingerprint: string;
	expires_at: string;
	diagnostics: {
		summary: {
			total_input_rows: number;
			accepted: number;
			quarantined: number;
			confirmed_duplicates: number;
		};
		positions?: Position[];
	};
};
type Metric = { numerator: number; denominator: number };
type Job = {
	metrics?: {
		counts: Record<string, Metric>;
		eligibility: Record<string, Metric>;
	} | null;
	id: string;
	status: string;
	snapshot_id: string;
	reason: string | null;
	processed: number;
	total: number | null;
	stage: string;
};
const ROOT = "/api/v1";
const codes: Record<string, string> = {
	unauthenticated: "Войдите в кабинет.",
	forbidden: "Действующая роль не разрешает эту операцию.",
	not_found: "Объект или файл недоступен в выбранной области.",
	job_conflict: "Выбор изменился. Повторите предпросмотр.",
	request_too_large: "Превышен допустимый размер.",
	rate_limited: "Сейчас достигнут предел запросов. Попробуйте позже.",
	access_unavailable: "Сервис временно недоступен.",
	invalid_mapping: "Проверьте поля сопоставления.",
	unknown_column: "В файле отсутствует указанное поле.",
	unexpected_column: "Назначьте все столбцы файла.",
	empty_input: "В файле нет позиций.",
	invalid_request: "Проверьте формат, размеры и выбранную точку.",
	invalid_passport: "Проверьте паспорт источника.",
};
async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
	const res = await fetch(ROOT + path, {
		...init,
		credentials: "same-origin",
		cache: "no-store",
		headers: { "X-RS-CSRF": "1", ...init.headers },
	});
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new Error(codes[body.error] ?? "Операция недоступна.");
	}
	return res.status === 204 ? (null as T) : res.json();
}
const jsonPost = (value: unknown): RequestInit => ({
	method: "POST",
	headers: { "Content-Type": "application/json" },
	body: JSON.stringify(value),
});
function link(changes: Record<string, string>) {
	const u = new URL(window.location.href);
	if (
		["workspace", "source", "snapshot"].some(
			(k) => k in changes && changes[k] !== u.searchParams.get(k),
		)
	) {
		for (const k of [...evidenceFields, "view", "review", "offset"])
			u.searchParams.delete(k);
	}
	for (const [k, v] of Object.entries(changes)) {
		if (v) u.searchParams.set(k, v);
		else u.searchParams.delete(k);
	}
	return u.pathname + u.search;
}
const hex = (b: ArrayBuffer) =>
	[...new Uint8Array(b)].map((i) => i.toString(16).padStart(2, "0")).join("");
const status: Record<string, string> = {
	queued: "В очереди",
	running: "В работе",
	succeeded: "Завершена",
	cancelled: "Отменена",
	failed: "Ошибка",
	accepted: "Принята",
	quarantined: "В карантине",
	confirmed_duplicate: "Подтверждённый повтор",
};
const mappingDefault = JSON.stringify(
	{
		version: "pilot-synthetic-1",
		fields: Object.fromEntries(
			[
				"source",
				"source_place_id",
				"source_review_id",
				"text",
				"rating",
				"published_at",
				"observed_at",
				"source_url",
				"reviewer_id",
			].map((k) => [k, ["reviewer_id", "source_url"].includes(k) ? null : k]),
		),
	},
	null,
	2,
);
const passportDefault = JSON.stringify({ delimiter: "," }, null, 2);
type Term = {
	object_id: string | null;
	delivery_id: string | null;
	class: string;
	policy: string;
	anchor: string | null;
	days: number | null;
	expires_at: string | null;
	erasure: string | null;
};
const classLabels: Record<string, string> = {
	raw: "Исходные данные",
	configuration: "Конфигурации",
	positions: "Позиции",
	versions: "Версии",
	snapshot: "Снимки",
	temporary: "Временные файлы",
	derivatives: "Производные",
	report_export: "Отчёты и экспорт",
	diagnostics: "Диагностика",
	backup: "Резервные копии",
};
function RetentionTerms({ sourceUrl }: { sourceUrl: string }) {
	const [terms, setTerms] = useState<Term[] | null>(null),
		[offset, setOffset] = useState(0),
		[open, setOpen] = useState(false),
		[error, setError] = useState(false);
	useEffect(() => {
		let current = true;
		if (open) {
			setTerms(null);
			setError(false);
			api<Term[]>(sourceUrl + "?offset=" + offset)
				.then((v) => {
					if (current) setTerms(v);
				})
				.catch(() => {
					if (current) setError(true);
				});
		}
		return () => {
			current = false;
		};
	}, [sourceUrl, offset, open]);
	return (
		<details onToggle={(e) => setOpen(e.currentTarget.open)}>
			<summary>Сроки хранения источника</summary>
			<p>
				Исчезновение из резервных копий требует проверки
				(BACKUP_ERASURE_NOT_VERIFIED).
			</p>
			{error ? (
				<p role="alert">Условия недоступны.</p>
			) : terms ? (
				<>
					<table>
						<thead>
							<tr>
								<th>Данные</th>
								<th>Срок</th>
								<th>Истечение</th>
							</tr>
						</thead>
						<tbody>
							{terms.map((t) => (
								<tr key={JSON.stringify([t.class, t.object_id, t.delivery_id])}>
									<td>{classLabels[t.class] ?? t.class}</td>
									<td>
										{t.policy === "unset"
											? "Не установлен"
											: t.days +
												" дн. · " +
												({
													received: "от получения",
													registered: "от регистрации",
													created: "от создания",
												}[t.anchor ?? ""] ?? "")}
									</td>
									<td>
										{t.expires_at
											? new Date(t.expires_at).toLocaleDateString("ru")
											: "Неизвестно"}
										{t.erasure && " · " + t.erasure}
									</td>
								</tr>
							))}
						</tbody>
					</table>
					{offset > 0 && (
						<button type="button" onClick={() => setOffset(offset - 50)}>
							Предыдущие условия
						</button>
					)}
					{terms.length === 50 && (
						<button type="button" onClick={() => setOffset(offset + 50)}>
							Следующие условия
						</button>
					)}
				</>
			) : (
				<p role="status">Загрузка условий…</p>
			)}
		</details>
	);
}
function Cabinet() {
	const pendingJob = useRef<{ snapshot: string; key: string } | null>(null);
	const [session, setSession] = useState<Session | null>(null),
		[ready, setReady] = useState(false),
		[msg, setMsg] = useState(""),
		[busy, setBusy] = useState(false);
	const [search, setSearch] = useState(location.search);
	const query = new URLSearchParams(search);
	const space = query.has("workspace")
		? session?.workspaces.find((w) => w.id === query.get("workspace"))
		: session?.workspaces[0];
	const w = space?.id ?? "",
		workspace = "/workspaces/" + w,
		canWrite = space?.role === "owner" || space?.role === "analyst";
	const source = query.get("source") ?? "",
		delivery = query.get("delivery") ?? "",
		run = query.get("run") ?? "",
		snapshot = query.get("snapshot") ?? "";
	const [targets, setTargets] = useState<Target[]>([]),
		[drafts, setDrafts] = useState<Draft[]>([]),
		[loadedDraft, setDraft] = useState<(Draft & { scope: string }) | null>(
			null,
		);
	const [runs, setRuns] = useState<Job[]>([]),
		[job, setJob] = useState<Job | null>(null),
		[success, setSuccess] = useState<Job | null>(null);
	const [locs, setLocs] = useState<string[]>([]),
		[file, setFile] = useState<File | null>(null),
		[fmt, setFmt] = useState("csv");
	const [mapping, setMapping] = useState(mappingDefault),
		[passport, setPassport] = useState(passportDefault),
		[valid, setValid] = useState(false),
		[page, setPage] = useState(0);
	const [login, setLogin] = useState(""),
		[password, setPassword] = useState("");
	const sourcePath = workspace + "/sources/" + source + "/uploads";
	const draft =
		loadedDraft?.scope === sourcePath && loadedDraft.id === delivery
			? loadedDraft
			: null;
	const go = (v: Record<string, string>) => {
		history.pushState(null, "", link(v));
		setSearch(location.search);
	};
	useEffect(() => {
		const back = () => setSearch(location.search);
		addEventListener("popstate", back);
		return () => removeEventListener("popstate", back);
	}, []);
	useEffect(() => {
		api<Session>("/session")
			.then(setSession)
			.catch(() => {})
			.finally(() => setReady(true));
	}, []);
	useEffect(() => {
		let current = true;
		setTargets([]);
		setRuns([]);
		if (w) {
			if (canWrite)
				api<Target[]>(workspace + "/upload-targets")
					.then((v) => {
						if (current) setTargets(v);
					})
					.catch((e) => {
						if (current) setMsg(e.message);
					});
			api<Job[]>(workspace + "/runs")
				.then((v) => {
					if (current) setRuns(v);
				})
				.catch((e) => {
					if (current) setMsg(e.message);
				});
		}
		return () => {
			current = false;
		};
	}, [workspace, w, canWrite]);
	useEffect(() => {
		let current = true;
		setDrafts([]);
		setPage(0);
		if (canWrite && source) {
			api<Draft[]>(sourcePath)
				.then((v) => {
					if (current) setDrafts(v);
				})
				.catch((e) => {
					if (current) setMsg(e.message);
				});
			if (delivery)
				api<Draft>(sourcePath + "/" + delivery)
					.then((v) => {
						if (current) setDraft({ ...v, scope: sourcePath });
					})
					.catch((e) => {
						if (current) setMsg(e.message);
					});
		}
		return () => {
			current = false;
		};
	}, [sourcePath, source, delivery, canWrite]);
	useEffect(() => {
		if (sourcePath) {
			setFile(null);
			setLocs([]);
		}
	}, [sourcePath]);
	useEffect(() => {
		let current = true;
		let timer: ReturnType<typeof setTimeout>;
		setJob(null);
		setSuccess(null);
		async function poll() {
			try {
				let running = false;
				if (run) {
					const v = await api<Job>(workspace + "/runs/" + run + "/status");
					if (!current) return;
					if (snapshot && v.snapshot_id !== snapshot)
						throw new Error("Проверка недоступна для выбранного снимка.");
					setJob(v);
					setRuns((before) => [
						v,
						...before.filter((item) => item.id !== v.id),
					]);
					running = ["queued", "running"].includes(v.status);
				}
				if (snapshot) {
					const res = await fetch(
						ROOT + workspace + "/snapshots/" + snapshot + "/summary-metrics",
						{ cache: "no-store" },
					);
					if (!current) return;
					if (res.ok) setSuccess(await res.json());
					else if (res.status === 404) setSuccess(null);
					else throw new Error("Статус недоступен.");
				}
				if (current && running) timer = setTimeout(poll, 1500);
			} catch (e) {
				if (current) {
					setJob(null);
					setSuccess(null);
					setMsg((e as Error).message);
				}
			}
		}
		if (w && (run || snapshot)) void poll();
		return () => {
			current = false;
			clearTimeout(timer);
		};
	}, [run, snapshot, workspace, w]);
	async function action(fn: () => Promise<void>) {
		setBusy(true);
		setMsg("");
		try {
			await fn();
		} catch (e) {
			setMsg((e as Error).message);
		} finally {
			setBusy(false);
		}
	}
	async function signIn(e: FormEvent) {
		e.preventDefault();
		await action(async () => {
			await api("/session/login", jsonPost({ login, password }));
			setPassword("");
			setSession(await api<Session>("/session"));
		});
	}
	async function load(confirm: boolean) {
		if (!file || !locs.length)
			throw new Error("Выберите файл и хотя бы одну точку.");
		const parts = [new Blob([mapping]), new Blob([passport]), file];
		if (file.size === 0) throw new Error("В файле нет позиций.");
		if (
			file.size > 4194304 ||
			parts.slice(0, 2).some((v) => v.size === 0 || v.size > 32768)
		)
			throw new Error("Файл — до 4 МиБ, каждая конфигурация — до 32 КиБ.");
		const id = delivery || crypto.randomUUID();
		const headers: Record<string, string> = {
			"Content-Type": "application/octet-stream",
			"X-RS-Parts": parts.map((p) => p.size).join(","),
			"X-RS-Locations": [...locs].sort().join(","),
			"X-RS-Format": fmt,
			"X-RS-File-Sha256": hex(
				await crypto.subtle.digest("SHA-256", await file.arrayBuffer()),
			),
		};
		if (confirm && draft) headers["X-RS-Fingerprint"] = draft.fingerprint;
		await api(
			sourcePath + "/eligibility",
			jsonPost({
				checksum: headers["X-RS-File-Sha256"],
				locations: [...locs].sort(),
			}),
		);
		const current = location.search;
		const v = await api<Draft>(
			sourcePath + "/" + id + "/" + (confirm ? "confirm" : "preview"),
			{ method: "POST", headers, body: new Blob(parts) },
		);
		if (current !== location.search) return;
		if (id !== delivery || v.snapshot_id) {
			go({
				workspace: w,
				source,
				delivery: id,
				snapshot: v.snapshot_id ?? "",
				run: "",
			});
		}
		setDraft({ ...v, scope: sourcePath });
		setPage(0);
		setValid(!confirm);
		setDrafts(await api<Draft[]>(sourcePath));
	}
	async function start() {
		const id =
			pendingJob.current?.snapshot === snapshot
				? pendingJob.current.key
				: crypto.randomUUID();
		pendingJob.current = { snapshot, key: id };
		const result = await api<Job>(
			workspace + "/runs",
			jsonPost({
				snapshot_id: snapshot,
				idempotency_key: id,
				job_kind: "snapshot_integrity_and_summary_v1",
				engine_version: "1",
			}),
		);
		pendingJob.current = null;
		go({ run: result.id });
	}
	const locations = targets.filter((t) => t.source_id === source);
	const projects = [...new Map(targets.map((t) => [t.project_id, t])).values()];
	const project =
		query.get("project") ??
		locations[0]?.project_id ??
		projects[0]?.project_id ??
		"";
	const sources = [
		...new Map(
			targets
				.filter((t) => t.project_id === project)
				.map((t) => [t.source_id, t]),
		).values(),
	];
	const selected = draft?.diagnostics.summary;
	const positions = draft?.diagnostics.positions ?? [];
	return (
		<main className="cab">
			<header>
				<strong>ReviewScope</strong>
				<span>Локальный кабинет</span>
				{session && (
					<button
						type="button"
						onClick={() =>
							void action(async () => {
								await api("/session/logout", { method: "POST" });
								location.replace("/cabinet/");
							})
						}
					>
						Выйти
					</button>
				)}
			</header>
			<h1>
				{["graph", "legacy-graph"].includes(query.get("view") ?? "")
					? "Граф повторов"
					: query.get("view") === "reviews"
						? "Панель доказательств"
						: query.get("view") === "overview" || query.get("view") === "legacy"
							? "Обзор данных"
							: "Проверка снимка и пригодности данных"}
			</h1>
			{msg && <p role="alert">{msg}</p>}
			{!ready ? (
				<p role="status">Загрузка…</p>
			) : !session ? (
				<form onSubmit={signIn}>
					<h2>Вход в рабочую область</h2>
					<label>
						Учётная запись
						<input
							autoComplete="username"
							required
							value={login}
							onChange={(e) => setLogin(e.target.value)}
						/>
					</label>
					<label>
						Пароль
						<input
							type="password"
							autoComplete="current-password"
							required
							value={password}
							onChange={(e) => setPassword(e.target.value)}
						/>
					</label>
					<button className="primary" disabled={busy} type="submit">
						Войти
					</button>
				</form>
			) : (
				<>
					<label>
						Рабочая область
						<select
							aria-label="Рабочая область"
							value={w}
							onChange={(e) =>
								go({
									workspace: e.target.value,
									project: "",
									source: "",
									delivery: "",
									snapshot: "",
									run: "",
								})
							}
						>
							{session.workspaces.map((v) => (
								<option key={v.id} value={v.id}>
									{v.name} · {v.role}
								</option>
							))}
						</select>
					</label>
					{!w ? (
						<p>Рабочая область недоступна.</p>
					) : query.get("view") === "graph" ? (
						snapshot ? (
							<CanonicalGraph
								key={workspace + search}
								workspace={w}
								snapshot={snapshot}
								search={search}
								api={api}
								link={link}
							/>
						) : (
							<p>Выберите снимок.</p>
						)
					) : query.get("view") === "legacy-graph" ? (
						<LegacyGraph key={search} search={search} />
					) : query.get("view") === "overview" ? (
						snapshot ? (
							<SnapshotOverview
								key={workspace + search}
								workspace={w}
								snapshot={snapshot}
								search={search}
								canRead={canWrite}
								api={api}
								link={link}
							/>
						) : (
							<p>Выберите снимок.</p>
						)
					) : query.get("view") === "legacy" ? (
						<LegacyOverview search={search} />
					) : query.get("view") === "reviews" ? (
						snapshot && canWrite ? (
							<ReviewEvidence
								workspace={w}
								snapshot={snapshot}
								search={search}
								targets={targets}
								api={api}
								link={link}
							/>
						) : (
							<p>Нет доступа к отзывам выбранного снимка.</p>
						)
					) : (
						<>
							{canWrite ? (
								<section>
									<h2>Поставка</h2>
									<p>
										Разрешены только заранее проверенные синтетические CSV и
										JSON. До 4 МиБ и 10 000 позиций. Покрытие неизвестно.
										Реальные источники закрыты (BLOCKED_REAL_SOURCE).
									</p>
									{projects.length === 0 ? (
										<p>Нет разрешённых источников.</p>
									) : (
										<>
											<div className="pair">
												<label>
													Проект
													<select
														aria-label="Проект"
														value={project}
														onChange={(e) =>
															go({
																workspace: w,
																project: e.target.value,
																source: "",
																delivery: "",
																snapshot: "",
																run: "",
															})
														}
													>
														{projects.map((t) => (
															<option key={t.project_id} value={t.project_id}>
																{t.project_name}
															</option>
														))}
													</select>
												</label>
												<label>
													Источник
													<select
														aria-label="Источник"
														value={source}
														onChange={(e) =>
															go({
																workspace: w,
																project,
																source: e.target.value,
																delivery: "",
																snapshot: "",
																run: "",
															})
														}
													>
														<option value="">Выберите источник</option>
														{sources.map((t) => (
															<option key={t.source_id} value={t.source_id}>
																{t.source_label}
															</option>
														))}
													</select>
												</label>
											</div>
											{source && (
												<>
													<RetentionTerms
														key={sourcePath}
														sourceUrl={
															workspace + "/sources/" + source + "/retention"
														}
													/>
													<fieldset>
														<legend>Разрешённые точки</legend>
														{locations.map((t) => (
															<label className="check" key={t.location_id}>
																<input
																	type="checkbox"
																	checked={locs.includes(t.location_id)}
																	onChange={(e) => {
																		setLocs(
																			e.target.checked
																				? [...locs, t.location_id]
																				: locs.filter(
																						(l) => l !== t.location_id,
																					),
																		);
																		setValid(false);
																	}}
																/>
																{t.location_name ?? t.location_id}
															</label>
														))}
													</fieldset>
													<div className="pair">
														<label>
															Файл
															<input
																key={sourcePath}
																type="file"
																accept=".csv,.json"
																onChange={(e) => {
																	setFile(e.target.files?.[0] ?? null);
																	setValid(false);
																}}
															/>
														</label>
														<label>
															Формат
															<select
																aria-label="Формат"
																value={fmt}
																onChange={(e) => {
																	setFmt(e.target.value);
																	setValid(false);
																}}
															>
																<option value="csv">CSV</option>
																<option value="json">JSON</option>
															</select>
														</label>
													</div>
													<details>
														<summary>Сопоставление и паспорт</summary>
														<label>
															Mapping (JSON)
															<textarea
																aria-label="Mapping (JSON)"
																value={mapping}
																onChange={(e) => {
																	setMapping(e.target.value);
																	setValid(false);
																}}
															/>
														</label>
														<label>
															Паспорт (JSON)
															<textarea
																aria-label="Паспорт (JSON)"
																value={passport}
																onChange={(e) => {
																	setPassport(e.target.value);
																	setValid(false);
																}}
															/>
														</label>
													</details>
													<button
														type="button"
														className="primary"
														disabled={
															busy ||
															!file ||
															!locs.length ||
															!!draft?.snapshot_id
														}
														onClick={() => void action(() => load(false))}
													>
														Предпросмотр
													</button>
													{delivery && (
														<a
															href={link({
																delivery: "",
																snapshot: "",
																run: "",
															})}
														>
															Новая поставка
														</a>
													)}
													<details>
														<summary>
															Сохранённые поставки ({drafts.length})
														</summary>
														{drafts.map((v) => (
															<p key={v.id}>
																<a
																	href={link({
																		delivery: v.id,
																		snapshot: v.snapshot_id ?? "",
																		run: "",
																	})}
																>
																	{v.snapshot_id ? "Снимок" : "Предпросмотр"} ·{" "}
																	{v.id}
																</a>
															</p>
														))}
													</details>
												</>
											)}
										</>
									)}
								</section>
							) : (
								<p>
									Просмотр доступных заданий. Загрузка доступна владельцу и
									аналитику.
								</p>
							)}
							{canWrite && draft && selected && (
								<section>
									<h2>Диагностика поставки</h2>
									<dl>
										{Object.entries({
											Позиций: selected.total_input_rows,
											Принято: selected.accepted,
											Карантин: selected.quarantined,
											"Подтверждённых повторов": selected.confirmed_duplicates,
										}).map(([k, v]) => (
											<div key={k}>
												<dt>{k}</dt>
												<dd>{v}</dd>
											</div>
										))}
									</dl>
									<details>
										<summary>Позиционный журнал</summary>
										<table>
											<thead>
												<tr>
													<th>Позиция</th>
													<th>Итог</th>
													<th>Причины и предупреждения</th>
												</tr>
											</thead>
											<tbody>
												{positions.slice(page * 30, page * 30 + 30).map((r) => (
													<tr key={r.position}>
														<td>{r.position}</td>
														<td>{status[r.status]}</td>
														<td>
															{[...r.reasons, ...r.warnings].join(", ") || "—"}{" "}
															{r.related_position && "· №" + r.related_position}
														</td>
													</tr>
												))}
											</tbody>
										</table>
										<button
											type="button"
											disabled={!page}
											onClick={() => setPage(page - 1)}
										>
											Назад
										</button>
										<button
											type="button"
											disabled={(page + 1) * 30 >= positions.length}
											onClick={() => setPage(page + 1)}
										>
											Далее
										</button>
									</details>
									{draft.snapshot_id ? (
										<p>
											Снимок сохранён.{" "}
											<a href={link({ snapshot: draft.snapshot_id })}>
												Открыть снимок
											</a>
										</p>
									) : (
										<>
											<p>
												Предпросмотр доступен до{" "}
												{new Date(draft.expires_at).toLocaleString("ru")}. Для
												подтверждения после возврата выберите файл и проверьте
												его снова.
											</p>
											<button
												type="button"
												disabled={busy || !valid}
												onClick={() => void action(() => load(true))}
											>
												Подтвердить и создать снимок
											</button>
										</>
									)}
								</section>
							)}
							<section>
								{snapshot && canWrite && (
									<p>
										<a href={link({ view: "reviews", review: "", offset: "" })}>
											Отзывы выбранного снимка
										</a>
									</p>
								)}
								{snapshot && (
									<p>
										<a href={link({ view: "overview" })}>
											Обзор выбранного снимка
										</a>
									</p>
								)}
								<a href="/cabinet/?view=legacy">Обзор публичной синтетики</a>
								{snapshot && (
									<p>
										<a href={link({ view: "graph" })}>
											Повторы выбранного снимка
										</a>
									</p>
								)}
								<p>
									<a href={demoGraphHref()}>
										Исследовать повторы публичной синтетики
									</a>
								</p>
								<h2>Фоновые проверки</h2>
								<p>
									Доступна проверка целостности и пригодности. Анализ жалоб пока
									недоступен (ANALYSIS_NOT_AVAILABLE).
								</p>
								{snapshot && canWrite && (
									<button
										type="button"
										className="primary"
										disabled={
											busy ||
											job?.status === "running" ||
											job?.status === "queued"
										}
										onClick={() => void action(start)}
									>
										{job ? "Повторить проверку" : "Запустить проверку"}
									</button>
								)}
								{job && (
									<p role="status">
										{status[job.status]} · {job.stage} · Обработано{" "}
										{job.processed} из {job.total ?? "неизвестно"} {job.reason}
									</p>
								)}
								{canWrite &&
									job &&
									["queued", "running"].includes(job.status) && (
										<button
											type="button"
											onClick={() =>
												void action(async () =>
													setJob(
														await api<Job>(
															workspace + "/runs/" + run + "/cancel",
															{ method: "POST" },
														),
													),
												)
											}
										>
											Отменить проверку
										</button>
									)}
								{success && (
									<p>
										<a href={link({ run: success.id })}>
											Последняя успешная проверка
										</a>{" "}
										· обработано {success.processed}
										{success.metrics && (
											<span>
												{" "}
												·{" "}
												<a
													href={link({
														view: "reviews",
														snapshot: success.snapshot_id,
														...Object.fromEntries(
															evidenceFields.map((k) => [k, ""]),
														),
														has_text: "true",
														review: "",
														offset: "",
													})}
												>
													С пригодным текстом
												</a>{" "}
												{success.metrics.eligibility.usable_text.numerator} из{" "}
												{success.metrics.eligibility.usable_text.denominator}.
												Карантин {success.metrics.counts.quarantined.numerator}{" "}
												из {success.metrics.counts.quarantined.denominator}.
											</span>
										)}
									</p>
								)}
								<h3>История</h3>
								{runs.length ? (
									runs.map((v) => (
										<p key={v.id}>
											<a href={link({ run: v.id, snapshot: v.snapshot_id })}>
												{status[v.status] ?? v.status} · {v.id}
											</a>
										</p>
									))
								) : (
									<p>Сохранённых заданий пока нет.</p>
								)}
							</section>
						</>
					)}
				</>
			)}
		</main>
	);
}
createRoot(document.getElementById("root")!).render(<Cabinet />);
