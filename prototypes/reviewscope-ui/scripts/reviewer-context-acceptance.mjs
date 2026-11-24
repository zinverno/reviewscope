import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

export async function checkPublicReviewer(page, origin) {
	const link = (name) => page.getByRole("link", { name, exact: true });
	const history = page.getByRole("heading", {
		name: "История отзывов",
		exact: true,
	});
	const p2 = JSON.parse(
		await readFile(
			new URL("../src/overview-sources/p2.json", import.meta.url),
			"utf8",
		),
	);
	await page.goto(origin + "/cabinet/?view=legacy&place=p1&ranking=five");
	await link("Тема 3 · 142 отзывов").press("Enter");
	await link("Отзыв r00027").click();
	await page.locator(".source-text").waitFor();
	const initial = page.url();
	await link("Контекст автора в этом наборе").press("Enter");
	await history.waitFor();
	const profile = page.url();
	await page
		.getByRole("img", { name: "Координаты точек из таблицы" })
		.scrollIntoViewIfNeeded();
	await link("Исходный отзыв r00157").press("Enter");
	await page.locator(".source-text").waitFor();
	assert.equal(
		await page.locator(".source-text").textContent(),
		p2.reviews.r00157.text,
	);
	assert.equal(
		new URL(page.url()).searchParams.get("evidenceVersion"),
		p2.reviews.r00157.version,
	);
	assert.equal(new URL(page.url()).searchParams.get("evidencePlace"), "p2");
	const evidence = page.url();
	await page.goBack();
	await history.waitFor();
	assert.equal(page.url(), profile);
	await page.goForward();
	await page.locator(".source-text").waitFor();
	assert.equal(page.url(), evidence);
	await page.goBack();
	await link("Вернуться к исходному экрану").click();
	await page.locator(".source-text").waitFor();
	assert.equal(page.url(), initial);
}

export async function checkSnapshotReviewer(page, origin, fixture, snapshots) {
	const detail = page.getByRole("article", { name: "Доказательство отзыва" });
	const enter = page.getByRole("link", {
		name: "Контекст автора в этом снимке",
		exact: true,
	});
	const base = new URL("/cabinet/", origin);
	base.search = new URLSearchParams({
		view: "reviews",
		workspace: fixture.w,
		source: fixture.s,
		snapshot: fixture.authorSnapshot,
		review: fixture.authorVersions[0],
		position: "1",
	});
	await page.goto(base.href);
	await detail.waitFor();
	await enter.press("Enter");
	const target = page.locator(
		'a[href*="authorVersion=' + fixture.authorVersions[1] + '"]',
	);
	await target.first().click();
	await detail.waitFor();
	assert((await detail.innerText()).includes(fixture.authorVersions[1]));
	await page.goBack();
	await target.first().waitFor();
	await page.goBack();
	await detail.waitFor();
	assert.equal(page.url(), base.href);
	for (const name of ["a", "b", "json"]) {
		const u = new URL(base);
		u.searchParams.set("snapshot", snapshots.get(name));
		u.searchParams.delete("position");
		u.searchParams.delete("review");
		await page.goto(u.href);
		await page
			.getByRole("link", { name: /^Позиция/ })
			.first()
			.click();
		await enter.click();
		await page
			.getByText(/NO_REVIEWER_ID/)
			.first()
			.waitFor();
		assert.equal(await page.locator('a[href*="authorVersion="]').count(), 0);
		await page.goBack();
		await detail.waitFor();
	}
}
