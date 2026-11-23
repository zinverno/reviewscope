import { useEffect, useState } from "react";
import manifest from "./demo-graph/manifest.json";

type Kind = "exact" | "fuzzy" | "near" | "semantic";
type Edge = [string, string, Kind, number];
type Family = {
	id: number;
	members: string[];
	edges: Edge[];
	signals: string[];
	counter: string[];
};
type Review = {
	text: string | null;
	rating: number | null;
	date: string | null;
	version: string;
};
type Result = {
	schema: string;
	snapshot: string;
	place: string;
	run: string;
	groups: Family[];
	reviews: Record<string, Review>;
};
type Link = (updates: Record<string, string>) => string;
const files = import.meta.glob<Result>("./demo-graph/p*.json", {
	import: "default",
});
const kinds: Kind[] = ["exact", "fuzzy", "near", "semantic"];
const cfg = manifest.thresholds;
const meaning: Record<Kind, string> = {
	exact: "Совпадение после приведения регистра и пробелов.",
	fuzzy: `Сходство текста RapidFuzz от ${cfg.fuzzy_group_threshold} до ${cfg.fuzzy_threshold}.`,
	near: `Сходство текста RapidFuzz от ${cfg.fuzzy_threshold}.`,
	semantic: `Косинусное сходство сохранённых векторов от ${cfg.semantic_threshold}.`,
};
export function demoGraphHref(
	place = "p1",
	updates: Record<string, string> = {},
) {
	const selection = manifest.places.find((p) => p.id === place);
	const q = new URLSearchParams({
		view: "legacy-graph",
		dataset: manifest.snapshot,
		result: selection?.run ?? "",
		place,
	});
	for (const [key, value] of Object.entries(updates))
		if (value) q.set(key, value);
	return "/cabinet/?" + q;
}
export function CanonicalGraph({
	workspace,
	snapshot,
	search,
	api,
	link,
}: {
	workspace: string;
	snapshot: string;
	search: string;
	api: <T>(path: string) => Promise<T>;
	link: Link;
}) {
	const q = new URLSearchParams(search),
		run = q.get("run"),
		source = q.get("source");
	const [state, setState] = useState<"loading" | "available" | "closed">(
		"loading",
	);
	useEffect(() => {
		let live = true;
		const base = "/workspaces/" + workspace;
		Promise.all([
			api<{ id: string; source_id: string }>(
				base + "/snapshots/" + encodeURIComponent(snapshot),
			),
			run
				? api<{ snapshot_id: string }>(
						base + "/runs/" + encodeURIComponent(run) + "/status",
					)
				: Promise.resolve(null),
		])
			.then(([s, r]) => {
				if (live)
					setState(
						s.id === snapshot &&
							(!source || s.source_id === source) &&
							(!r || r.snapshot_id === snapshot)
							? "available"
							: "closed",
					);
			})
			.catch(() => {
				if (live) setState("closed");
			});
		return () => {
			live = false;
		};
	}, [workspace, snapshot, source, run, api]);
	return (
		<section>
			<h2>Повторы в выбранном снимке</h2>
			{state === "loading" ? (
				<p role="status">Проверка области…</p>
			) : state === "closed" ? (
				<p role="alert">Снимок или запуск недоступны.</p>
			) : (
				<>
					<p>
						CANONICAL_GRAPH_NOT_AVAILABLE. Проверка целостности этого снимка не
						создаёт семейства текстов или прямые пары.
					</p>
					<p>
						<a href={link({ view: "overview" })}>
							Обзор и статус выбранного снимка
						</a>
					</p>
					<a href={demoGraphHref()}>Исследовать повторы публичной синтетики</a>
				</>
			)}
		</section>
	);
}
function MapOfPairs({
	edges,
	totals,
	select,
}: {
	edges: Edge[];
	totals: { members: number; edges: number };
	select: (a: string, b: string) => string;
}) {
	const [open, setOpen] = useState(
		() => !matchMedia("(prefers-reduced-motion: reduce)").matches,
	);
	// Follow direct pairs in table order, bound the diagram even on a dense family.
	const shown = edges.slice(0, 10);
	const ids = [...new Set(shown.flatMap(([a, b]) => [a, b]))];
	const points = new Map(
		ids.map((id, i) => [
			id,
			{
				x: 150 + 120 * Math.cos((2 * Math.PI * i) / ids.length),
				y: 100 + 76 * Math.sin((2 * Math.PI * i) / ids.length),
			},
		]),
	);
	return (
		<>
			<label className="check">
				<input
					type="checkbox"
					checked={open}
					onChange={(e) => setOpen(e.target.checked)}
				/>
				Показать карту этой страницы
			</label>
			{open && (
				<figure className="direct-map">
					<svg
						viewBox="0 0 300 200"
						role="img"
						aria-label="Карта прямых пар текущей страницы"
					>
						{shown.map(([a, b]) => {
							const l = points.get(a)!,
								r = points.get(b)!;
							return (
								<a
									key={a + " " + b}
									href={select(a, b)}
									aria-label={a + " и " + b}
								>
									<line x1={l.x} x2={r.x} y1={l.y} y2={r.y} />
								</a>
							);
						})}
						{ids.map((id, i) => {
							const p = points.get(id)!;
							return (
								<g key={id}>
									<title>{id}</title>
									<circle cx={p.x} cy={p.y} r="8" />
									<text x={p.x} y={p.y + 3} textAnchor="middle">
										{i + 1}
									</text>
								</g>
							);
						})}
					</svg>
					<figcaption>
						На карте показаны {ids.length} из {totals.members} отзывов семейства
						и {shown.length} из {totals.edges} прямых пар выбранного типа.
						Расстояние на схеме не измеряет сходство.
					</figcaption>
					<details>
						<summary>Номера отзывов на карте</summary>
						<ol>
							{ids.map((id) => (
								<li key={id}>
									<a href={select(id, "")}>{id}</a>
								</li>
							))}
						</ol>
					</details>
				</figure>
			)}
		</>
	);
}
function TextOfReview({ id, data }: { id: string; data: Review }) {
	return (
		<article className="evidence-detail">
			<h4>Отзыв {id}</h4>
			<p>
				{data.rating ?? "Н/Д"} ★ · {data.date ?? "Дата неизвестна"}
			</p>
			{data.text ? (
				<blockquote>{data.text}</blockquote>
			) : (
				<p>Текст отсутствует.</p>
			)}
			<details>
				<summary>Версия записи</summary>
				<p>{data.version}</p>
			</details>
		</article>
	);
}
export function LegacyGraph({ search }: { search: string }) {
	const q = new URLSearchParams(search),
		place = q.get("place") ?? "",
		entry = manifest.places.find((p) => p.id === place);
	const valid =
		entry &&
		q.get("dataset") === manifest.snapshot &&
		q.get("result") === entry.run &&
		[...q.keys()].every((k) =>
			[
				"view",
				"dataset",
				"result",
				"place",
				"family",
				"kind",
				"page",
				"a",
				"b",
			].includes(k),
		);
	const [doc, setDoc] = useState<Result | null>(),
		[failure, setFailure] = useState(false);
	useEffect(() => {
		let live = true;
		if (valid && entry)
			files["./demo-graph/" + entry.id + ".json"]()
				.then((d) => {
					if (live) {
						if (
							d.snapshot === manifest.snapshot &&
							d.schema === manifest.schema &&
							d.place === entry.id &&
							d.run === entry.run
						)
							setDoc(d);
						else setFailure(true);
					}
				})
				.catch(() => {
					if (live) setFailure(true);
				});
		return () => {
			live = false;
		};
	}, [valid, entry]);
	if (!valid || failure)
		return (
			<section>
				<p role="alert">Для выбранного снимка и результата граф недоступен.</p>
				<a href={demoGraphHref()}>Открыть проверенный результат демо</a>
			</section>
		);
	const f = q.has("family")
		? doc?.groups.find((g) => String(g.id) === q.get("family"))
		: doc?.groups[0];
	const kind = q.get("kind") ?? "",
		rawPage = q.get("page") ?? "0",
		page = Number(rawPage),
		size = 20;
	const edges = f?.edges.filter((e) => !kind || e[2] === kind) ?? [],
		slice = edges.slice(page * size, (page + 1) * size);
	const a = q.get("a") ?? "",
		b = q.get("b") ?? "";
	const scope = {
		family: f ? String(f.id) : "",
		kind,
		page: page ? String(page) : "",
		a,
		b,
	};
	const href = (update: Record<string, string>) =>
		demoGraphHref(place, { ...scope, ...update });
	const pair = f?.edges.find(
		([l, r]) => (l === a && r === b) || (l === b && r === a),
	);
	const wrong =
		(!f && q.has("family")) ||
		(kind && !kinds.some((k) => k === kind)) ||
		!/^\d{1,4}$/.test(rawPage) ||
		(page > 0 && page * size >= edges.length) ||
		(a && !f?.members.includes(a)) ||
		(b && (!a || b === a || !f?.members.includes(b)));
	return (
		<section className="duplicate-investigation">
			<p>
				<a href={"/cabinet/?view=legacy&place=" + place}>
					Обзор точки публичного демо
				</a>
			</p>
			<h2>Похожие и повторяющиеся тексты</h2>
			<p>Публичная синтетика · упакованный DuckDB</p>
			<label>
				Точка графа
				<select
					value={place}
					onChange={(e) => location.assign(demoGraphHref(e.target.value))}
				>
					{manifest.places.map((p) => (
						<option key={p.id} value={p.id}>
							{p.id} · семейств {p.families}
						</option>
					))}
				</select>
			</label>
			{!doc ? (
				<p role="status">Загрузка прямых пар…</p>
			) : wrong ? (
				<p role="alert">Выбор отсутствует в этом результате.</p>
			) : !f ? (
				<p>Детектор не выделил семейств в этой точке.</p>
			) : (
				<>
					<label>
						Семейство
						<select
							value={f.id}
							onChange={(e) =>
								location.assign(
									demoGraphHref(place, { family: e.target.value }),
								)
							}
						>
							{doc.groups.map((g) => (
								<option key={g.id} value={g.id}>
									№ {g.id} · {g.members.length} отзывов · {g.edges.length} пар
								</option>
							))}
						</select>
					</label>
					<p className="overview-count">
						<strong>{f.members.length}</strong> отзывов в семействе ·{" "}
						{f.edges.length} прямых пар
					</p>
					<p>
						Отзывы входят в одну связанную группу. Похожесть может объясняться
						общей темой или короткой формулировкой. По сходству текста нельзя
						установить намерения авторов.
					</p>
					<details>
						<summary>Основания и границы метода</summary>
						{f.signals.map((s) => (
							<p key={s}>{s}</p>
						))}
						{f.counter.map((s) => (
							<p key={s}>{s}</p>
						))}
						{!f.counter.length && (
							<p>Дополнительные контрсигналы в результате не записаны.</p>
						)}
						{Object.entries(meaning).map(([k, v]) => (
							<p key={k}>
								{k} · {v}
							</p>
						))}
					</details>
					<label>
						Тип прямой связи
						<select
							value={kind}
							onChange={(e) =>
								location.assign(
									href({ kind: e.target.value, page: "", a: "", b: "" }),
								)
							}
						>
							<option value="">Все</option>
							{kinds.map((k) => (
								<option key={k} value={k}>
									{k}
								</option>
							))}
						</select>
					</label>
					<p>
						На этой странице показаны {slice.length} из {edges.length} прямых
						пар выбранного типа.
					</p>
					<MapOfPairs
						edges={slice}
						totals={{ members: f.members.length, edges: edges.length }}
						select={(l, r) => href({ a: l, b: r })}
					/>
					<table>
						<caption>Прямые пары</caption>
						<thead>
							<tr>
								<th>Два отзыва</th>
								<th>Тип связи</th>
								<th>Сходство</th>
							</tr>
						</thead>
						<tbody>
							{slice.map(([l, r, k, score]) => (
								<tr key={l + " " + r}>
									<td>
										<a href={href({ a: l, b: r })}>
											{l} · {r}
										</a>
									</td>
									<td>{k}</td>
									<td>
										{score.toLocaleString("ru", { maximumFractionDigits: 4 })}
									</td>
								</tr>
							))}
						</tbody>
					</table>
					{page > 0 && (
						<a href={href({ page: String(page - 1), a: "", b: "" })}>
							Предыдущие пары
						</a>
					)}{" "}
					{(page + 1) * size < edges.length && (
						<a href={href({ page: String(page + 1), a: "", b: "" })}>
							Следующие пары
						</a>
					)}
					<h3>Сравнение исходных текстов</h3>
					<div className="pair">
						{["a", "b"].map((side, i) => (
							<label key={side}>
								{i ? "Второй отзыв" : "Первый отзыв"}
								<select
									value={i ? b : a}
									disabled={Boolean(i && !a)}
									onChange={(e) =>
										location.assign(
											href(
												i
													? { b: e.target.value }
													: { a: e.target.value, b: "" },
											),
										)
									}
								>
									<option value="">Выбрать</option>
									{f.members
										.filter((id) => !i || id !== a)
										.map((id) => (
											<option key={id}>{id}</option>
										))}
								</select>
							</label>
						))}
					</div>
					{a && b && (
						<p role="status">
							{pair
								? `Прямая связь: ${pair[2]} · ${meaning[pair[2]]} Значение детектора: ${pair[3]}.`
								: "В одном семействе. Прямой связи между выбранными отзывами в результате нет."}
						</p>
					)}
					{a && <TextOfReview id={a} data={doc.reviews[a]} />}{" "}
					{b && <TextOfReview id={b} data={doc.reviews[b]} />}
				</>
			)}
			<details>
				<summary>Происхождение графа</summary>
				<p>
					{manifest.producer} · {manifest.schema}
				</p>
				<p>
					Снимок {manifest.snapshot} · результат точки {entry.run} · реестр
					результатов {manifest.run}
				</p>
			</details>
		</section>
	);
}
