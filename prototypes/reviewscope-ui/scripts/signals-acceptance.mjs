import assert from "node:assert/strict";
import { join } from "node:path";
import { readFile } from "node:fs/promises";

export async function checkSignals(
	page,
	origin,
	fixture,
	snapshots,
	step,
	media,
) {
	const cabinet = (params) =>
		origin + "/cabinet/?" + new URLSearchParams(params);
	step("public overview");
	await page.goto(cabinet({ view: "legacy", place: "p1" }));
	step("public overview link");
	await page
		.getByRole("link", { name: "Сигналы и оценки точки" })
		.press("Enter");
	step("public ratings");
	await page.waitForURL((u) => u.searchParams.get("view") === "public-signals");
	const base = page.url();
	await page.getByText(/Среднее исходных оценок 4,19/).waitFor();
	await page
		.getByText(/^Всплеск ·/)
		.first()
		.click();
	await page.getByRole("link", { name: "Состав события" }).first().click();
	step("event cohort");
	await page.getByText(/FILTER_NOT_COMPUTED/).waitFor();
	const p1 = JSON.parse(
		await readFile(
			new URL("../src/demo-signals/p1.json", import.meta.url),
			"utf8",
		),
	);
	const ids = new Set(p1.bursts[0].event.ids);
	const cohort = p1.reviews.filter((r) => ids.has(r.id) && r.rating !== null);
	const den = cohort.reduce((sum, r) => sum + r.weight.weight, 0);
	const mean =
		cohort.reduce((sum, r) => sum + r.rating * r.weight.weight, 0) / den;
	await page
		.getByText("Оценено " + cohort.length + " из " + ids.size + ".", {
			exact: false,
		})
		.waitFor();
	await page
		.getByText(
			"взвешенное среднее " +
				mean.toLocaleString("ru", { maximumFractionDigits: 2 }) +
				" ★",
			{ exact: false },
		)
		.waitFor();
	const first = page
		.locator("summary")
		.filter({ hasText: /· вес / })
		.first();
	await first.press("Enter");
	const detail = page.getByRole("link", { name: /^Исходный отзыв/ }).first();
	await detail.click();
	await page.getByRole("heading", { name: /Исходный отзыв/ }).waitFor();
	await page.getByRole("link", { name: "Вернуться к вкладам" }).click();
	await page.getByRole("link", { name: "Снять фильтры" }).click();
	step("keyboard explanation and widths");
	for (const width of [320, 390, 1366, 1920]) {
		await page.setViewportSize({ width, height: 800 });
		const row = page
			.locator("summary")
			.filter({ hasText: /· вес / })
			.first();
		if (
			!(await page
				.getByText("Расчёт веса", { exact: true })
				.first()
				.isVisible())
		)
			await row.press("Enter");
		const explain = page.getByText("Расчёт веса", { exact: true }).first();
		if (!(await explain.locator("..").evaluate((e) => e.open)))
			await explain.press("Enter");
		assert(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= window.innerWidth,
			),
		);
		if (media) {
			await explain.scrollIntoViewIfNeeded();
			await page.screenshot({
				path: join(
					media,
					"signals-" +
						page.context().browser().browserType().name() +
						"-" +
						width +
						".png",
				),
			});
		}
	}
	step("paging and empty selection");
	await page.getByRole("link", { name: "Следующая страница" }).click();
	await page.getByText(/Страница 2 из/).waitFor();
	await page.getByLabel("Фильтр оценки").selectOption("missing");
	await page.getByText("В выборке нет записей.").waitFor();
	await page.getByLabel("Точка расчёта").selectOption("p2");
	await page.getByRole("heading", { name: "События" }).waitFor();
	const invalid = new URL(base);
	invalid.searchParams.set("result", "0".repeat(64));
	await page.goto(invalid.href);
	await page.getByRole("alert").waitFor();
	for (const name of ["a", "json"]) {
		step("snapshot " + name);
		const scope = {
			workspace: fixture.w,
			source: fixture.s,
			snapshot: snapshots.get(name),
		};
		await page.goto(cabinet({ view: "overview", ...scope }));
		await page.getByRole("link", { name: "Сигналы и оценки снимка" }).click();
		await page
			.getByRole("button", { name: "Рассчитать сигналы снимка" })
			.press("Enter");
		step("read " + name);
		await page.getByText(/Взвешенное среднее Н\/Д/).waitFor({ timeout: 30000 });
		const run = new URL(page.url()).searchParams.get("run");
		assert(run);
		if (name === "json") {
			await page
				.getByText(/Принято 6 из 12. Карантин 5, подтверждённые повторы 1/)
				.waitFor();
			await page.getByLabel("Точка расчёта").selectOption("p2");
			await page.getByText(/оценено 0 из 1/).waitFor();
		} else {
			await page.getByText(/Среднее исходных оценок 3,6667/).waitFor();
			await page.getByLabel("Фильтр оценки").selectOption("5");
			await page.getByText(/оценено 1 из 1/).waitFor();
		}
		await page
			.getByRole("link", { name: /^Исходная версия/ })
			.first()
			.click();
		await page.getByText("Вернуться к сигналам запуска").click();
		assert.equal(new URL(page.url()).searchParams.get("run"), run);
		await page.getByText(/Взвешенное среднее Н\/Д/).waitFor();
		await page.goto(
			cabinet({ view: "signals", ...scope, run: crypto.randomUUID() }),
		);
		await page.getByText("Проверка недоступна в выбранной области.").waitFor();
	}
	await page.goto(
		cabinet({
			view: "signals",
			workspace: fixture.emptyWorkspace,
			source: fixture.s,
			snapshot: snapshots.get("a"),
		}),
	);
	await page.getByText("Проверка недоступна в выбранной области.").waitFor();
}
