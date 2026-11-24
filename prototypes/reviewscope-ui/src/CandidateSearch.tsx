import { type FormEvent, useEffect, useRef, useState } from "react";
import type { EvidenceResult } from "./ReviewEvidence";

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Link = (p: Record<string, string>) => string;
type Hit = {
	version_id: string;
	fragment_id: string;
	text_sha256: string;
	start: number;
	end: number;
	context_start: number;
	context_end: number;
	score: number;
	rank: number;
	review_rank: number;
	channels: Record<string, { rank: number; similarity: number }>;
};
type Result = {
	status: string;
	workspace_id: string;
	source_id: string;
	snapshot_id: string;
	run_id: string;
	reviewed_count: number;
	eligible_review_count: number;
	fragment_count: number | null;
	retrieved_review_count: number;
	candidate_count: number;
	mode: string;
	k: number;
	candidates: Hit[];
};
type Listing = {
	evidence?: EvidenceResult;
	runs: { id: string; status: string }[];
	result: Result | null;
};
type Props = {
	workspace: string;
	source: string;
	snapshot: string;
	api: Api;
	link: Link;
	search: string;
};

export async function exactParts(text: string, hit: Hit) {
	const bytes = new Uint8Array(
		await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)),
	);
	const sha = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(
		"",
	);
	const chars = Array.from(text);
	const { start: a, end: b, context_start: c, context_end: d } = hit;
	if (
		sha !== hit.text_sha256 ||
		![a, b, c, d].every(Number.isSafeInteger) ||
		!(0 <= c && c <= a && a < b && b <= d && d <= chars.length)
	)
		throw new Error("Фрагмент недоступен в этой версии.");
	return [
		chars.slice(c, a).join(""),
		chars.slice(a, b).join(""),
		chars.slice(b, d).join(""),
	];
}

