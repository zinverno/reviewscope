import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const read = async (path) =>
	JSON.parse(
		await readFile(new URL("../../src/" + path, import.meta.url), "utf8"),
	);
const workspace = "a1111111-1111-4111-8111-111111111111";
const source = "b1111111-1111-4111-8111-111111111111";
const snapshot = "c1111111-1111-4111-8111-111111111111";
const version = "d1111111-1111-4111-8111-111111111111";
async function cabinet(browser, origin) {
	const page = await browser.newPage({ reducedMotion: "reduce" });
	// In production FastAPI maps /cabinet/ to the built cabinet.html entry.
	await page.route(
		(url) => url.pathname === "/cabinet/",
		async (route) => {
			const res = await route.fetch({ url: origin + "cabinet.html" });
			await route.fulfill({
				response: res,
				body: (await res.text()).replace(
					'src="./src/Cabinet.tsx"',
					'src="/src/Cabinet.tsx"',
				),
			});
		},
	);
	await page.route("**/api/v1/**", (route) =>
		route.fulfill({
			json: route.request().url().endsWith("/session")
				? {
						principal_id: "synthetic",
						workspaces: [{ id: workspace, name: "Синтетика", role: "analyst" }],
					}
				: [],
		}),
	);
	return page;
}
export async function checkReviewerNavigation(browser, origin) {
	const page = await cabinet(browser, origin);
	const foreign = [];
	page.on("request", (req) => {
		if (new URL(req.url()).origin !== new URL(origin).origin)
			foreign.push(new URL(req.url()).origin);
	});
	try {
		const index = await read("reviewer-context/p1.json");
		const long = index.authors.find((a) => a.total > 25);
		const history = await read("reviewer-context/" + long.history);
		const at = (name) => page.getByRole("link", { name, exact: true });
		await page.goto(origin + "cabinet/?view=legacy&place=p1&ranking=five");
		await at("Авторы этой точки").press("Enter");
		const heading = page.getByRole("heading", { name: "Авторы этой точки" });
		await heading.waitFor();
		assert.equal(await page.locator("ol li").count(), 25);
		await at("Следующая страница").click();
		assert.equal(new URL(page.url()).searchParams.get("authorsPage"), "2");
		await heading.waitFor();
		assert.equal(await page.locator("ol li").count(), 25);
		const profile = new URL(page.url());
		profile.searchParams.set("review", long.anchor.id);
		profile.searchParams.set("version", long.anchor.version);
		await page.goto(profile.href);
		const timeline = page.getByRole("region", { name: "История отзывов" });
		const first = page.getByRole("heading", {
			name: "История отзывов",
			exact: true,
		});
		await first.waitFor();
		assert.equal(await timeline.locator("ol li").count(), 25);
		const rows = page
			.locator(".reviewer-context table")
			.last()
			.locator("tbody tr");
		assert.equal(await rows.count(), history.places.length);
		assert.equal(
			await page.locator(".reviewer-plot circle").count(),
			history.places.filter((p) => p.geo === "valid").length,
		);
		for (const width of [320, 390, 1366, 1920]) {
			await page.setViewportSize({ width, height: 840 });
			assert(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth,
				),
			);
		}
		await page.getByText("Метрики, метод и основания", { exact: true }).click();
		await page
			.getByRole("heading", { name: "Индекс локального контекста" })
			.waitFor();
		await timeline
			.getByRole("link", { name: "Следующая страница" })
			.press("Enter");
		await first.waitFor();
		assert.equal(
			await timeline.locator("ol li").count(),
			history.reviews.length - 25,
		);
		const secondPage = page.url(),
			dest = history.reviews.slice(25).find((r) => r.place !== "p1");
		await at("Исходный отзыв " + dest.id).press("Enter");
		const evidence = page.locator(".source-text");
		await evidence.waitFor();
		assert.equal(
			await evidence.textContent(),
			(await read("overview-sources/" + dest.place + ".json")).reviews[dest.id]
				.text,
		);
		const selected = new URL(page.url());
		assert.equal(selected.searchParams.get("evidenceVersion"), dest.version);
		await at("Вернуться к истории отзывов").click();
		await first.waitFor();
		assert.equal(page.url(), secondPage);
		await page.goBack();
		await evidence.waitFor();
		await page.goForward();
		await first.waitFor();
		for (const [k, v] of [
			["result", "wrong"],
			["source", source],
			["version", dest.version],
			["evidencePlace", "p999"],
			["page", "99"],
			["from", "view=reviews&review=" + version],
		]) {
			const broken = new URL(selected);
			broken.searchParams.set(k, v);
			await page.goto(broken.href);
			await page.getByRole("alert").filter({ hasText: "недоступна" }).waitFor();
			assert.equal(await evidence.count(), 0);
			assert.equal(await first.count(), 0);
		}
		assert.equal(foreign.length, 0);
	} finally {
		await page.close();
	}
}

