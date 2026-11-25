import { type FormEvent, useEffect, useRef, useState } from "react";
import templates from "../../../src/reviewscope/resources/decision-definitions.json";
import demo from "./overview.synthetic.json";

export type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Link = (p: Record<string, string>) => string;
type Payload = {
	name: string;
	definition: string;
	include: string[];
	exclude: string[];
};
type Saved = { aspect_id: string; version_id: string };
type Summary = Saved & { name: string; version_no: number; archived: boolean };
export type Detail = Saved & {
	project_id: string;
	version_no: number;
	payload: Payload;
	archived: boolean;
	current_version_id: string;
	digest: string;
	origin: string;
};

export type Frozen = Omit<Detail, "current_version_id" | "archived">;

export function Definition({
	value,
}: {
	value: Frozen & { archived?: boolean };
}) {
	return (
		<article
			style={{ overflowWrap: "anywhere" }}
			aria-label="Определение аспекта"
		>
			<h3>
				{value.payload.name} · v{value.version_no}
			</h3>
			{value.archived && <p>Аспект в архиве</p>}
			<p style={{ whiteSpace: "pre-wrap" }}>{value.payload.definition}</p>
			{(["include", "exclude"] as const).map((group) => (
				<div key={group}>
					<strong>
						{group === "include" ? "Включающие примеры" : "Исключающие примеры"}
					</strong>
					<ul>
						{value.payload[group].map((text) => (
							<li key={text}>{text}</li>
						))}
					</ul>
				</div>
			))}
			<details>
				<summary>Происхождение и идентификаторы</summary>
				<p>
					{value.aspect_id} · {value.version_id} · SHA-256 {value.digest}
				</p>
				<p>
					{value.origin === "manual"
						? "Введено вручную"
						: findTopic(value.origin)
							? value.origin
							: "Историческое предложение недоступно · " + value.origin}
				</p>
			</details>
		</article>
	);
}

function findTopic(origin: string) {
	for (const place of demo.places)
		for (const topic of place.topics)
			if (
				[demo.snapshot, demo.run, place.source_result, place.id, topic.id].join(
					":",
				) === origin
			)
				return { place: place.name, topic };
}

