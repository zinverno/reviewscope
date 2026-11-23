import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

export async function checkSources(page, origin) {
	const doc = JSON.parse(
		await readFile(
			new URL("../src/overview-sources/p1.json", import.meta.url),
			"utf8",
		),
	);
	const at = (name) => page.getByRole("link", { name, exact: true });
	await page.setViewportSize({ width: 320, height: 768 });
	await page.goto(origin + "/cabinet/?view=legacy&place=p1&ranking=five");
	const topic = at("Тема 3 · 142 отзывов");
	await topic.focus();
	await page.keyboard.press("Enter");
	await page.getByRole("heading", { name: "Тема 3", exact: true }).waitFor();
	const seen = [];
	let detail;
	for (let n = 1; n <= Math.ceil(doc.topics["3"].length / 12); n++) {
		const rows = page.getByRole("list").getByRole("link");
		await rows.first().waitFor();
		seen.push(
			...(await rows.allTextContents()).map((v) => v.replace(/^Отзыв /, "")),
		);
		if (await at("Отзыв r00066").count())
			detail = await at("Отзыв r00066").getAttribute("href");
		if (n * 12 < doc.topics["3"].length) await at("Следующая страница").click();
	}
	assert.deepEqual(seen, doc.topics["3"]);
	await page.goto(new URL(detail, origin).href);
	const source = page.locator(".source-text");
	await source.waitFor();
	assert.equal(await source.textContent(), doc.reviews.r00066.text);
	await page.getByText("Версия и источник", { exact: true }).click();
	await page
		.getByText("Версия публичного отзыва " + doc.reviews.r00066.version)
		.waitFor();
	assert.equal(
		await page.evaluate(
			() => document.documentElement.scrollWidth <= innerWidth,
		),
		true,
	);
	const address = page.url();
	await at("Вернуться к составу · страница 4").click();
	await at("Отзыв r00066").waitFor();
	await page.goBack();
	await source.waitFor();
	assert.equal(page.url(), address);
	await page.goForward();
	await at("Отзыв r00066").waitFor();
	await at("Вернуться в обзор точки").click();
	assert.equal(new URL(page.url()).searchParams.get("ranking"), "five");
	for (const [group, term] of [
		["keywords", "советую"],
		["star-5", "советую"],
		["emerging", "место"],
	]) {
		const selector = 'a[href*="group=' + group + '&"]';
		const link = page
			.locator(selector)
			.filter({ hasText: new RegExp("^" + term + "$") });
		if (group === "star-5")
			await page
				.getByText("Тональность по оценке и ключевые слова по звёздам")
				.click();
		await link.click();
		await page
			.getByText(
				new RegExp(
					"Отзывов в выборке: " + doc.terms[group][term].length + "\\.",
				),
			)
			.waitFor();
		await at("Отзыв " + doc.terms[group][term][0]).click();
		await source.waitFor();
		assert.equal(
			await source.textContent(),
			doc.reviews[doc.terms[group][term][0]].text,
		);
		await page.goBack();
		await at("Вернуться в обзор точки").click();
	}
	for (const [key, value] of [
		["result", "invalid"],
		["dataset", "invalid"],
		["snapshot", "00000000-0000-4000-8000-000000000000"],
		["item", "unknown"],
		["review", "absent"],
		["version", "old"],
	]) {
		const u = new URL(address);
		u.searchParams.set(key, value);
		await page.goto(u.href);
		await page
			.getByRole("alert")
			.filter({ hasText: "публичного демо недоступна" })
			.waitFor();
		assert.equal(await source.count(), 0);
	}
}
