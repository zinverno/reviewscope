import { useEffect, useRef, useState } from "react";
import type { Api, Detail } from "./Aspects";

type Page<T> = { rows: T[]; total: number; offset: number };
type Aspect = {
	aspect_id: string;
	version_id: string;
	name: string;
	archived: boolean;
};
type Spec = {
	name: string;
	aspect_id: string;
	aspect_version_id: string;
	profile: string;
	threshold_set: string;
	event: { threshold: number; minimum: number };
};
type Overview = {
	monitor_id: string;
	version_id: string;
	version_no: number;
	archived: boolean;
	state: string;
	payload: Spec;
	last_success_at: string | null;
	last_attempt: { at: string; status: string; reason: string | null } | null;
	latest_snapshot_id: string | null;
};
type History = {
	run_id: string;
	version_id: string;
	snapshot_id: string;
	status: string;
	parent: string | null;
	decision_run_id: string;
	reason: string | null;
};
type Run = History & {
	stage: string;
	processed: number;
	total: number | null;
	source_id: string;
	file_sha256: string;
	registered_at: string;
	coverage_passport: unknown;
	observed_at: unknown;
	receipt: null | {
		parent_snapshot_id: string | null;
		change_state: string;
		reused_eligible: number;
		computed_eligible: number;
		accepted: number;
		eligible: number;
		excluded_blank: number;
		categories: Record<string, number>;
	};
};
type Snapshot = {
	previous_run_id: string | null;
	id: string;
	registered_at: string;
	coverage: string | null;
	accepted_count: number;
};
type Delta = {
	identity_id: string;
	category: string;
	previous_version_id: string | null;
	target_version_id: string | null;
};
type Data = {
	catalog?: Page<{ monitor_id: string; name: string; archived: boolean }>;
	aspects: Aspect[];
	aspectCount: number;
	current?: Overview;
	versions?: Page<{ version_id: string; version_no: number }>;
	snapshots?: Page<Snapshot>;
	history?: Page<History>;
	run?: Run;
	delta?: Page<Delta>;
};
type Props = {
	workspace: string;
	project: string;
	source: string;
	search: string;
	api: Api;
	link: (p: Record<string, string>) => string;
};
const states: Record<string, string> = {
	NO_SNAPSHOT: "Нет допущенной поставки",
	NEW_DELIVERY_AVAILABLE: "Доступна новая поставка",
	NO_NEW_DELIVERY: "Новой поставки нет",
	NO_VERSION_CHANGES: "Состав версий не изменился",
	ARCHIVED: "В архиве",
	UNSUPPORTED_DEFINITION: "Определение не поддерживается",
	SUPERSEDED_VERSION: "Историческая редакция",
	ASPECT_ARCHIVED: "Аспект в архиве",
	queued: "В очереди",
	running: "Выполняется",
	succeeded: "Завершён",
	failed: "Ошибка",
	cancelled: "Отменён",
	unchanged_version: "Та же версия",
	changed_version: "Изменённая версия",
	newly_observed: "Впервые в этой паре поставок",
	not_observed_in_target: "Отсутствует в новой поставке",
	unlinked_predecessor: "Прежнее наблюдение без внешнего ID",
	unlinked_observation: "Новое наблюдение без внешнего ID",
	initial_observed: "Первое наблюдение",
};
export function Monitors({
	workspace,
	project,
	source,
	search,
	api,
	link,
}: Props) {
	const q = new URLSearchParams(search),
		id = q.get("monitor") || "",
		v = q.get("monitorVersion") || "",
		r = q.get("monitorRun") || "";
	const aspect = q.get("aspect"),
		aspectVersion = q.get("aspectVersion");
	const base = `/workspaces/${workspace}/projects/${project}/monitors`,
		url = base + (id ? "/" + id : "");
	const [data, setData] = useState<Data>(),
		[error, setError] = useState(""),
		[revision, refresh] = useState(0),
		[busy, setBusy] = useState(false);
	const [target, setTarget] = useState(""),
		[parent, setParent] = useState("auto");
	const alive = useRef(true),
		pending = useRef<{ selection: string; key: string } | null>(null);
	const pages = {
		catalog: q.get("monitorPage") || "0",
		versions: q.get("versionPage") || "0",
		snapshots: q.get("deliveryPage") || "0",
		history: q.get("historyPage") || "0",
		delta: q.get("deltaPage") || "0",
		aspects: q.get("aspectPage") || "0",
	};
	useEffect(() => {
		if (revision > 0) setData(undefined);
		alive.current = true;
		let live = true,
			timer: ReturnType<typeof setTimeout>,
			active: AbortController;
		async function read() {
			clearTimeout(timer);
			active?.abort();
			const control = new AbortController();
			active = control;
			if (document.visibilityState === "hidden") {
				setData(undefined);
				return;
			}
			const opts = { signal: control.signal },
				query = (offset: string) =>
					"?" +
					new URLSearchParams({
						source_id: source,
						offset,
						...(v ? { version_id: v } : {}),
					});
			try {
				const aspects = await api<Aspect[]>(
					`/workspaces/${workspace}/projects/${project}/aspects?offset=${pages.aspects}`,
					opts,
				);
				const aspectCount = aspects.length;
				let next: Data = { aspects, aspectCount };
				if (!id) next.catalog = await api(base + query(pages.catalog), opts);
				else {
					const [current, versions, snapshots, history, run] =
						await Promise.all([
							api<Overview>(url + query("0"), opts),
							api<NonNullable<Data["versions"]>>(
								url + "/versions" + query(pages.versions),
								opts,
							),
							api<Page<Snapshot>>(
								url + "/snapshots" + query(pages.snapshots),
								opts,
							),
							api<Page<History>>(url + "/history" + query(pages.history), opts),
							r ? api<Run>(url + "/runs/" + r + query("0"), opts) : undefined,
						]);
					next = {
						aspects,
						aspectCount,
						current,
						versions,
						snapshots,
						history,
						run,
					};
					if (run?.receipt)
						next.delta = await api<Page<Delta>>(
							url + "/runs/" + r + "/delta" + query(pages.delta),
							opts,
						);
				}
				const pinned = next.current?.payload;
				const a = pinned?.aspect_id || aspect,
					v = pinned?.aspect_version_id || aspectVersion;
				if (
					a &&
					v &&
					!aspects.some((x) => x.aspect_id === a && x.version_id === v)
				) {
					const exact = await api<Detail>(
						`/workspaces/${workspace}/projects/${project}/aspects/${a}/versions/${v}`,
						opts,
					);
					next.aspects = [
						...aspects,
						{
							aspect_id: exact.aspect_id,
							version_id: exact.version_id,
							name: exact.payload.name,
							archived: exact.archived,
						},
					];
				}
				if (live && !control.signal.aborted) {
					setData(next);
					setError("");
				}
			} catch (e) {
				if (live && !control.signal.aborted) {
					setData(undefined);
					setError((e as Error).message);
				}
			}
			if (live && !control.signal.aborted) timer = setTimeout(read, 5000);
		}
		const reload = () => {
			setData(undefined);
			void read();
		};
		void read();
		window.addEventListener("focus", reload);
		window.addEventListener("pageshow", reload);
		document.addEventListener("visibilitychange", reload);
		return () => {
			live = false;
			alive.current = false;
			active?.abort();
			clearTimeout(timer);
			window.removeEventListener("focus", reload);
			window.removeEventListener("pageshow", reload);
			document.removeEventListener("visibilitychange", reload);
		};
	}, [
		workspace,
		project,
		source,
		revision,
		api,
		base,
		url,
		id,
		v,
		r,
		pages.catalog,
		pages.versions,
		pages.snapshots,
		pages.history,
		pages.delta,
		aspect,
		aspectVersion,
		pages.aspects,
	]);
	const detail = data?.current,
		run = data?.run;
	const snapshots = data?.snapshots?.rows;
	const selection = snapshots?.find((x) => x.id === target) || snapshots?.[0];
	const snapshot = selection?.id || "";
	const previous = selection?.previous_run_id;
	const predecessor = parent === "auto" ? previous || null : parent || null;
	const post = async (
		path: string,
		body: object | undefined,
		after?: (x: {
			monitor_id: string;
			version_id: string;
			run_id: string;
		}) => void,
	) => {
		if (busy) return;
		setBusy(true);
		setError("");
		try {
			const x = await api<{
				monitor_id: string;
				version_id: string;
				run_id: string;
			}>(path, {
				method: "POST",
				headers: body ? { "Content-Type": "application/json" } : undefined,
				body: JSON.stringify(body),
			});
			if (alive.current) {
				after?.(x);
				refresh((n) => n + 1);
			}
		} catch (e) {
			if (alive.current) setError((e as Error).message);
		} finally {
			if (alive.current) setBusy(false);
		}
	};
	const nav = (field: string, page?: { total: number; offset: number }) =>
		page &&
		page.total > 25 && (
			<nav aria-label={field}>
				{page.offset > 0 && (
					<a href={link({ [field]: String(page.offset - 25) })}>Назад</a>
				)}{" "}
				{page.offset + 1}–{Math.min(page.offset + 25, page.total)} из{" "}
				{page.total}{" "}
				{page.offset + 25 < page.total && (
					<a href={link({ [field]: String(page.offset + 25) })}>Дальше</a>
				)}
			</nav>
		);
	const allowed =
		detail &&
		!detail.archived &&
		![
			"UNSUPPORTED_DEFINITION",
			"SUPERSEDED_VERSION",
			"ASPECT_ARCHIVED",
		].includes(detail.state);
	return (
		<section style={{ overflowWrap: "anywhere" }}>
			<h2>Сохранённые мониторы</h2>
			<p>Ручной запуск. Расписание пока недоступно.</p>
			{error && <p role="alert">{error}</p>}
			{data?.catalog && (
				<>
					<ul>
						{data.catalog.rows.map((x) => (
							<li key={x.monitor_id}>
								<a
									href={link({
										monitor: x.monitor_id,
										monitorVersion: "",
										monitorRun: "",
									})}
								>
									{x.name}
								</a>
								{x.archived && " · В архиве"}
							</li>
						))}
					</ul>
					{nav("monitorPage", data.catalog)}
				</>
			)}
			{detail && (
				<>
					<a href={link({ monitor: "", monitorVersion: "", monitorRun: "" })}>
						Все мониторы
					</a>
					<h3>
						{detail.payload.name} · v{detail.version_no}
					</h3>
					<p>
						{states[detail.state] || detail.state}. Последняя успешная проверка
						этой редакции — {detail.last_success_at || "ещё не было"}.
					</p>
					<p>
						Все принятые позиции выбранной поставки · {detail.payload.profile} ·{" "}
						{detail.payload.threshold_set}
					</p>
					<a
						href={link({
							view: "aspects",
							aspect: detail.payload.aspect_id,
							aspectVersion: detail.payload.aspect_version_id,
							project,
						})}
					>
						Открыть точное определение аспекта
					</a>
					{detail.last_attempt && (
						<p>
							Последняя попытка {detail.last_attempt.at} ·{" "}
							{states[detail.last_attempt.status]} {detail.last_attempt.reason}
						</p>
					)}
					<p>
						Критерий — {detail.payload.event.threshold} машинных упоминаний при
						выборке от {detail.payload.event.minimum} пригодных отзывов. Событие
						не вычисляется. QUALITY_GATE_NOT_MET.
					</p>
					<p>
						Редакции{" "}
						{data.versions?.rows.map((x) => (
							<a
								key={x.version_id}
								href={link({
									monitorVersion: x.version_id,
									monitorRun: "",
									deltaPage: "0",
									historyPage: "0",
								})}
							>
								v{x.version_no}{" "}
							</a>
						))}
					</p>
					{nav("versionPage", data.versions)}
					<label>
						Поставка{" "}
						<select
							aria-label="Поставка"
							value={snapshot}
							onChange={(e) => {
								setTarget(e.target.value);
								setParent("auto");
							}}
						>
							{data.snapshots?.rows.map((x) => (
								<option value={x.id} key={x.id}>
									{x.registered_at} · {x.accepted_count} принятых ·{" "}
									{x.coverage || "покрытие неизвестно"} · {x.id}
								</option>
							))}
						</select>
					</label>
					{nav("deliveryPage", data.snapshots)}
					<label>
						Предыдущий результат{" "}
						<select value={parent} onChange={(e) => setParent(e.target.value)}>
							<option value="auto">
								{previous
									? `Предыдущий успешный · ${previous}`
									: "Первый полный проход"}
							</option>
							<option value="">Полный проход без предшественника</option>
							{data.history?.rows
								.filter(
									(x) =>
										x.status === "succeeded" &&
										x.version_id === detail.version_id &&
										x.snapshot_id !== snapshot,
								)
								.map((x) => (
									<option key={x.run_id} value={x.run_id}>
										{x.run_id}
									</option>
								))}
						</select>
					</label>
					<button
						type="button"
						disabled={
							!allowed ||
							!snapshot ||
							busy ||
							(detail.state === "NO_NEW_DELIVERY" &&
								snapshot === detail.latest_snapshot_id)
						}
						onClick={() => {
							const selection = [
								id,
								detail.version_id,
								snapshot,
								predecessor,
							].join("/");
							if (pending.current?.selection !== selection)
								pending.current = { selection, key: crypto.randomUUID() };
							void post(
								url + "/runs",
								{
									source_id: source,
									version_id: detail.version_id,
									snapshot_id: snapshot,
									parent_run_id: predecessor,
									idempotency_key: pending.current.key,
								},
								(x) =>
									location.assign(
										link({
											monitorRun: x.run_id,
											monitorVersion: detail.version_id,
											deltaPage: "0",
										}),
									),
							);
						}}
					>
						Проверить поставку
					</button>
					<h3>История запусков</h3>
					<ul>
						{data.history?.rows.map((x) => (
							<li key={x.run_id}>
								<a
									href={link({
										monitorRun: x.run_id,
										monitorVersion: x.version_id,
										deltaPage: "0",
									})}
								>
									{states[x.status] || x.status} · {x.snapshot_id}
								</a>{" "}
								{x.reason}
							</li>
						))}
					</ul>
					{nav("historyPage", data.history)}
				</>
			)}
			{run && detail && (
				<>
					<h3>Запуск {r}</h3>
					<p>
						{states[run.status]} · {run.stage} · {run.processed} /{" "}
						{run.total ?? "—"} {run.reason}
					</p>
					{["queued", "running"].includes(run.status) && (
						<button
							type="button"
							disabled={busy}
							onClick={() =>
								void post(
									`/workspaces/${workspace}/runs/${run.decision_run_id}/cancel`,
									undefined,
								)
							}
						>
							Отменить запуск
						</button>
					)}
					<details>
						<summary>Паспорт поставки</summary>
						<p>
							Зарегистрирована {run.registered_at}. SHA-256 {run.file_sha256}
						</p>
						<p>
							Наблюдение {JSON.stringify(run.observed_at)}. Покрытие{" "}
							{JSON.stringify(run.coverage_passport)}
						</p>
					</details>
					{run.receipt && (
						<>
							<p>
								{states[run.receipt.change_state] ||
									"Результат выбранной поставки"}
								. Пригодных переиспользовано {run.receipt.reused_eligible},
								рассчитано {run.receipt.computed_eligible}. Пустых{" "}
								{run.receipt.excluded_blank}.
							</p>
							<p>
								Принято {run.receipt.accepted}, пригодно {run.receipt.eligible}.
								{Object.entries(run.receipt.categories).map(([key, count]) => (
									<span key={key}>
										{" "}
										{states[key] || key} · {count}.
									</span>
								))}
							</p>
							<p>
								Отсутствие в поставке не подтверждает удаление у источника.
								Машинная оценка · QUALITY_GATE_NOT_MET.
							</p>
							<a
								href={link({
									view: "decisions",
									snapshot: run.snapshot_id,
									source,
									candidateSource: source,
									aspect: detail.payload.aspect_id,
									aspectVersion: detail.payload.aspect_version_id,
									decisionRun: run.decision_run_id,
								})}
							>
								Решения и доказательства PR-22
							</a>
							<ul>
								{data.delta?.rows.map((x) => (
									<li key={x.identity_id}>
										{states[x.category]}{" "}
										{x.previous_version_id &&
											run.receipt?.parent_snapshot_id && (
												<a
													href={link({
														view: "reviews",
														source,
														snapshot: run.receipt.parent_snapshot_id,
														review: x.previous_version_id,
													})}
												>
													Прежняя версия {x.previous_version_id}
												</a>
											)}{" "}
										{x.target_version_id && (
											<a
												href={link({
													view: "reviews",
													source,
													snapshot: run.snapshot_id,
													review: x.target_version_id,
												})}
											>
												Версия поставки {x.target_version_id}
											</a>
										)}
									</li>
								))}
							</ul>
							{nav("deltaPage", data.delta)}
						</>
					)}
				</>
			)}
			{data &&
				(!id ||
					(detail &&
						!detail.archived &&
						detail.state !== "SUPERSEDED_VERSION")) && (
					<form
						key={detail?.version_id || "new"}
						onSubmit={(e) => {
							e.preventDefault();
							const form = new FormData(e.currentTarget);
							const [a, v] = String(form.get("definition")).split("/");
							void post(
								id ? url + "/versions" : base,
								{
									source_id: source,
									name: form.get("name"),
									aspect_id: a,
									aspect_version_id: v,
									selection: "accepted_in_delivery",
									threshold_count: Number(form.get("threshold")),
									min_eligible: Number(form.get("minimum")),
									...(detail ? { expected_version_id: detail.version_id } : {}),
								},
								(x) =>
									location.assign(
										link({
											monitor: x.monitor_id,
											monitorVersion: "",
											monitorRun: "",
										}),
									),
							);
						}}
					>
						<h3>{id ? "Новая редакция монитора" : "Создать монитор"}</h3>
						<label>
							Название{" "}
							<input
								name="name"
								required
								maxLength={240}
								defaultValue={detail?.payload.name}
							/>
						</label>
						<label>
							Сохранённый аспект{" "}
							<select
								name="definition"
								required
								defaultValue={
									detail
										? detail.payload.aspect_id +
											"/" +
											detail.payload.aspect_version_id
										: q.get("aspect") && q.get("aspectVersion")
											? q.get("aspect") + "/" + q.get("aspectVersion")
											: ""
								}
							>
								<option value="">Выберите определение</option>
								{data.aspects
									.filter((a) => !a.archived)
									.map((a) => (
										<option
											key={a.version_id}
											value={a.aspect_id + "/" + a.version_id}
										>
											{a.name}
										</option>
									))}
							</select>
						</label>
						<nav aria-label="Страницы аспектов">
							{Number(pages.aspects) > 0 && (
								<a
									href={link({
										aspectPage: String(Math.max(0, Number(pages.aspects) - 25)),
									})}
								>
									Предыдущие аспекты
								</a>
							)}{" "}
							{data.aspectCount === 25 && (
								<a
									href={link({
										aspectPage: String(Number(pages.aspects) + 25),
									})}
								>
									Следующие аспекты
								</a>
							)}
						</nav>
						<p>
							Выборка включает все принятые позиции выбранной поставки одного
							источника.
						</p>
						<label>
							Порог упоминаний{" "}
							<input
								name="threshold"
								type="number"
								min="1"
								max="10000"
								defaultValue={detail?.payload.event.threshold || 1}
								required
							/>
						</label>
						<label>
							Минимум пригодных отзывов{" "}
							<input
								name="minimum"
								type="number"
								min="1"
								max="10000"
								defaultValue={detail?.payload.event.minimum || 1}
								required
							/>
						</label>
						<button type="submit" disabled={busy}>
							Сохранить {id ? "редакцию" : "монитор"}
						</button>
						{detail && (
							<button
								type="button"
								disabled={busy}
								onClick={() =>
									void post(url + "/archive", {
										source_id: source,
										expected_version_id: detail.version_id,
									})
								}
							>
								Архивировать монитор
							</button>
						)}
					</form>
				)}
		</section>
	);
}
