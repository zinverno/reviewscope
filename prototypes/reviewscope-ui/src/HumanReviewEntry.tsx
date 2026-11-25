import { useState, type FormEvent } from "react";
import type { Page } from "./AspectDecisions";
import { outcomes, type Outcome } from "./decisionOutcomes";
import { citationBounds, quote, type Span } from "./reviewCitations";

export type Label = Exclude<Outcome, "excluded_blank">;
export const labels = Object.entries(outcomes).filter(
	([id]) => id !== "excluded_blank",
);
export const modes = {
	all: "Все пригодные",
	uncertain: "Неясные решения модели",
	disagreement: "Разногласия",
	sample: "Контрольная выборка",
};
const actions = {
	confirm: "Подтвердить решение модели",
	correct: "Исправить исход",
	dispute: "Отметить разногласие",
};
type Event = {
	id: string;
	previous_id: string | null;
	sequence: number;
	actor: string;
	created_at: string;
	action: keyof typeof actions;
	outcome: Label | null;
	reason: string;
	spans: Span[];
	selection: keyof typeof modes;
	plan_id: string | null;
};
export type History = {
	head: string | null;
	total: number;
	events: Event[];
	machine: Label;
	text_sha256: string;
	content_sha256: string;
};
export type Write = (
	path: string,
	data: Record<string, unknown>,
	done: (id: string) => void,
) => Promise<void>;
export function HumanReviewEntry({
	history,
	text,
	row,
	selection,
	plan,
	write,
	busy,
	onSaved,
}: {
	history: History;
	text: string;
	row: Page["rows"][number];
	selection: string;
	plan: string;
	write: Write;
	busy: boolean;
	onSaved: () => void;
}) {
	// Keep the head the reviewer actually opened until an explicit reload or a successful save.
	const [expected] = useState(history.head);
	const [action, setAction] = useState<keyof typeof actions>("confirm");
	const [label, setLabel] = useState<Label>(history.machine);
	const [reason, setReason] = useState("");
	const [bounds, setBounds] = useState([
		["", ""],
		["", ""],
	]);
	const chars = Array.from(text);
	const outcome =
		action === "dispute"
			? null
			: action === "confirm"
				? history.machine
				: label;
	const { spans, valid } = citationBounds(chars, bounds);
	const needsQuote = outcome === "complaint" || outcome === "mixed";
	async function save(e: FormEvent) {
		e.preventDefault();
		if (!valid || (needsQuote && !spans.length) || !reason.trim()) return;
		await write(
			`/reviews/${row.version_id}/events`,
			{
				action,
				outcome,
				reason,
				spans,
				expected_event_id: expected,
				selection,
				plan_id: plan || null,
			},
			onSaved,
		);
	}
	return (
		<>
			<form onSubmit={save} aria-label="Решение человека">
				<p>
					Версия отзыва {row.version_id}. Полный текст содержит {chars.length}{" "}
					символов Unicode.
				</p>
				<blockquote style={{ whiteSpace: "pre-wrap" }}>{text}</blockquote>
				<p>
					Исход модели — {outcomes[history.machine]}. Оценка модели{" "}
					{row.score ?? "отсутствует"}.
				</p>
				{row.start !== null && row.end !== null ? (
					<p>
						Окно модели [{row.start}, {row.end}) ·{" "}
						{chars.slice(row.start, row.end).join("")}
					</p>
				) : (
					<p>Модель не выделила окно текста.</p>
				)}
				{row.counter_start !== null && row.counter_end !== null && (
					<p>
						Встречное основание модели [{row.counter_start}, {row.counter_end})
						· {chars.slice(row.counter_start, row.counter_end).join("")}
					</p>
				)}
				<label>
					Действие{" "}
					<select
						value={action}
						onChange={(e) => setAction(e.target.value as keyof typeof actions)}
					>
						{Object.entries(actions).map(([k, v]) => (
							<option key={k} value={k}>
								{v}
							</option>
						))}
					</select>
				</label>
				{action === "correct" && (
					<label>
						Исход человека{" "}
						<select
							value={label}
							onChange={(e) => setLabel(e.target.value as Label)}
						>
							{labels.map(([k, v]) => (
								<option key={k} value={k}>
									{v}
								</option>
							))}
						</select>
					</label>
				)}
				<p>
					Неизвестный этап ожидания можно отметить как неясный. Смешанная
					позиция относится к этому же аспекту. Для другого аспекта откройте его
					отдельное определение.
				</p>
				<p>
					Укажите границы цитаты в символах Unicode с нуля. Правая граница
					следует за последним символом.{" "}
					{needsQuote
						? "Для жалобы обязательна цитата. Без точной цитаты можно отметить разногласие."
						: "Цитата по возможности."}
				</p>
				{bounds.map((pair, i) => (
					<fieldset key={i === 0 ? "main" : "counter"}>
						<legend>
							{i === 0 ? "Цитата человека" : "Дополнительная цитата"}
						</legend>
						{["Начало", "Конец"].map((name, j) => (
							<label key={name}>
								{name} {i + 1}{" "}
								<input
									type="number"
									min="0"
									max={chars.length}
									step="1"
									value={pair[j]}
									onChange={(e) =>
										setBounds(
											bounds.map((old, n) =>
												n === i
													? old.map((v, m) => (m === j ? e.target.value : v))
													: old,
											),
										)
									}
								/>
							</label>
						))}
						{pair.every(Boolean) && (
							<output
								aria-label={`Предпросмотр цитаты ${i + 1}`}
								style={{ whiteSpace: "pre-wrap" }}
							>
								{quote(chars, { start: Number(pair[0]), end: Number(pair[1]) })}
							</output>
						)}
					</fieldset>
				))}
				{!valid && (
					<p role="alert">Проверьте границы цитат и их пересечение.</p>
				)}
				<label>
					Причина проверки{" "}
					<textarea
						required
						maxLength={300}
						value={reason}
						onChange={(e) => setReason(e.target.value)}
					/>
				</label>
				<button
					type="submit"
					disabled={
						busy ||
						!reason.trim() ||
						!valid ||
						(needsQuote && !spans.length) ||
						(action === "correct" && label === history.machine)
					}
				>
					Сохранить решение
				</button>
			</form>
			<h4>История решений</h4>
			{!history.total && <p>Событий ещё нет.</p>}
			<ol aria-label="История решений">
				{history.events.map((ev) => (
					<li key={ev.id}>
						<strong>
							№{ev.sequence} · {actions[ev.action]}
						</strong>{" "}
						{ev.outcome && outcomes[ev.outcome]}
						<p>
							Автор {ev.actor} ·{" "}
							{new Date(ev.created_at).toLocaleString("ru-RU")} ·{" "}
							{modes[ev.selection]} {ev.plan_id}
						</p>
						<p style={{ whiteSpace: "pre-wrap" }}>{ev.reason}</p>
						{ev.spans.map((sp) => (
							<blockquote
								key={sp.start + "-" + sp.end}
								style={{ whiteSpace: "pre-wrap" }}
							>
								[{sp.start}, {sp.end}) · {quote(chars, sp)}
							</blockquote>
						))}
						<details>
							<summary>Событие</summary>
							{ev.id} · Предыдущее {ev.previous_id || "отсутствует"}
						</details>
					</li>
				))}
			</ol>
		</>
	);
}
