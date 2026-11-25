import assert from "node:assert/strict";
import { join } from "node:path";

// All inputs are authored public synthetic fixtures. Recording begins after sign-in.
export async function checkDecisions(
	browser,
	storageState,
	start,
	media,
	stage,
) {
	const engine = browser.browserType().name();
	const origin = new URL(start).origin;
	const context = await browser.newContext({
		storageState,
		locale: "ru-RU",
		reducedMotion: "reduce",
	});
	await context.route("**/*", (route) =>
		new URL(route.request().url()).origin === origin
			? route.continue()
			: route.abort(),
	);
	const page = await context.newPage();
	context.setDefaultTimeout(15000);
	const errors = [];
	const reads = [];
	page.on("response", async (response) => {
		const path = new URL(response.url()).pathname;
		if (path.includes("/decisions/")) {
			const value = await response.json().catch(() => ({}));
			const state = [
				"queued",
				"running",
				"succeeded",
				"failed",
				"cancelled",
			].includes(value.status)
				? value.status
				: "";
			reads.push([response.status(), state]);
		}
	});
	page.on("pageerror", (e) => errors.push(e.name));
	try {
		stage("create supported immutable version");
		await page.goto(start);
		await page
			.getByRole("link", { name: "Выбрать аспект для оценки всего снимка" })
			.press("Enter");
		await page
			.getByRole("button", { name: "Создать аспект", exact: true })
			.press("Enter");
		await page.getByLabel("Начальное определение").selectOption("queue");
		await page
			.getByRole("button", { name: "Подтвердить сохранение" })
			.press("Enter");
		const card = page.getByRole("article", { name: "Определение аспекта" });
		await card
			.getByRole("heading", { name: "Очередь до оформления заказа · v1" })
			.waitFor();
		const definitionURL = page.url();
		await page
			.getByRole("link", { name: "Оценка по всему снимку" })
			.press("Enter");
		stage("explicit full snapshot launch and retry same command");
		const route = "**/api/v1/workspaces/*/snapshots/*/decisions";
		let body, id;
		const first = async (interception) => {
			if (interception.request().method() !== "POST")
				return interception.fallback();
			body = interception.request().postDataJSON();
			const received = await interception.fetch();
			assert.equal(received.status(), 202);
			id = (await received.json()).id;
			// The server committed the command but its response was lost in transit.
			await interception.abort("failed");
			await page.unroute(route, first);
		};
		await page.route(route, first);
		const submit = page.getByRole("button", {
			name: "Оценить все пригодные отзывы",
		});
		stage("intercept explicit launch");
		await submit.press("Enter");
		stage("observe lost response");
		await page.locator(".evidence [role=alert]").waitFor();
		await submit.waitFor();
		stage("retry exact command");
		const [repeat] = await Promise.all([
			page.waitForResponse(
				(r) =>
					r.request().method() === "POST" &&
					new URL(r.url()).pathname.endsWith("/decisions"),
			),
			submit.press("Enter"),
		]);
		assert.deepEqual(repeat.request().postDataJSON(), body);
		assert.equal(repeat.status(), 200);
		await page.waitForURL((url) => url.searchParams.get("decisionRun") === id);
		stage("read completed summary");
		const summary = page.getByRole("region", { name: "Итог оценки" });
		await summary
			.getByText(
				/Принято 28, пригодно 27, обработано 27, без текста 1, не обработано 0/,
			)
			.waitFor({ timeout: 45000 });
		stage("verify pages and filter");
		if (media)
			await summary.screenshot({
				path: join(media, "aspect-decision-summary-" + engine + ".png"),
			});
		const list = page.getByRole("list", { name: "Решения по отзывам" });
		assert.equal(await list.getByRole("link").count(), 25);
		const firstIDs = new Set(await list.getByRole("link").allTextContents());
		await page.getByRole("link", { name: "Следующие 25" }).press("Enter");
		await page.getByRole("link", { name: "Предыдущие 25" }).waitFor();
		const rest = await list.getByRole("link").allTextContents();
		assert.equal(rest.length, 3);
		assert(rest.every((s) => !firstIDs.has(s)));
		await list.getByRole("link").first().press("Enter");
		const article = page.getByRole("article", {
			name: "Решение по версии отзыва",
		});
		await article.waitFor();
		await page.goBack();
		await page.getByRole("link", { name: "Предыдущие 25" }).waitFor();
		await page.goForward();
		await article.waitFor();
		await page.getByRole("link", { name: "К списку решений" }).press("Enter");
		await page.getByLabel("Исход решения").selectOption("excluded_blank");
		await page
			.getByText("Отзывов с выбранным исходом во всём снимке — 1.")
			.waitFor();
		await list.getByRole("link", { name: /Нет текста/ }).press("Enter");
		await article.getByText("Текст отсутствует.").waitFor();
		await page.goBack();
		await page.getByLabel("Исход решения").selectOption("");
		await page
			.getByText("Отзывов с выбранным исходом во всём снимке — 28.")
			.waitFor();
		const entry = list
			.getByRole("link")
			.filter({ hasNotText: "Нет текста" })
			.first();
		await entry.press("Enter");
		await article.waitFor();
		const detailURL = page.url();
		const version = new URL(detailURL).searchParams.get("decisionReview");
		await article
			.getByRole("link", { name: "Открыть исходную версию отзыва" })
			.press("Enter");
		assert.equal(new URL(page.url()).searchParams.get("review"), version);
		await page.getByRole("link", { name: "К списку с фильтрами" }).waitFor();
		await page.goBack();
		await article.waitFor();
		stage("inspect four widths and restore exact v1 after edit");
		for (const width of [320, 390, 1366, 1920]) {
			await page.setViewportSize({ width, height: 768 });
			assert(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth + 1,
				),
			);
		}
		if (media)
			await article.screenshot({
				path: join(media, "aspect-decision-" + engine + ".png"),
			});
		await page.goto(definitionURL);
		await page
			.getByRole("button", { name: "Создать следующую версию" })
			.press("Enter");
		await page
			.getByLabel("Определение", { exact: true })
			.fill("Другая граница решения в эксперименте.");
		await page
			.getByRole("button", { name: "Подтвердить сохранение" })
			.press("Enter");
		await card
			.getByRole("heading", { name: "Очередь до оформления заказа · v2" })
			.waitFor();
		await page
			.getByRole("link", { name: "Оценка по всему снимку" })
			.press("Enter");
		await page.getByText(/Эта версия определения не поддерживается/).waitFor();
		assert(await submit.isDisabled());
		await page.goto(detailURL);
		await card
			.getByRole("heading", { name: "Очередь до оформления заказа · v1" })
			.waitFor();
		await article.waitFor();
		const invalid = new URL(detailURL);
		invalid.searchParams.set(
			"decisionReview",
			"00000000-0000-0000-0000-000000000000",
		);
		await page.goto(invalid.href);
		await page.locator(".evidence [role=alert]").waitFor();
		assert.equal(await article.count(), 0);
		assert.deepEqual(errors, []);
		return detailURL;
	} catch (error) {
		if (media)
			await page.screenshot({
				path: join(media, "decision-failure-" + engine + ".png"),
			});
		stage(
			"result diagnostic " +
				JSON.stringify(reads.slice(-12)) +
				" · " +
				error.name,
		);
		throw error;
	} finally {
		await context.close();
	}
}