type Props = {
	workspace: string;
	project: string;
	projects: { project_id: string; project_name: string }[];
	search: string;
	link: Link;
	api: Api;
};
export function Aspects({
	workspace,
	project,
	projects,
	search,
	link,
	api,
}: Props) {
	const q = new URLSearchParams(search),
		aspect = q.get("aspect") || "",
		version = q.get("aspectVersion") || "";
	const proposal = q.get("proposal"),
		topic = proposal ? findTopic(proposal) : null;
	const page = Number(q.get("aspectPage") || 0);
	const base = `/workspaces/${workspace}/projects/${project}/aspects`;
	const [data, setData] = useState<{ rows: Summary[]; detail?: Detail }>();
	const [error, setError] = useState(""),
		[notice, setNotice] = useState(""),
		[busy, setBusy] = useState(false);
	const [reload, setReload] = useState(0),
		[editing, setEditing] = useState(Boolean(proposal));
	const [confirmArchive, setArchive] = useState(false);
	const pending = useRef<{ body: string; key: string } | null>(null);
	useEffect(() => {
		if (reload > 0) setData(undefined);
		let live = true,
			timer: ReturnType<typeof setTimeout>;
		const control = new AbortController();
		async function read() {
			try {
				if (
					!project ||
					!!aspect !== !!version ||
					!Number.isSafeInteger(page) ||
					page < 0
				)
					throw new Error("Выбранная область или версия недоступна.");
				const opts = { signal: control.signal };
				const [rows, detail] = await Promise.all([
					api<Summary[]>(
						(aspect ? base + "/" + aspect + "/versions" : base) +
							"?offset=" +
							page,
						opts,
					),
					aspect
						? api<Detail>(base + "/" + aspect + "/versions/" + version, opts)
						: undefined,
				]);
				if (live) {
					setData({ rows, detail });
					setError("");
				}
			} catch (e) {
				if (live) {
					setData(undefined);
					setEditing(false);
					setArchive(false);
					setError((e as Error).message);
				}
			}
			if (live) timer = setTimeout(read, 5000);
		}
		void read();
		const hide = () => {
			setData(undefined);
			setEditing(false);
			setArchive(false);
		};
		const refresh = () => {
			hide();
			setReload((n) => n + 1);
		};
		window.addEventListener("pagehide", hide);
		window.addEventListener("focus", refresh);
		const restored = (event: PageTransitionEvent) => {
			if (event.persisted) refresh();
		};
		window.addEventListener("pageshow", restored);
		return () => {
			live = false;
			control.abort();
			clearTimeout(timer);
			window.removeEventListener("pagehide", hide);
			window.removeEventListener("focus", refresh);
			window.removeEventListener("pageshow", restored);
		};
	}, [api, base, aspect, version, project, page, reload]);
	async function save(e: FormEvent<HTMLFormElement>, archive = false) {
		e.preventDefault();
		const form = new FormData(e.currentTarget),
			detail = data?.detail,
			address = location.href;
		const string = (key: string) => String(form.get(key) ?? "");
		const body = archive
			? { expected_version: version }
			: {
					payload: {
						name: string("name"),
						definition: string("definition"),
						include: string("include").split("\n"),
						exclude: string("exclude").split("\n"),
					},
					origin: detail?.origin ?? proposal ?? "manual",
					...(detail ? { expected_version: version } : {}),
				};
		const route =
			base + (aspect ? `/${aspect}/${archive ? "archive" : "versions"}` : "");
		const signature = route + JSON.stringify(body);
		if (pending.current?.body !== signature)
			pending.current = { body: signature, key: crypto.randomUUID() };
		setBusy(true);
		setNotice("");
		try {
			const saved = await api<Saved>(route, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ ...body, idempotency_key: pending.current.key }),
			});
			if (location.href === address)
				location.assign(
					link({
						aspect: saved.aspect_id,
						aspectVersion: saved.version_id,
						aspectPage: "",
						proposal: "",
					}),
				);
		} catch (e) {
			if (location.href === address) {
				setNotice((e as Error).message);
				setReload((n) => n + 1);
			}
		} finally {
			setBusy(false);
		}
	}
	const detail = data?.detail;
	const [template, setTemplate] = useState<keyof typeof templates | "">("");
	const initial =
		detail?.payload ?? (template ? templates[template] : undefined);
	const change = { aspect: "", aspectVersion: "", aspectPage: "" };
	return (
		<section className="evidence" style={{ overflowWrap: "anywhere" }}>
			<h2>Определения аспектов</h2>
			<label>
				Проект аспекта
				<select
					value={project}
					onChange={(e) =>
						location.assign(
							link({
								...change,
								project: e.target.value,
								source: "",
								snapshot: "",
								view: "aspects",
								proposal: proposal || "",
							}),
						)
					}
				>
					{projects.map((p) => (
						<option value={p.project_id} key={p.project_id}>
							{p.project_name}
						</option>
					))}
				</select>
			</label>
			{error && (
				<p role="alert">
					{error}{" "}
					<button type="button" onClick={() => setReload((n) => n + 1)}>
						Повторить чтение
					</button>
				</p>
			)}
			{notice && <p role="alert">{notice}</p>}
			{!data && !error && <p role="status">Проверка доступа…</p>}
			{data && (
				<>
					{detail && (
						<>
							<Definition value={detail} />
							<a href={link({ ...change, proposal: "" })}>К списку аспектов</a>{" "}
							{q.get("snapshot") && (
								<a href={link({ view: "decisions" })}>Оценка по всему снимку</a>
							)}{" "}
							{!detail.archived && q.get("snapshot") && (
								<a
									href={link({
										view: "candidates",
										candidateRun: "",
										fragment: "",
									})}
								>
									Искать по этой версии
								</a>
							)}
						</>
					)}
					<h3>{aspect ? "История версий" : "Аспекты проекта"}</h3>
					{data.rows.length === 0 && <p>На этой странице записей нет.</p>}
					<ul>
						{data.rows.map((v) => (
							<li key={v.version_id}>
								<a
									href={link({
										aspect: v.aspect_id,
										aspectVersion: v.version_id,
										aspectPage: "",
										proposal: "",
									})}
								>
									{v.name} · v{v.version_no}
									{v.archived ? " · архив" : ""}
								</a>
							</li>
						))}
					</ul>
					{page > 0 && (
						<a href={link({ aspectPage: String(Math.max(0, page - 25)) })}>
							Предыдущие 25
						</a>
					)}{" "}
					{data.rows.length === 25 && (
						<a href={link({ aspectPage: String(page + 25) })}>Следующие 25</a>
					)}
					{proposal &&
						(topic ? (
							<p>
								Предложение из публичного демо · {topic.place} · кластер{" "}
								{topic.topic.id} · {topic.topic.count} отзывов ·{" "}
								{topic.topic.phrases.join(" · ")}
								<br />
								{proposal}
							</p>
						) : (
							<p role="alert">Исходная версия предложения недоступна.</p>
						))}
					{(!detail ||
						(!detail.archived && detail.current_version_id === version)) &&
						(!proposal || topic) && (
							<>
								{!editing ? (
									<button type="button" onClick={() => setEditing(true)}>
										{aspect ? "Создать следующую версию" : "Создать аспект"}
									</button>
								) : (
									<form onSubmit={save} key={template}>
										{!detail && !proposal && (
											<label>
												Начальное определение
												<select
													value={template}
													onChange={(e) =>
														setTemplate(
															e.target.value as keyof typeof templates | "",
														)
													}
												>
													<option value="">Ввести вручную</option>
													{Object.entries(templates).map(([id, payload]) => (
														<option key={id} value={id}>
															{payload.name} · экспериментальный профиль
														</option>
													))}
												</select>
											</label>
										)}
										<p>
											Опишите границы своими словами. Не вставляйте персональные
											данные и цитаты из реальных отзывов.
										</p>
										<label>
											Короткое имя
											<input
												name="name"
												maxLength={120}
												required
												defaultValue={initial?.name}
											/>
										</label>
										<label>
											Определение
											<textarea
												aria-label="Определение"
												name="definition"
												maxLength={2000}
												required
												defaultValue={initial?.definition}
											/>
										</label>
										{(["include", "exclude"] as const).map((group) => (
											<label key={group}>
												{group === "include" ? "Включающие" : "Исключающие"}{" "}
												примеры, по одному на строку
												<textarea
													name={group}
													required
													defaultValue={initial?.[group].join("\n")}
												/>
											</label>
										))}
										<button disabled={busy} type="submit">
											Подтвердить сохранение
										</button>{" "}
										<a href={link({ proposal: "" })}>Отменить</a>
									</form>
								)}
								{detail &&
									(confirmArchive ? (
										<form onSubmit={(e) => save(e, true)}>
											<p>
												Архивировать аспект? Старые запуски сохранят свои
												версии. Возврат из архива недоступен.
											</p>
											<button type="submit" disabled={busy}>
												Подтвердить архивацию
											</button>
											<button type="button" onClick={() => setArchive(false)}>
												Отменить
											</button>
										</form>
									) : (
										<button type="button" onClick={() => setArchive(true)}>
											Архивировать аспект
										</button>
									))}
							</>
						)}
				</>
			)}
			<p>
				<a
					href={link({
						view: q.get("snapshot") ? "candidates" : "",
						...change,
						proposal: "",
					})}
				>
					{q.get("snapshot") ? "К поиску снимка" : "К поставке"}
				</a>
			</p>
		</section>
	);
}
