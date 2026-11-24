import manifest from "./reviewer-manifest.json";

export const reviewerPlaces = manifest.places;
export const reviewerDataset = manifest.dataset;

export function reviewerHref(
	place: string,
	params: Record<string, string> = {},
) {
	const q = new URLSearchParams({
		view: "public-reviewers",
		dataset: manifest.dataset,
		result: reviewerPlaces.find((p) => p.id === place)?.run ?? "",
		place,
	});
	for (const [key, value] of Object.entries(params))
		if (value) q.set(key, value);
	return "/cabinet/?" + q;
}

// Only a validated public screen supplies origin state. Reconstruct a same-origin cabinet link.
export function publicOrigin(search: string, place: string) {
	const q = new URLSearchParams(search);
	const fields: Record<string, string[]> = {
		legacy: ["ranking", "group", "item", "page", "review", "version"],
		"legacy-graph": ["family", "kind", "page", "a", "b"],
		"public-signals": ["rating", "cohort", "page", "review", "version"],
	};
	const view = q.get("view") ?? "";
	if (!Object.hasOwn(fields, view) || (q.get("place") ?? "p1") !== place)
		return "";
	const valid = ["view", "dataset", "result", "place", ...fields[view]];
	if ([...q.keys()].some((k) => !valid.includes(k) || q.getAll(k).length !== 1))
		return "";
	return q.toString();
}

export function reviewContextHref(
	place: string,
	review: string,
	version: string,
	originSearch = location.search,
) {
	return reviewerHref(place, {
		review,
		version,
		from: publicOrigin(originSearch, place),
	});
}
