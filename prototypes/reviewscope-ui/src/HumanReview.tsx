import { useEffect, useRef, useState } from "react";
import type { Api } from "./Aspects";
import type { Page } from "./AspectDecisions";
import {
	HumanReviewEntry,
	labels,
	modes,
	type History,
	type Label,
	type Write,
} from "./HumanReviewEntry";
import { outcomes } from "./decisionOutcomes";
import type { EvidenceResult } from "./ReviewEvidence";

const states = {
	unreviewed: "Без проверки",
	confirmed: "Подтверждено",
	corrected: "Исправлено",
	disputed: "Есть спор",
};
const reasons = {
	ALL_ELIGIBLE: "Пригодная версия",
	MACHINE_UNCERTAIN: "Машина оставила исход неясным",
	HUMAN_DISPUTE: "Явный спор",
	HUMAN_MACHINE_DIFFERENCE: "Решение человека отличается от машинного",
	FIXED_RANDOM_SAMPLE: "Член контрольной выборки",
};
type Link = (changes: Record<string, string>) => string;
type Row = {
	version_id: string;
	event_id: string | null;
	state: keyof typeof states;
	machine: Label;
	outcome: Label | null;
	reason?: string;
};

type Summary = {
	eligible: number;
	resolved: number;
	uncertain: number;
	disputed: number;
	unreviewed: number;
	confirmed: number;
	corrected: number;
	complaints: number;
	reviewed: number;
	complaints_among_resolved: number | null;
};
type Plan = {
	id: string;
	seed: string;
	requested: number;
	eligible: number;
	size: number;
	digest: string;
};
type Revision = {
	id: string;
	created_at: string;
	actor: string;
	digest: string;
	manifest_sha256: string;
	artifact_sha256: string;
	rows_sha256: string;
	summary: Summary;
	rows: Row[];
	total: number;
};
type Data =
	| { kind: "queue"; eligible: number; total: number; rows: Row[] }
	| { kind: "plans"; total: number; items: Plan[] }
	| {
			kind: "results";
			total: number;
			items: Pick<Revision, "id" | "created_at">[];
	  }
	| ({ kind: "result" } & Revision)
	| { kind: "review"; history: History; text: string };