export async function checkReviewerRevocation(browser, origin) {
	const page = await cabinet(browser, origin);
	const other = "c2222222-2222-4222-8222-222222222222";
	const marker = "Синтетическая контрольная точка";
	const params = new URLSearchParams({
		view: "reviewer-context",
		workspace,
		source,
		snapshot,
		review: version,
		contextSource: source,
	});
	const doc = {
		method: "snapshot-reviewer-context-v1",
		workspace_id: workspace,
		source_id: source,
		snapshot_id: snapshot,
		anchor_version_id: version,
		manifest_sha256: "f".repeat(64),
		offset: 0,
		limit: 25,
		status: "computed",
		availability: {
			ratings: {
				status: "insufficient_data",
				eligible: 0,
				total: 1,
				reason: "NO_ELIGIBLE_RATINGS",
			},
		},
		total: 1,
		metrics: {
			places: 1,
			dated: 0,
			first_day: null,
			last_day: null,
			rated: 0,
			rating_mean: null,
			rating_counts: {},
			with_text: 1,
		},
		rows: [
			{
				version_id: version,
				location_id: "loc",
				location_name: marker,
				position: 1,
				day: null,
				rating: null,
				has_text: true,
				specificity: 0,
			},
		],
	};
	let status = 200,
		stale = false,
		release;
	const delay = new Promise((resolve) => {
		release = resolve;
	});
	const requests = [];
	await page.route("**/reviewer-context?*", async (route) => {
		requests.push(route.request().url());
		if (stale && route.request().url().includes(snapshot)) await delay;
		const code = route.request().url().includes(other) ? 404 : status;
		try {
			await route.fulfill({
				status: code,
				json: code === 200 ? doc : { error: "not_found" },
			});
		} catch {
			/* scope change cancelled the pending fetch */
		}
	});
	try {
		const url = new URL("cabinet/?" + params, origin);
		const found = page.getByText(marker, { exact: false });
		await page.goto(url.href);
		await found.waitFor();
		status = 403;
		await page.evaluate(() => dispatchEvent(new Event("focus")));
		await page.getByRole("alert").waitFor();
		assert.equal(await found.count(), 0);
		status = 200;
		await page.goto(url.href);
		await found.waitFor();
		stale = true;
		await page.evaluate(() => dispatchEvent(new Event("focus")));
		await page
			.getByRole("status")
			.filter({ hasText: "Проверяем допуск" })
			.waitFor();
		await page.evaluate((s) => {
			const changed = new URL(location.href);
			changed.searchParams.set("snapshot", s);
			history.pushState(null, "", changed);
			dispatchEvent(new PopStateEvent("popstate"));
		}, other);
		await page.getByRole("alert").waitFor();
		release();
		assert.equal(await found.count(), 0);
		assert(!requests.some((u) => u.includes(encodeURIComponent(marker))));
		assert.equal(await page.locator("a[href*='public-reviewers']").count(), 0);
	} finally {
		release();
		await page.close();
	}
}