export function CandidateSearch({
	workspace,
	source,
	snapshot,
	api,
	link,
	search,
}: Props) {
	const base = `/workspaces/${workspace}/snapshots/${snapshot}`;
	const route = `${base}/candidates`;
	const params = new URLSearchParams(search);
	const run = params.get("candidateRun") ?? "",
		fragment = params.get("fragment") ?? "";
	const [state, setState] = useState<{
		listing?: Listing;
		parts?: string[];
		full?: string;
		error?: string;
	}>();
	const [notice, setNotice] = useState(""),
		[busy, setBusy] = useState(false);
	const [reload, setReload] = useState(0);
	const pending = useRef<{ body: string; key: string } | null>(null);
	useEffect(() => {
		if (reload > 0) setState(undefined);
		let live = true;
		const controller = new AbortController();
		let timer: ReturnType<typeof setTimeout>;
		async function read() {
			try {
				const init = { signal: controller.signal },
					filter = new URLSearchParams({ source_id: source });
				const listing = await api<Listing>(`${route}?${filter}`, init);
				const status = listing.runs.find((r) => r.id === run)?.status;
				if (run && !status) throw new Error("Запуск недоступен.");
				let parts: string[] | undefined, full: string | undefined;
				if (status === "succeeded") {
					filter.set("run_id", run);
					if (fragment) filter.set("fragment_id", fragment);
					const verified = await api<Listing>(`${route}?${filter}`, init);
					listing.result = verified.result;
					listing.evidence = verified.evidence;
					const doc = listing.result;
					if (
						!doc ||
						doc.workspace_id !== workspace ||
						doc.source_id !== source ||
						doc.snapshot_id !== snapshot ||
						doc.run_id !== run
					)
						throw new Error("Результат недоступен в этом снимке.");
					if (fragment) {
						const hit = doc.candidates.find((h) => h.fragment_id === fragment);
						if (!hit) throw new Error("Фрагмент недоступен в этом запуске.");
						const detail = listing.evidence;
						if (!detail) throw new Error("Версия недоступна.");
						const row = detail.rows[0];
						if (
							detail.snapshot_id !== snapshot ||
							detail.source_id !== source ||
							row?.version_id !== hit.version_id ||
							!row.full_text
						)
							throw new Error("Версия недоступна.");
						full = row.full_text;
						parts = await exactParts(full, hit);
					}
				} else if (fragment) throw new Error("Фрагмент недоступен.");
				if (live) {
					setState({ listing, parts, full });
					if (status === "queued" || status === "running")
						timer = setTimeout(read, 1200);
					else if (fragment) timer = setTimeout(read, 5000);
				}
			} catch (e) {
				if (live) setState({ error: (e as Error).message });
			}
		}
		void read();
		function refresh() {
			setState(undefined);
			setReload((n) => n + 1);
		}
		function hide() {
			setState(undefined);
		}
		window.addEventListener("focus", refresh);
		window.addEventListener("pagehide", hide);
		function restored(e: PageTransitionEvent) {
			if (e.persisted) refresh();
		}
		window.addEventListener("pageshow", restored);
		return () => {
			live = false;
			controller.abort();
			clearTimeout(timer);
			window.removeEventListener("focus", refresh);
			window.removeEventListener("pagehide", hide);
			window.removeEventListener("pageshow", restored);
		};
	}, [api, route, workspace, source, snapshot, run, fragment, reload]);
	async function submit(e: FormEvent<HTMLFormElement>) {
		e.preventDefault();
		const form = new FormData(e.currentTarget);
		const value = {
			source_id: source,
			query: String(form.get("query")),
			role: form.get("role"),
			mode: form.get("mode"),
			k: Number(form.get("k")),
		};
		const body = JSON.stringify(value);
		if (!pending.current || pending.current.body !== body)
			pending.current = { body, key: crypto.randomUUID() };
		const address = location.href;
		setBusy(true);
		setNotice("");
		try {
			const job = await api<{ id: string }>(route, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					...value,
					idempotency_key: pending.current.key,
				}),
			});
			if (location.href === address)
				location.assign(link({ candidateRun: job.id, fragment: "" }));
		} catch (e) {
			setNotice((e as Error).message);
			setState(undefined);
			setReload((n) => n + 1);
		} finally {
			setBusy(false);
		}
	}
	const listing = state?.listing,
		doc = listing?.result;
	const selected = doc?.candidates.find((h) => h.fragment_id === fragment);
	const status = listing?.runs.find((r) => r.id === run)?.status;
	return (
		<section className="evidence">
			<a
				href={link({
					view: "reviews",
					candidateSource: "",
					candidateRun: "",
					fragment: "",
				})}
			>
				Вернуться к отзывам снимка
			</a>
			<h2>Кандидаты, не подтверждённые жалобы</h2>
			{notice && <p role="alert">{notice}</p>}
			{state?.error ? (
				<>
					<p role="alert">{state.error}</p>
					<button type="button" onClick={() => setReload((n) => n + 1)}>
						Повторить чтение
					</button>
				</>
			) : !listing ? (
				<p role="status">Проверка доступа…</p>
			) : (
				<>
					<form onSubmit={submit}>
						<label>
							Формулировка или пример
							<textarea name="query" required maxLength={2048} />
						</label>
						<label>
							Тип запроса
							<select name="role">
								<option value="query_to_passage">Формулировка проблемы</option>
								<option value="text_to_text">Текст примера</option>
							</select>
						</label>
						<label>
							Метод поиска
							<select name="mode">
								<option value="lexical">Лексический</option>
								<option value="dense">MiniLM</option>
								<option value="hybrid">Лексический + MiniLM (RRF)</option>
							</select>
						</label>
						<label>
							Максимум разных отзывов
							<input
								name="k"
								type="number"
								required
								min={1}
								max={10}
								defaultValue={5}
							/>
						</label>
						<button disabled={busy} type="submit">
							Найти кандидатов
						</button>
					</form>
					{listing.runs.length > 0 && (
						<details>
							<summary>Сохранённые поиски</summary>
							{listing.runs.map((r) => (
								<p key={r.id}>
									<a href={link({ candidateRun: r.id, fragment: "" })}>
										{r.id} · {r.status}
									</a>
								</p>
							))}
						</details>
					)}
					{status && <p role="status">Состояние запуска · {status}</p>}
					{(status === "queued" || status === "running") && (
						<button
							type="button"
							onClick={async () => {
								try {
									await api(`/workspaces/${workspace}/runs/${run}/cancel`, {
										method: "POST",
									});
									setReload((n) => n + 1);
								} catch (e) {
									setNotice((e as Error).message);
									setState(undefined);
									setReload((n) => n + 1);
								}
							}}
						>
							Отменить поиск
						</button>
					)}
					{doc &&
						(doc.status !== "READY" ? (
							<p role="status">Поиск недоступен · {doc.status}</p>
						) : (
							<>
								<p>
									Снимок {snapshot} · Источник {source} · Метод {doc.mode}
								</p>
								<p>
									Принято {doc.reviewed_count}, с текстом{" "}
									{doc.eligible_review_count}, фрагментов в индексе{" "}
									{doc.fragment_count}. В выдаче {doc.retrieved_review_count}{" "}
									разных отзывов и {doc.candidate_count} фрагментов.
								</p>
								{selected && state?.parts && (
									<article aria-label="Фрагмент кандидата">
										<h3>Область кандидата в контексте</h3>
										<p>
											Версия {selected.version_id} · [{selected.start},{" "}
											{selected.end})
										</p>
										<blockquote style={{ whiteSpace: "pre-wrap" }}>
											{state.parts[0]}
											<mark>{state.parts[1]}</mark>
											{state.parts[2]}
										</blockquote>
										<details>
											<summary>Полный текст версии</summary>
											<blockquote style={{ whiteSpace: "pre-wrap" }}>
												{state.full}
											</blockquote>
										</details>
										<a href={link({ fragment: "" })}>
											Вернуться к списку кандидатов
										</a>
									</article>
								)}
								{doc.candidates.map((h) => (
									<p key={h.fragment_id}>
										<a
											aria-current={
												h.fragment_id === fragment ? "true" : undefined
											}
											href={link({ fragment: h.fragment_id })}
										>
											Фрагмент {h.rank} · отзыв {h.review_rank}
										</a>{" "}
										· {h.score.toFixed(4)} ·{" "}
										{Object.entries(h.channels)
											.map(
												([c, v]) =>
													c +
													" #" +
													v.rank +
													" (" +
													v.similarity.toFixed(3) +
													")",
											)
											.join(", ")}
									</p>
								))}
							</>
						))}
				</>
			)}
		</section>
	);
}
