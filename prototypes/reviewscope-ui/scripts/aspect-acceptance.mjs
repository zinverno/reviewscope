import assert from "node:assert/strict";
import { join } from "node:path";

// Only the authored synthetic scenario is recorded, after the local TLS sign-in.
export async function checkAspects(browser, storageState, start, media, stage) {
	const engine = browser.browserType().name(),
		origin = new URL(start).origin;
	const context = await browser.newContext({
		storageState,
		locale: "ru-RU",
		reducedMotion: "reduce",
		...(media
			? { recordVideo: { dir: media, size: { width: 1024, height: 768 } } }
			: {}),
	});
	await context.route("**/*", (route) =>
		new URL(route.request().url()).origin === origin
			? route.continue()
			: route.abort(),
	);
	const page = await context.newPage();
	context.setDefaultTimeout(10000);
	try {
		stage("open");
		await page.goto(start);
		await page
			.getByRole("link", { name: "Выбрать определение аспекта" })
			.click();
		await page
			.getByRole("button", { name: "Создать аспект", exact: true })
			.press("Enter");
		const title = "Выдача готового заказа · " + engine;
		await page.getByLabel("Короткое имя").fill(title);
		await page
			.getByLabel("Определение", { exact: true })
			.fill("Синтетический пример ожидания после готовности.");
		await page
			.getByLabel("Включающие примеры, по одному на строку")
			.fill("Пакет был готов. " + "Я".repeat(440));
		await page
			.getByLabel("Исключающие примеры, по одному на строку")
			.fill("<b>Очередь до заказа</b>");
		await page
			.getByRole("button", { name: "Подтвердить сохранение" })
			.press("Enter");
		const card = page.getByRole("article", { name: "Определение аспекта" });
		await card.getByText(title + " · v1").waitFor();
		assert.equal(await card.locator("b").count(), 0);
		for (const width of [320, 390, 1366, 1920]) {
			await page.setViewportSize({ width, height: 768 });
			assert.deepEqual(
				await page.evaluate(() => [
					innerWidth,
					document.documentElement.scrollWidth <= innerWidth + 1,
				]),
				[width, true],
			);
		}
		if (media)
			await card.screenshot({ path: join(media, "aspect-" + engine + ".png") });
		stage("bind");
		const definitionURL = page.url();
		await page
			.getByRole("link", { name: "Искать по этой версии" })
			.press("Enter");
		await page.getByLabel("Формулировка или пример").fill("Ждать не пришлось");
		await page.getByRole("button", { name: "Найти кандидатов" }).press("Enter");
		await page
			.getByRole("link", { name: "Фрагмент 1 · отзыв 1", exact: true })
			.click({ timeout: 30000 });
		await page.locator("mark").waitFor();
		const original = page.url();
		stage("edit");
		await page.goto(definitionURL);
		await page
			.getByRole("button", { name: "Создать следующую версию" })
			.click();
		await page
			.getByLabel("Определение", { exact: true })
			.fill("Уточнённая граница готовности.");
		await page.getByRole("button", { name: "Подтвердить сохранение" }).click();
		await card.getByText(title + " · v2").waitFor();
		stage("reopen v1");
		await page.goto(original);
		await card.getByText(title + " · v1").waitFor();
		await page.locator("mark").waitFor();
		assert.equal(
			await page.getByText("Уточнённая граница готовности.").count(),
			0,
		);
		assert(!decodeURIComponent(page.url()).includes("готов"));
		stage("proposal");
		await page.goto(origin + "/cabinet/?view=legacy");
		await page
			.getByRole("link", { name: /Предложить аспект из темы/ })
			.first()
			.click();
		const workspace = new URL(start).searchParams.get("workspace");
		if ((await page.getByLabel("Рабочая область").inputValue()) !== workspace)
			await page.getByLabel("Рабочая область").selectOption(workspace);
		await page.getByText(/Предложение из публичного демо/).waitFor();
		await page.getByRole("link", { name: "Отменить", exact: true }).click();
		await page
			.getByRole("button", { name: "Создать аспект", exact: true })
			.waitFor();
	} finally {
		await context.close();
	}
	if (media)
		await page.video().saveAs(join(media, "aspect-" + engine + ".webm"));
}