type Props = {
	workspace: string;
	source: string;
	snapshot: string;
	run: string;
	page: Page;
	search: string;
	api: Api;
	link: Link;
};
function Pager({
	total,
	offset,
	href,
}: {
	total: number;
	offset: number;
	href: (offset: number) => string;
}) {
	return (
		<nav aria-label="Страницы">
			{offset > 0 && <a href={href(Math.max(0, offset - 25))}>Предыдущие 25</a>}{" "}
			{offset + 25 < total && <a href={href(offset + 25)}>Следующие 25</a>}
		</nav>
	);
}
export function HumanReview({
	workspace,
	source,
	snapshot,
	run,
	page,
	search,
	api,
	link,
}: Props) {
	const q = new URLSearchParams(search),
		screen = q.get("human") || "",
		member = q.get("decisionReview") || "",
		mode = q.get("reviewMode") || "all",
		state = q.get("reviewState") || "",
		outcome = q.get("reviewOutcome") || "",
		plan = q.get("reviewPlan") || "",
		revision = q.get("reviewResult") || "";
	const queueOffset = Number(q.get("reviewPage") || 0),
		subOffset = Number(q.get("reviewSubpage") || 0);
	const root = `/workspaces/${workspace}/snapshots/${snapshot}`,
		base = `${root}/decisions/${run}/human`;
	const [data, setData] = useState<Data>(),
		[error, setError] = useState(""),
		[notice, setNotice] = useState(""),
		[generation, bump] = useState(0),
		[busy, setBusy] = useState(false),
		[conflict, setConflict] = useState(false);
	const [size, setSize] = useState("25"),
		[seed, setSeed] = useState<string>(() => crypto.randomUUID());
	const pending = useRef<{ signature: string; key: string } | null>(null);
	const controller = useRef<AbortController | null>(null);
	const selected = page.rows.find((r) => r.version_id === member);
	const machine = selected?.outcome,
		machineText = selected?.text_sha256,
		machineContent = selected?.content_sha256;
	const reload = () => {
		setData(undefined);
		setError("");
		setNotice("");
		setConflict(false);
		pending.current = null;
		bump((v) => v + 1);
	};
	useEffect(() => {
		if (generation) setData(undefined);
		const control = new AbortController();
		controller.current = control;
		let timer: ReturnType<typeof setTimeout>;
		async function read() {
			const args = new URLSearchParams({
				source_id: source,
				offset: String(screen === "queue" ? queueOffset : subOffset),
			});
			const get = <T,>(path: string) =>
				api<T>(path + "?" + args, { signal: control.signal });
			try {
				let next: Data;
				if (screen === "queue") {
					args.set("mode", mode);
					if (state) args.set("state", state);
					if (outcome) args.set("outcome", outcome);
					if (plan) args.set("plan_id", plan);
					next = {
						kind: "queue",
						...(await get<Omit<Extract<Data, { kind: "queue" }>, "kind">>(
							base + "/queue",
						)),
					};
				} else if (screen === "plans")
					next = {
						kind: "plans",
						...(await get<Omit<Extract<Data, { kind: "plans" }>, "kind">>(
							base + "/plans",
						)),
					};
				else if (screen === "results")
					next = {
						kind: "results",
						...(await get<Omit<Extract<Data, { kind: "results" }>, "kind">>(
							base + "/results",
						)),
					};
				else if (screen === "result" && revision) {
					const result = await get<Revision>(
						base + "/results/" + encodeURIComponent(revision),
					);
					if (
						result.id !== revision ||
						result.manifest_sha256 !== page.manifest_sha256 ||
						result.rows_sha256 !== page.rows_sha256 ||
						result.artifact_sha256 !== page.artifact_sha256
					)
						throw new Error("Редакция не соответствует исходному запуску.");
					next = { kind: "result", ...result };
				} else if (screen === "review" && machine) {
					const detail = await api<EvidenceResult>(
						root +
							"/reviews/" +
							encodeURIComponent(member) +
							"?source_id=" +
							encodeURIComponent(source),
						{ signal: control.signal },
					);
					const history = await get<History>(
						base + "/reviews/" + encodeURIComponent(member) + "/history",
					);
					const row = detail.rows[0];
					if (
						detail.rows.length !== 1 ||
						row.version_id !== member ||
						detail.snapshot_id !== snapshot ||
						detail.source_id !== source ||
						!row.full_text ||
						history.text_sha256 !== machineText ||
						history.content_sha256 !== machineContent ||
						history.machine !== machine
					)
						throw new Error("Версия текста или решения недоступна.");
					next = { kind: "review", history, text: row.full_text };
				} else throw new Error("Страница проверки недоступна.");
				if (!control.signal.aborted) {
					setData(next);
					setError("");
					timer = setTimeout(read, 5000);
				}
			} catch (e) {
				if (!control.signal.aborted) {
					setData(undefined);
					setNotice("");
					setError((e as Error).message);
				}
			}
		}
		void read();
		const clear = () => {
			control.abort();
			clearTimeout(timer);
			setData(undefined);
			setNotice("");
			pending.current = null;
		};
		const restore = (e: PageTransitionEvent) => {
			if (e.persisted) bump((v) => v + 1);
		};
		window.addEventListener("pagehide", clear);
		window.addEventListener("pageshow", restore);
		return () => {
			control.abort();
			clearTimeout(timer);
			window.removeEventListener("pagehide", clear);
			window.removeEventListener("pageshow", restore);
		};
	}, [
		base,
		root,
		member,
		source,
		snapshot,
		screen,
		queueOffset,
		subOffset,
		outcome,
		mode,
		state,
		revision,
		plan,
		machineText,
		machineContent,
		machine,
		page.rows_sha256,
		page.artifact_sha256,
		page.manifest_sha256,
		api,
		generation,
	]);
	const write: Write = async (path, body, done) => {
		const c = controller.current;
		if (!c || c.signal.aborted || busy || conflict) return;
		const signature = JSON.stringify([path, body]);
		if (pending.current?.signature !== signature)
			pending.current = { signature, key: crypto.randomUUID() };
		setBusy(true);
		setNotice("");
		try {
			const saved = await api<{ id: string }>(base + path, {
				method: "POST",
				signal: c.signal,
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					...body,
					source_id: source,
					idempotency_key: pending.current.key,
				}),
			});
			if (!c.signal.aborted) {
				pending.current = null;
				done(saved.id);
			}
		} catch (e) {
			if (!c.signal.aborted) {
				const status = (e as { status?: number }).status;
				if (status === 409) {
					setConflict(true);
					setNotice(
						"Запись изменилась или ключ запроса уже использован. Перечитайте историю перед новым решением.",
					);
				} else if (
					status === 401 ||
					status === 403 ||
					status === 404 ||
					status === 503
				) {
					c.abort();
					setData(undefined);
					pending.current = null;
					setError((e as Error).message);
				} else
					setNotice(
						status
							? (e as Error).message
							: "Ответ не получен. Повторите то же действие для проверки сохранения.",
					);
			}
		} finally {
			setBusy(false);
		}
	};
	const navigate = (changes: Record<string, string>) =>
		location.assign(link(changes));
	const home = {
		human: "queue",
		decisionReview: "",
		reviewResult: "",
		reviewSubpage: "",
	};
	const sub = (dest: string) => ({ ...home, human: dest });
	const queueChange = (changes: Record<string, string>) => ({
		...home,
		reviewPage: "",
		...changes,
	});
	const inspect = (id: string) =>
		link({ ...sub("review"), decisionReview: id });
	return (
		<section aria-label="Ручная проверка">
			<h3>Ручная проверка</h3>
			<p>
				<a href={link({ human: "", decisionReview: "" })}>
					К машинному результату
				</a>
			</p>
			{error && (
				<p role="alert">
					{error}{" "}
					<button type="button" onClick={reload}>
						Повторить чтение проверки
					</button>
				</p>
			)}
			{!error && !data && <p role="status">Проверка доступа…</p>}
			{data && (
				<>
					<nav aria-label="Разделы проверки">
						<a href={link(home)}>Очередь проверки</a>
						{" · "}
						<a href={link(sub("plans"))}>Контрольные выборки</a>
						{" · "}
						<a href={link(sub("results"))}>Сохранённые редакции</a>
					</nav>
					{notice && (
						<p role="alert">
							{notice}{" "}
							{conflict && (
								<button type="button" onClick={reload}>
									Перечитать историю
								</button>
							)}
						</p>
					)}
					{data.kind === "queue" && (
						<>
							<label>
								Очередь{" "}
								<select
									value={mode}
									onChange={(e) =>
										e.target.value === "sample"
											? navigate(sub("plans"))
											: navigate(
													queueChange({
														reviewMode: e.target.value,
														reviewPlan: "",
													}),
												)
									}
								>
									{Object.entries(modes).map(([k, v]) => (
										<option key={k} value={k}>
											{v}
										</option>
									))}
								</select>
							</label>
							<label>
								Машинный исход{" "}
								<select
									value={outcome}
									onChange={(e) =>
										navigate(queueChange({ reviewOutcome: e.target.value }))
									}
								>
									<option value="">Все исходы</option>
									{labels.map(([k, v]) => (
										<option key={k} value={k}>
											{v}
										</option>
									))}
								</select>
							</label>
							<label>
								Состояние проверки{" "}
								<select
									value={state}
									onChange={(e) =>
										navigate(queueChange({ reviewState: e.target.value }))
									}
								>
									<option value="">Все состояния</option>
									{Object.entries(states).map(([k, v]) => (
										<option key={k} value={k}>
											{v}
										</option>
									))}
								</select>
							</label>
							<p>
								По фильтрам {data.total} из {data.eligible} пригодных версий.
								Жалоба может встретиться среди машинных исходов «Аспект не
								упомянут».
							</p>
							{plan && <p>Зафиксированная выборка {plan}</p>}
							{!data.total && <p>Подходящих записей нет.</p>}
							<ol aria-label="Версии для проверки" start={queueOffset + 1}>
								{data.rows.map((r) => (
									<li key={r.version_id}>
										<a href={inspect(r.version_id)}>
											{outcomes[r.machine]} · {r.version_id}
										</a>
										{" · "}
										{states[r.state]}
										{r.outcome && " · " + outcomes[r.outcome]} ·{" "}
										{reasons[r.reason as keyof typeof reasons]}
									</li>
								))}
							</ol>
							<Pager
								total={data.total}
								offset={queueOffset}
								href={(n) => link({ reviewPage: String(n) })}
							/>
						</>
					)}
					{data.kind === "plans" && (
						<>
							<h4>Контрольные выборки</h4>
							<p>
								Отбор по seed среди всех пригодных версий. План сохраняет
								порядок независимо от исходов и последующих правок.
							</p>
							<form
								onSubmit={(e) => {
									e.preventDefault();
									void write("/plans", { seed, size: Number(size) }, (id) =>
										navigate(
											queueChange({
												reviewMode: "sample",
												reviewPlan: id,
												reviewState: "",
												reviewOutcome: "",
											}),
										),
									);
								}}
							>
								<label>
									Seed выборки (UUID){" "}
									<input
										required
										value={seed}
										onChange={(e) => setSeed(e.target.value)}
									/>
								</label>
								<label>
									Размер выборки{" "}
									<input
										type="number"
										required
										min={1}
										max={10000}
										step={1}
										value={size}
										onChange={(e) => setSize(e.target.value)}
									/>
								</label>
								<button type="submit" disabled={busy || conflict}>
									Создать контрольную выборку
								</button>
							</form>
							<ul>
								{data.items.map((p) => (
									<li key={p.id}>
										<a
											href={link(
												queueChange({
													reviewMode: "sample",
													reviewPlan: p.id,
													reviewState: "",
													reviewOutcome: "",
												}),
											)}
										>
											{p.id} · выбрано {p.size} из {p.eligible}
										</a>
										<details>
											<summary>Параметры отбора</summary>Запрошено {p.requested}
											, seed {p.seed}, SHA-256 {p.digest}
										</details>
									</li>
								))}
							</ul>
						</>
					)}
					{data.kind === "results" && (
						<>
							<h4>Сохранённые редакции</h4>
							<p>
								Фиксация сохраняет текущие решения людей для этого запуска.
								Последующие изменения попадут в следующую редакцию после явной
								фиксации.
							</p>
							<button
								type="button"
								disabled={busy || conflict}
								onClick={() =>
									void write("/results", {}, (id) =>
										navigate({ ...sub("result"), reviewResult: id }),
									)
								}
							>
								Зафиксировать проверенную редакцию
							</button>
							<ul>
								{data.items.map((r) => (
									<li key={r.id}>
										<a href={link({ ...sub("result"), reviewResult: r.id })}>
											{r.id} · {new Date(r.created_at).toLocaleString("ru-RU")}
										</a>
									</li>
								))}
							</ul>
						</>
					)}
					{data.kind === "result" && (
						<>
							<article aria-label="Проверенная редакция">
								<h4>По меткам человека жалоб — {data.summary.complaints}</h4>
								<p>
									Редакция {data.id}, автор {data.actor},{" "}
									{new Date(data.created_at).toLocaleString("ru-RU")}.
								</p>
								<p>
									Просмотрено {data.summary.reviewed} из {data.summary.eligible}
									. Разрешено {data.summary.resolved}, в споре{" "}
									{data.summary.disputed}, без проверки{" "}
									{data.summary.unreviewed}.
								</p>
								<p>
									Неясных ручных исходов {data.summary.uncertain}. Подтверждений
									исхода модели {data.summary.confirmed}, исправлений{" "}
									{data.summary.corrected}. Среди разрешённых —{" "}
									{data.summary.complaints}/{data.summary.resolved}
									{data.summary.complaints_among_resolved === null
										? " · доля не определена, нет разрешённых решений"
										: ` (${data.summary.complaints_among_resolved.toLocaleString("ru-RU", { style: "percent", maximumFractionDigits: 1 })})`}
									. Это результат проверки выбранных отзывов. Для оценки частоты
									жалоб во всём корпусе нужно учитывать непроверенные и спорные
									версии.
								</p>
								<details>
									<summary>Происхождение редакции</summary>SHA-256 редакции{" "}
									{data.digest}, manifest {data.manifest_sha256}, строки{" "}
									{data.rows_sha256}, артефакт {data.artifact_sha256}.
								</details>
								<ol start={subOffset + 1}>
									{data.rows.map((r) => (
										<li key={r.version_id}>
											{r.version_id} · модель — {outcomes[r.machine]} ·{" "}
											{states[r.state]}
											{r.outcome && " · " + outcomes[r.outcome]}
											{r.event_id && " · событие " + r.event_id}
										</li>
									))}
								</ol>
							</article>
						</>
					)}
					{data.kind === "review" && selected && (
						<HumanReviewEntry
							history={data.history}
							row={selected}
							text={data.text}
							selection={mode}
							plan={plan}
							busy={busy || conflict || subOffset > 0}
							write={write}
							onSaved={() => {
								reload();
							}}
						/>
					)}
					{data.kind !== "queue" && (
						<Pager
							total={data.kind === "review" ? data.history.total : data.total}
							offset={subOffset}
							href={(n) => link({ reviewSubpage: String(n) })}
						/>
					)}
					<details>
						<summary>Ограничения машинной оценки PR-22</summary>
						<p>
							На 381 синтетической паре с известной позицией лексический профиль
							дал TP/FP/FN 4/1/28 и precision/recall/F1 0,800/0,125/0,216. У
							MiniLM separate — 24/12/8 и 0,667/0,750/0,706, у MiniLM pair —
							24/22/8 и 0,522/0,750/0,615. Ни один профиль не достиг порогов
							precision 0,85 и recall 0,80. В кабинете доступен лексический
							профиль для шести поддержанных определений.
						</p>
						<p>
							QUALITY_GATE_NOT_MET · SYNTHETIC_EVIDENCE_ONLY. История
							исправлений хранится в этом запуске. Обучение модели требует
							отдельного разрешения и проверки на независимых данных.
						</p>
					</details>
				</>
			)}
		</section>
	);
}
