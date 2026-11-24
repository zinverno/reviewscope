import { sources } from "./OverviewSources";
import {
	publicOrigin,
	reviewerDataset,
	reviewerPlaces,
} from "./reviewerNavigation";

export const reviewerPageSize = 25;
export type Ref = { id: string; version: string };
type Member = Ref & { place: string; date: string | null };
type Author = {
	id: string;
	source: string;
	anchor: Ref;
	count: number;
	total: number;
	chunk: string;
	history: string;
};
type Index = {
	dataset: string;
	source_result: string;
	place: string;
	run: string;
	known: number;
	missing: number;
	total: number;
	authors: Author[];
};
export type Place = {
	id: string;
	name: string;
	category: string | null;
	count: number;
	coordinates: [number, number] | null;
	geo: string;
	anchor: Ref;
};
type Coverage = Record<
	"total" | "date" | "rating" | "text" | "category" | "city" | "coordinates",
	number
>;
export type History = {
	source: string;
	id: string;
	coverage: Coverage;
	first: string | null;
	last: string | null;
	categories: [string, number][];
	cities: [string, number][];
	places: Place[];
	reviews: Member[];
};
type Score = {
	value: number | null;
	band: string;
	components: Record<string, number>;
	signals: string[];
	counter_signals: string[];
};
export type Profile = {
	source: string;
	id: string;
	metrics: Record<string, number | null>;
	scores: Record<"category" | "local" | "relevance", Score>;
};
type Primary = Awaited<
	ReturnType<(typeof sources)[string]>
>["default"]["reviews"][string];
export type State = {
	index: Index;
	author?: Author;
	history?: History;
	profile?: Profile;
	member?: Member;
	primary?: Primary;
};
const chunks = import.meta.glob<string>("./reviewer-context/*.json", {
	query: "?raw",
	import: "default",
});
async function chunk<T>(
	name: string,
	expected = name.slice(0, -5),
): Promise<T> {
	const read = chunks["./reviewer-context/" + name];
	if (!read) throw new Error("chunk unavailable");
	const raw = await read();
	const bytes = await crypto.subtle.digest(
		"SHA-256",
		new TextEncoder().encode(raw),
	);
	if (
		Array.from(new Uint8Array(bytes), (n) =>
			n.toString(16).padStart(2, "0"),
		).join("") !== expected
	)
		throw new Error("chunk digest mismatch");
	return JSON.parse(raw) as T;
}
async function source(place: string) {
	const entry = reviewerPlaces.find((p) => p.id === place);
	const doc = (await sources["./overview-sources/" + place + ".json"]())
		.default;
	if (
		doc.place !== place ||
		doc.result !== entry?.source_result ||
		doc.snapshot !== reviewerDataset
	)
		throw new Error("source mismatch");
	return doc.reviews;
}
function pageNumber(q: URLSearchParams, key: string, total: number) {
	const raw = q.get(key) ?? "1";
	if (
		!/^[1-9]\d{0,5}$/.test(raw) ||
		Number(raw) > Math.max(1, Math.ceil(total / reviewerPageSize))
	)
		throw new Error("invalid page");
	return Number(raw);
}
export async function readReviewerContext(search: string): Promise<State> {
	const q = new URLSearchParams(search);
	const place = q.get("place") ?? "",
		entry = reviewerPlaces.find((p) => p.id === place);
	const allowed = [
		"view",
		"dataset",
		"result",
		"place",
		"review",
		"version",
		"page",
		"authorsPage",
		"evidencePlace",
		"evidence",
		"evidenceVersion",
		"from",
	];
	if (
		!entry ||
		q.get("dataset") !== reviewerDataset ||
		q.get("result") !== entry.run ||
		[...q.keys()].some(
			(k) => !allowed.includes(k) || q.getAll(k).length !== 1,
		) ||
		(q.has("from") && !publicOrigin(q.get("from") ?? "", place))
	)
		throw new Error("invalid scope");
	const index = await chunk<Index>(place + ".json", entry.sha256);
	if (
		index.place !== place ||
		index.run !== entry.run ||
		index.dataset !== reviewerDataset ||
		index.source_result !== entry.source_result
	)
		throw new Error("index mismatch");
	pageNumber(q, "authorsPage", index.authors.length);
	const state: State = { index };
	const review = q.get("review"),
		version = q.get("version");
	if (
		!review &&
		![
			"review",
			"version",
			"page",
			"evidencePlace",
			"evidence",
			"evidenceVersion",
		].some((k) => q.has(k))
	)
		return state;
	const records = await source(place);
	if (
		!review ||
		!version ||
		!Object.hasOwn(records, review) ||
		records[review].version !== version
	)
		throw new Error("unknown version");
	const row = records[review];
	if (!row.reviewer_id?.trim() || !row.source?.trim()) {
		if (
			["page", "evidencePlace", "evidence", "evidenceVersion"].some((k) =>
				q.has(k),
			)
		)
			throw new Error("unknown author");
		return state;
	}
	const author = index.authors.find(
		(a) => a.source === row.source && a.id === row.reviewer_id,
	);
	if (!author) throw new Error("missing scoped author");
	const [history, detail] = await Promise.all([
		chunk<History>(author.history),
		chunk<{ place: string; profiles: Profile[] }>(author.chunk),
	]);
	const profile = detail.profiles.find(
		(p) => p.id === author.id && p.source === author.source,
	);
	if (
		!profile ||
		detail.place !== place ||
		history.source !== author.source ||
		history.id !== author.id ||
		history.reviews.length !== author.total ||
		!history.reviews.some(
			(r) => r.place === place && r.id === review && r.version === version,
		)
	)
		throw new Error("profile mismatch");
	pageNumber(q, "page", history.reviews.length);
	Object.assign(state, { author, profile, history });
	if (["evidencePlace", "evidence", "evidenceVersion"].some((k) => q.has(k))) {
		const member = history.reviews.find(
			(r) =>
				r.place === q.get("evidencePlace") &&
				r.id === q.get("evidence") &&
				r.version === q.get("evidenceVersion"),
		);
		if (!member) throw new Error("unknown history member");
		const rows = member.place === place ? records : await source(member.place);
		const primary = Object.hasOwn(rows, member.id) ? rows[member.id] : null;
		if (
			!primary ||
			primary.version !== member.version ||
			primary.reviewer_id !== author.id ||
			primary.source !== author.source
		)
			throw new Error("changed source version");
		Object.assign(state, { member, primary });
	}
	return state;
}
