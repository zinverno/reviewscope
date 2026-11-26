import {
	useEffect,
	useRef,
	useState,
	type FormEvent,
	type ReactNode,
} from "react";
import { createRoot } from "react-dom/client";
import "./tokens.css";
import "./cabinet.css";
import { SnapshotOverview, LegacyOverview } from "./Overview";
import { TopicDiscovery } from "./TopicDiscovery";
import { TypedRelations } from "./TypedRelations";
import { CanonicalGraph, LegacyGraph, demoGraphHref } from "./DuplicateGraph";
import { PublicSignals, CanonicalSignals, signalKind } from "./Signals";
import { PublicReviewers } from "./ReviewerContext";
import {
	SnapshotReviewerContext,
	privateUuid,
} from "./SnapshotReviewerContext";
import { AspectDecisions } from "./AspectDecisions";
import { Monitors } from "./Monitors";
import { Aspects } from "./Aspects";
import { CandidateSearch } from "./CandidateSearch";
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
	job_kind?: string;
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
class AccessError extends Error {
	constructor(
		readonly status: number,
		message: string,
	) {
		super(message);
	}
}
async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
	const res = await fetch(ROOT + path, {
		...init,
		credentials: "same-origin",
		cache: "no-store",
		headers: { "X-RS-CSRF": "1", ...init.headers },
	});
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new AccessError(
			res.status,
			codes[body?.error] ?? "Операция недоступна.",
		);
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
		"view" in changes ||
		["workspace", "project", "source"].some(
			(k) => k in changes && changes[k] !== u.searchParams.get(k),
		)
	) {
		for (const k of [
			"monitor",
			"monitorVersion",
			"monitorRun",
			"monitorPage",
			"deliveryPage",
			"historyPage",
			"deltaPage",
			"versionPage",
		])
			u.searchParams.delete(k);
	}
	if (
		"view" in changes ||
		["source", "workspace", "snapshot"].some(
			(k) => k in changes && changes[k] !== u.searchParams.get(k),
		)
	) {
		for (const k of [
			"relation",
			"topicResult",
			"topicCandidate",
			"topicDigest",
			"topicHistory",
			"topicEvidence",
			"topicPage",
			"topicRun",
			"topicRevision",
			"relationDigest",
			"left",
			"right",
			"pairPage",
			"pairFilter",
			"relationRun",
			"relationRevision",
		])
			u.searchParams.delete(k);
	}
	if (
		changes.view === "decisions" ||
		(changes.view === "reviews" && u.searchParams.get("view") === "decisions")
	) {
		for (const k of [...evidenceFields, "offset", "review"])
			u.searchParams.delete(k);
	}
	if (
		"view" in changes ||
		["source", "workspace", "snapshot", "aspectVersion"].some(
			(k) => k in changes && changes[k] !== u.searchParams.get(k),
		)
	)
		for (const k of [
			"decisionRun",
			"decisionReview",
			"decisionOutcome",
			"decisionOffset",
		])
			u.searchParams.delete(k);
	if (
		"view" in changes ||
		[
			"workspace",
			"project",
			"source",
			"snapshot",
			"aspect",
			"aspectVersion",
			"decisionRun",
		].some((k) => k in changes && changes[k] !== u.searchParams.get(k)) ||
		changes.human === ""
	) {
		for (const k of [
			"human",
			"reviewMode",
			"reviewState",
			"reviewOutcome",
			"reviewPlan",
			"reviewPage",
			"reviewSubpage",
			"reviewResult",
		])
			u.searchParams.delete(k);
	}
	if ("candidateRun" in changes || "view" in changes)
		u.searchParams.delete("rerankRun");
	if (
		["workspace", "source", "snapshot"].some(
			(k) => k in changes && changes[k] !== u.searchParams.get(k),
		)
	) {
		for (const k of [
			...evidenceFields,
			"view",
			"review",
			"offset",
			"signalPlace",
			"signalRating",
			"signalPage",
			"aspect",
			"aspectVersion",
			"aspectPage",
			"proposal",
			"candidateRun",
			"candidateSource",
			"rerankRun",
			"fragment",
			"fromSignals",
			"contextSource",
			"contextFrom",
			"authorPage",
			"authorVersion",
		])
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
function WorkspacePanel({
	spaces,
	selected,
	connection,
	onSelect,
	onRefresh,
	children,
}: {
	spaces: Space[];
	selected: string;
	connection: "checking" | "ready" | "denied" | "retrying";
	onSelect: (workspace: string) => void;
	onRefresh: () => void;
	children: ReactNode;
}) {
	return (
		<>
			<label>
				Рабочая область
				<select
					aria-label="Рабочая область"
					value={selected}
					onChange={(e) => onSelect(e.target.value)}
				>
					{spaces.map((space) => (
						<option key={space.id} value={space.id}>
							{space.name} · {space.role}
						</option>
					))}
				</select>
			</label>
			{connection === "ready" ? (
				children
			) : (
				<section aria-label="Состояние соединения">
					<p role="status">
						{connection === "denied"
							? "Проверка недоступна в выбранной области."
							: connection === "retrying"
								? "Связь прервана. Повторяем проверку доступа…"
								: "Проверяем доступ к заданию…"}
					</p>
					{connection === "denied" && (
						<button type="button" onClick={onRefresh}>
							Обновить статус
						</button>
					)}
				</section>
			)}
		</>
	);
}
function Cabinet() {
	const pendingJob = useRef<{ scope: string; key: string } | null>(null);
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
	const [historyPage, setHistoryPage] = useState<{
		workspace: string;
		rows: Job[];
	} | null>(null);
	const runs = historyPage?.workspace === workspace ? historyPage.rows : [];
	const principal = session?.principal_id;
	const [revision, setRevision] = useState(0);
	const signalView = query.get("view") === "signals";
	const candidateSource = query.get("candidateSource") || source;
	const privateReviewer = query.get("view") === "reviewer-context";
	const reviewerView =
		privateReviewer || query.get("view") === "public-reviewers";
	const fromSignalResult =
		query.get("contextFrom") === "signals" || query.get("fromSignals") === "1";
	const watch =
		!!w &&
		(!query.get("view") ||
			signalView ||
			(privateReviewer && privateUuid(run) && privateUuid(snapshot))) &&
		!!(run || snapshot);
	const scope = JSON.stringify([
		session?.principal_id,
		w,
		source,
		snapshot,
		run,
		revision,
		signalView,
	]);
	const [observation, setObservation] = useState<{
		scope: string;
		state: "ready" | "retrying" | "denied";
		job?: Job | null;
		success?: Job | null;
	} | null>(null);
	const visible = watch && observation?.scope === scope ? observation : null;
	const job = visible?.job,
		success = visible?.success;
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
		setHistoryPage(null);
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
					if (current) setHistoryPage({ workspace, rows: v });
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
		if (canWrite && source && !reviewerView) {
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
	}, [sourcePath, source, delivery, canWrite, reviewerView]);
	useEffect(() => {
		if (sourcePath) {
			setFile(null);
			setLocs([]);
		}
	}, [sourcePath]);
	useEffect(() => {
		if (!watch) return;
		const controller = new AbortController();
		let timer: ReturnType<typeof setTimeout>;
		let backoff = 1500;
		let revalidate = true;
		async function poll() {
			const init = {
				signal: AbortSignal.any([
					controller.signal,
					AbortSignal.timeout(12000),
				]),
			};
			try {
				const job = run
					? await api<Job>(workspace + "/runs/" + run + "/status", init)
					: null;
				const id = snapshot || job?.snapshot_id;
				if (!id || (job && (job.id !== run || job.snapshot_id !== id)))
					throw new AccessError(404, codes.not_found);
				const path = workspace + "/snapshots/" + id;
				let success: Job | null = null;
				try {
					success = await api<Job>(path + "/summary-metrics", init);
				} catch (error) {
					if (!(error instanceof AccessError && error.status === 404))
						throw error;
					// A missing summary also occurs before the first successful run.
				}
				const target = await api<{ id: string; source_id: string }>(path, init);
				if (
					target.id !== id ||
					(source && target.source_id !== source) ||
					(success &&
						(success.snapshot_id !== id || success.status !== "succeeded"))
				)
					throw new AccessError(404, codes.not_found);
				if (
					job?.status === "succeeded" &&
					job.job_kind === "snapshot_integrity_and_summary_v1" &&
					!success
				)
					throw new AccessError(503, codes.access_unavailable);
				const current = revalidate
					? await api<Session>("/session", init)
					: null;
				if (controller.signal.aborted) return;
				if (current) {
					if (current.principal_id !== principal) {
						setSession(null);
						return;
					}
					if (!current.workspaces.some((v) => v.id === w))
						throw new AccessError(404, codes.not_found);
					setSession(current);
				}
				revalidate = false;
				setObservation({ scope, state: "ready", job, success });
				if (job)
					setHistoryPage((before) => ({
						workspace,
						rows: [
							job,
							...(before?.workspace === workspace ? before.rows : []).filter(
								(v) => v.id !== job.id,
							),
						],
					}));
				backoff = 1500;
				if (job && ["queued", "running"].includes(job.status))
					timer = setTimeout(poll, backoff);
			} catch (error) {
				if (controller.signal.aborted) return;
				if (error instanceof AccessError && error.status === 401) {
					setSession(null);
					return;
				}
				revalidate = true;
				const denied =
					error instanceof AccessError &&
					error.status < 500 &&
					![408, 429].includes(error.status);
				setObservation({ scope, state: denied ? "denied" : "retrying" });
				if (!denied) {
					timer = setTimeout(poll, backoff);
					backoff = Math.min(backoff * 2, 15000);
				}
			}
		}
		void poll();
		return () => {
			controller.abort();
			clearTimeout(timer);
		};
	}, [watch, scope, run, snapshot, workspace, source, principal, w]);
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
	async function start(kind = "snapshot_integrity_and_summary_v1") {
		const requestScope = JSON.stringify([principal, w, source, snapshot, kind]);
		const address = location.href;
		const id =
			pendingJob.current?.scope === requestScope
				? pendingJob.current.key
				: crypto.randomUUID();
		pendingJob.current = { scope: requestScope, key: id };
		const result = await api<Job>(
			workspace + "/runs",
			jsonPost({
				snapshot_id: snapshot,
				idempotency_key: id,
				job_kind: kind,
				engine_version: "1",
			}),
		);
		if (location.href !== address) return;
		pendingJob.current = null;
		go({ run: result.id });
	}
	async function cancelRun() {
		const address = location.href;
		await api<Job>(workspace + "/runs/" + run + "/cancel", { method: "POST" });
		if (location.href === address) setRevision((v) => v + 1);
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
				{privateReviewer || query.get("view") === "public-reviewers"
					? "Контекст автора"
					: ["graph", "legacy-graph"].includes(query.get("view") ?? "")
						? "Граф повторов"
						: signalView || query.get("view") === "public-signals"
							? "Сигналы и оценки"
							: query.get("view") === "reviews"
								? "Панель доказательств"
								: query.get("view") === "overview" ||
										query.get("view") === "legacy"
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
				<WorkspacePanel
					spaces={session.workspaces}
					selected={w}
					connection={watch ? (visible?.state ?? "checking") : "ready"}
					onRefresh={() => setRevision((v) => v + 1)}
					onSelect={(workspace) =>
						go({
							workspace,
							...(query.get("view") === "aspects"
								? { view: "aspects", proposal: query.get("proposal") || "" }
								: {}),
							project: "",
							source: "",
							delivery: "",
							snapshot: "",
							run: "",
						})
					}
				>
					{!w ? (
						<p>Рабочая область недоступна.</p>
					) : query.get("view") === "public-reviewers" ? (
						<PublicReviewers key={search} search={search} />
					) : privateReviewer ? (
						snapshot &&
						canWrite &&
						(!run
							? !fromSignalResult
							: job?.id === run &&
								job.status === "succeeded" &&
								(!fromSignalResult || job.job_kind === signalKind)) ? (
							<SnapshotReviewerContext
								key={workspace + search}
								workspace={w}
								snapshot={snapshot}
								search={search}
								api={api}
								link={link}
							/>
						) : (
							<p role="alert">История выбранного снимка недоступна.</p>
						)
					) : query.get("view") === "public-signals" ? (
						<PublicSignals key={search} search={search} />
					) : signalView ? (
						canWrite && source && snapshot ? (
							<CanonicalSignals
								key={scope + search + (job?.status ?? "")}
								search={search}
								workspace={w}
								source={source}
								snapshot={snapshot}
								run={run}
								job={job}
								api={api}
								link={link}
								busy={busy}
								start={() => void action(() => start(signalKind))}
								cancel={() => void action(cancelRun)}
							/>
						) : (
							<p>Нет доступа к сигналам выбранного снимка.</p>
						)
					) : query.get("view") === "topic-discovery" ? (
						canWrite && snapshot && source ? (
							<TopicDiscovery
								key={workspace + search}
								workspace={w}
								source={source}
								snapshot={snapshot}
								api={api}
								link={link}
								search={search}
							/>
						) : (
							<p>Выберите доступный источник и снимок.</p>
						)
					) : query.get("view") === "relations" ? (
						<TypedRelations
							key={workspace + search}
							workspace={w}
							source={source}
							snapshot={snapshot}
							api={api}
							link={link}
							search={search}
						/>
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
						<LegacyOverview search={search} workspace={w} />
					) : query.get("view") === "monitors" ? (
						canWrite && project && (source || sources[0]) ? (
							<Monitors
								key={workspace + search}
								workspace={w}
								project={project}
								source={source || sources[0].source_id}
								api={api}
								link={link}
								search={search}
							/>
						) : (
							<p>Выберите допущенный источник.</p>
						)
					) : query.get("view") === "aspects" ? (
						canWrite ? (
							<Aspects
								key={workspace + project + search}
								workspace={w}
								project={project}
								projects={projects}
								api={api}
								link={link}
								search={search}
							/>
						) : (
							<p>Нет доступа к определениям.</p>
						)
					) : query.get("view") === "decisions" ? (
						snapshot &&
						canWrite &&
						candidateSource &&
						(!source || source === candidateSource) ? (
							<AspectDecisions
								key={workspace + search}
								workspace={w}
								source={candidateSource}
								snapshot={snapshot}
								search={search}
								api={api}
								link={link}
							/>
						) : (
							<p>Оценка недоступна в выбранной области.</p>
						)
					) : query.get("view") === "candidates" ? (
						snapshot &&
						canWrite &&
						candidateSource &&
						(!source || source === candidateSource) ? (
							<CandidateSearch
								project={
									targets.find((t) => t.source_id === candidateSource)
										?.project_id || ""
								}
								key={workspace + search}
								workspace={w}
								source={candidateSource}
								snapshot={snapshot}
								search={search}
								api={api}
								link={link}
							/>
						) : (
							<p>Поиск недоступен.</p>
						)
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
									{project && (source || sources[0]) && (
										<p>
											<a
												href={link({
													view: "monitors",
													project,
													source: source || sources[0].source_id,
												})}
											>
												Сохранённые мониторы
											</a>
										</p>
									)}
									{project && (
										<a href={link({ view: "aspects", project })}>
											Определения аспектов проекта
										</a>
									)}
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
										{canWrite && source && (
											<>
												<a href={link({ view: "topic-discovery" })}>
													Кандидаты тем выбранного снимка
												</a>
												{" · "}
											</>
										)}
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
											onClick={() => void action(cancelRun)}
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
				</WorkspacePanel>
			)}
		</main>
	);
}
createRoot(document.getElementById("root")!).render(<Cabinet />);
