import { useEffect, useRef, useState } from "react";
import { MapOfPairs } from "./DuplicateGraph";
import { type Span, quote, citationBounds } from "./reviewCitations";

type Endpoint = {
	version_id: string;
	text_sha256: string;
	content_sha256: string;
	event_id: string | null;
	stance: string | null;
	state: string;
	spans: Span[];
	text?: string;
};
type Pair = {
	text_spans: [number[], number[]] | null;
	left: Endpoint;
	right: Endpoint;
	channels: Record<string, unknown>;
	text_kind: string;
	aspect_kind: string;
	aspect_basis: string;
	reason: string;
};
type Manifest = {
	source_id: string;
	snapshot_id: string;
	decision_run: string | null;
	human_revision: string | null;
	definition_digest: string | null;
	aspect_version_id: string | null;
	eligible_review_count: number;
	excluded_blank_count: number;
	total_possible_pairs: number;
	candidate_pairs_count: number;
	profile: string;
};
type Page = {
	filtered: number;
	id: string;
	digest: string;
	manifest: Manifest;
	pairs: Pair[];
};
type Props = {
	workspace: string;
	snapshot: string;
	source: string;
	search: string;
	api: <T>(p: string, i?: RequestInit) => Promise<T>;
	link: (v: Record<string, string>) => string;
};
const labels: Record<string, string> = {
	exact_text: "Тексты совпадают",
	text_similarity_candidate: "Кандидат по сходству слов",
	no_relation: "Связь не установлена",
	opposite_stance: "Противоположные позиции",
	same_specific_complaint: "Общая конкретная проблема",
	uncertain: "Недостаточно оснований",
	not_evaluated: "Не оценивалось",
	complaint: "Жалоба",
	denial: "Явное отрицание",
	mixed: "Смешанная оценка",
};
function Side({ e, word }: { e: Endpoint; word?: number[] }) {
	const chars = Array.from(e.text ?? "");
	let at = 0;
	const segments = [...e.spans]
		.sort((a, b) => a.start - b.start)
		.map((span) => {
			const before = chars.slice(at, span.start).join("");
			at = span.end;
			return (
				<span key={span.start}>
					{before}
					<mark>{quote(chars, span)}</mark>
				</span>
			);
		});
	return (
		<article className="evidence-detail">
			<h4>{labels[e.stance ?? ""] ?? e.stance ?? "Позиция не установлена"}</h4>
			<p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
				{segments}
				{chars.slice(at).join("")}
			</p>
			<details>
				<summary>Версия и свидетельство</summary>
				{word && (
					<p>
						Основание сравнения текста ·{" "}
						<q>{chars.slice(word[0], word[1]).join("")}</q>
					</p>
				)}
				<p>
					{e.version_id} · {e.state} · {e.event_id ?? "Без события"}
				</p>
				<p>
					{e.content_sha256} · {e.text_sha256}
				</p>
			</details>
		</article>
	);
}
export function TypedRelations({
	workspace,
	source,
	snapshot,
	api,
	link,
	search,
}: Props) {
	const q = new URLSearchParams(search),
		id = q.get("relation") ?? "",
		digest = q.get("relationDigest") ?? "",
		exactOnly = q.get("pairFilter") === "exact",
		left = q.get("left") ?? "",
		right = q.get("right") ?? "",
		offset = Number(q.get("pairPage") ?? 0),
		run = q.get("relationRun") ?? "",
		revision = q.get("relationRevision") ?? "";
	const root = `/workspaces/${workspace}/snapshots/${snapshot}/relations`;
	const [page, setPage] = useState<Page>(),
		[status, setStatus] = useState("Проверка доступа…"),
		[busy, setBusy] = useState(false);
	const generation = useRef(0),
		requestKey = useRef(crypto.randomUUID());
	useEffect(() => {
		let live = true;
		async function load() {
			const n = ++generation.current;
			setPage(undefined);
			setStatus("Проверка доступа…");
			if (document.hidden) return;
			try {
				const params = new URLSearchParams({ source_id: source });
				let next: Page | undefined;
				if (id) {
					if (left || right) {
						params.set("left", left);
						params.set("right", right);
					} else params.set("offset", String(offset));
					if (digest) params.set("digest", digest);
					if (exactOnly) params.set("exact_only", "true");
					next = await api<Page>(
						root + "/" + encodeURIComponent(id) + "?" + params,
					);
					if (
						next.id !== id ||
						next.manifest.snapshot_id !== snapshot ||
						next.manifest.source_id !== source ||
						((run || revision) &&
							(next.manifest.decision_run !== run ||
								next.manifest.human_revision !== revision))
					)
						throw Error();
					if (left && right) {
						const pair = next.pairs[0];
						if (
							next.pairs.length !== 1 ||
							pair.left.version_id !== left ||
							pair.right.version_id !== right
						)
							throw Error();
						for (const e of [pair.left, pair.right]) {
							if (typeof e.text !== "string") throw Error();
							const hash = await crypto.subtle.digest(
								"SHA-256",
								new TextEncoder().encode(e.text),
							);
							if (
								Array.from(new Uint8Array(hash), (v) =>
									v.toString(16).padStart(2, "0"),
								).join("") !== e.text_sha256 ||
								!citationBounds(
									Array.from(e.text),
									e.spans.map((s) => [String(s.start), String(s.end)]),
								).valid
							)
								throw Error();
						}
					}
				} else {
					if (run || revision) {
						params.set("decision_run", run);
						params.set("human_revision", revision);
					}
					const opt = await api<{ reason?: string }>(root + "?" + params);
					if (opt.reason) {
						if (live && n === generation.current)
							setStatus("RESOURCE_LIMIT · " + opt.reason);
						return;
					}
				}
				if (live && n === generation.current) {
					setPage(next);
					setStatus("ready");
				}
			} catch {
				if (live && n === generation.current)
					setStatus(
						"CANONICAL_GRAPH_NOT_AVAILABLE. Нет доступа к выбранной области или результату.",
					);
			}
		}
		function hide() {
			++generation.current;
			setPage(undefined);
			setStatus("Проверка доступа…");
		}
		function visible() {
			if (document.hidden) hide();
			else void load();
		}
		void load();
		const timer = window.setInterval(visible, 10000);
		window.addEventListener("pagehide", hide);
		window.addEventListener("pageshow", visible);
		window.addEventListener("focus", visible);
		document.addEventListener("visibilitychange", visible);
		return () => {
			live = false;
			++generation.current;
			clearInterval(timer);
			window.removeEventListener("pagehide", hide);
			window.removeEventListener("pageshow", visible);
			window.removeEventListener("focus", visible);
			document.removeEventListener("visibilitychange", visible);
		};
	}, [
		api,
		source,
		snapshot,
		root,
		run,
		revision,
		id,
		digest,
		left,
		right,
		offset,
		exactOnly,
	]);
	async function publish() {
		setBusy(true);
		try {
			const saved = await api<{ id: string }>(root, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					source_id: source,
					idempotency_key: requestKey.current,
					decision_run: run || null,
					human_revision: revision || null,
				}),
			});
			location.assign(
				link({
					relation: saved.id,
					pairPage: "0",
					left: "",
					right: "",
					relationDigest: "",
				}),
			);
		} catch (error) {
			setStatus(
				(error as { status?: number }).status === 413
					? "RESOURCE_LIMIT. Снимок превышает предел профиля."
					: "Не удалось сохранить результат.",
			);
		} finally {
			setBusy(false);
		}
	}
	const select = (a: string, b: string) =>
		link({ left: a, right: b, relationDigest: page?.digest ?? "" });
	return (
		<section style={{ overflowWrap: "anywhere" }}>
			<h2>Отношения между отзывами</h2>
			{status !== "ready" ? (
				<p role="status">{status}</p>
			) : !page ? (
				<>
					<p>
						Снимок {snapshot}. До 32 версий по 4 КиБ и 496 пар. Совпадение
						текста и общие слова проверяются отдельно. Семантическая модель
						отключена.
					</p>
					<p>
						{revision
							? "Позиции по фиксированной редакции " + revision
							: "Только текст. Для сравнения позиций откройте сохранённую редакцию ручной проверки."}
					</p>
					<button type="button" disabled={busy} onClick={() => void publish()}>
						Построить прямые пары
					</button>
				</>
			) : (
				<>
					<p>
						Прямых пар — {page.manifest.candidate_pairs_count} из{" "}
						{page.manifest.total_possible_pairs} возможных. Пригодных отзывов —{" "}
						{page.manifest.eligible_review_count}, пустых —{" "}
						{page.manifest.excluded_blank_count}.
					</p>
					<p>
						Совпадение слов служит для отбора. Позиции выводятся из двух
						отдельных ручных решений. Независимой проверки пары нет.
					</p>
					<details>
						<summary>Источник и метод</summary>
						<p>
							{source} · {snapshot} · {page.manifest.profile} · {page.digest}
						</p>
						<p>
							{page.manifest.decision_run} {page.manifest.aspect_version_id}{" "}
							{page.manifest.definition_digest} {page.manifest.human_revision}
						</p>
					</details>
					{left ? (
						<a href={link({ left: "", right: "" })}>К странице пар</a>
					) : null}
					{!left && (
						<p>
							<a
								href={link({
									pairFilter: exactOnly ? "" : "exact",
									pairPage: "0",
								})}
							>
								{exactOnly
									? "Все отобранные пары"
									: "Только совпадающие тексты"}
							</a>{" "}
							· В выборке {page.filtered}
						</p>
					)}
					{!page.pairs.length ? (
						<p>
							{page.manifest.eligible_review_count === 0
								? "Нет пригодных отзывов."
								: "На этой странице прямых пар нет."}
						</p>
					) : (
						<>
							{!left && (
								<MapOfPairs
									edges={page.pairs.map((p) => [
										p.left.version_id,
										p.right.version_id,
									])}
									totals={{
										members: page.manifest.eligible_review_count,
										edges: page.manifest.candidate_pairs_count,
									}}
									select={select}
									canonical
								/>
							)}
							<table>
								<thead>
									<tr>
										<th>Отзывы</th>
										<th>Текст</th>
										<th>Выбранный аспект</th>
									</tr>
								</thead>
								<tbody>
									{page.pairs.map((p) => (
										<tr key={p.left.version_id + p.right.version_id}>
											<td>
												<a href={select(p.left.version_id, p.right.version_id)}>
													{p.left.version_id} и {p.right.version_id}
												</a>
											</td>
											<td>{labels[p.text_kind]}</td>
											<td>{labels[p.aspect_kind]}</td>
										</tr>
									))}
								</tbody>
							</table>
						</>
					)}
					{left &&
						page.pairs.map((p) => (
							<div key={p.left.version_id}>
								<details>
									<summary>Основания и каналы отбора</summary>
									<p>
										{p.aspect_basis} · {p.reason} · {JSON.stringify(p.channels)}
									</p>
								</details>
								<div
									style={{
										display: "grid",
										gridTemplateColumns:
											"repeat(auto-fit,minmax(min(100%,16rem),1fr))",
									}}
								>
									<Side e={p.left} word={p.text_spans?.[0]} />
									<Side e={p.right} word={p.text_spans?.[1]} />
								</div>
							</div>
						))}
					{!left && offset > 0 && (
						<a href={link({ pairPage: String(Math.max(0, offset - 10)) })}>
							Предыдущие 10 пар
						</a>
					)}
					{!left && offset + 10 < page.filtered && (
						<a href={link({ pairPage: String(offset + 10) })}>
							Следующие 10 пар
						</a>
					)}
				</>
			)}
		</section>
	);
}
