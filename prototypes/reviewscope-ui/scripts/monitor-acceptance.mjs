import { setFixtureMode } from "./poll-acceptance.mjs";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

export async function checkMonitors(
	page,
	origin,
	fixture,
	engine,
	media,
	stage,
) {
	const scope = new URLSearchParams({
		workspace: fixture.w,
		source: fixture.s,
		project: fixture.monitorProject,
	});
	const home = origin + "/cabinet/?" + scope;
	const aspects =
		"/api/v1/workspaces/" +
		fixture.w +
		"/projects/" +
		fixture.monitorProject +
		"/aspects";
	const saveAspect = (payload) =>
		page.evaluate(
			async ({ aspects, payload }) => {
				const reply = await fetch(aspects, {
					method: "POST",
					headers: { "Content-Type": "application/json", "X-RS-CSRF": "1" },
					body: JSON.stringify({
						payload,
						idempotency_key: crypto.randomUUID(),
					}),
				});
				if (reply.status !== 201) throw Error("aspect create " + reply.status);
				return reply.json();
			},
			{ aspects, payload },
		);
	const definition = await saveAspect(fixture.monitorDefinition);
	const upload = async (name) => {
		stage("upload " + name);
		await page.goto(home);
		await page.getByLabel("p1", { exact: true }).check();
		await page.getByLabel("p2", { exact: true }).check();
		await page
			.getByLabel("Файл", { exact: true })
			.setInputFiles(join(fixture.csv, name + ".csv"));
		if (!(await page.getByLabel("Mapping (JSON)", { exact: true }).isVisible()))
			await page.getByText("Сопоставление и паспорт", { exact: true }).click();
		await page
			.getByLabel("Mapping (JSON)", { exact: true })
			.fill(await readFile(join(fixture.csv, "mapping.json"), "utf8"));
		await page
			.getByLabel("Паспорт (JSON)", { exact: true })
			.fill(await readFile(join(fixture.csv, name + ".passport.json"), "utf8"));
		await page
			.getByRole("button", { name: "Предпросмотр", exact: true })
			.click();
		await page
			.getByRole("button", { name: "Подтвердить и создать снимок" })
			.click();
		const open = page.getByRole("link", { name: "Открыть снимок" });
		await open.waitFor();
		return new URL(await open.getAttribute("href"), origin).searchParams.get(
			"snapshot",
		);
	};
	stage("save monitor");
	await page.getByRole("link", { name: "Сохранённые мониторы" }).press("Enter");
	await page
		.getByLabel("Название", { exact: true })
		.fill("Очередь · " + engine);
	await page
		.getByLabel("Сохранённый аспект")
		.selectOption(definition.aspect_id + "/" + definition.version_id);
	await page
		.getByRole("button", { name: "Сохранить монитор", exact: true })
		.press("Enter");
	await page
		.getByRole("heading", { name: "Очередь · " + engine + " · v1" })
		.waitFor();
	const monitor = page.url();
	const enqueue = async (snap) => {
		await page.goto(monitor);
		stage("select admitted delivery");
		const select = page.getByLabel("Поставка", { exact: true });
		await select.waitFor();
		await select.selectOption(snap);
		stage("enqueue");
		await page
			.getByRole("button", { name: "Проверить поставку" })
			.press("Enter");
		await page.waitForURL((url) => url.searchParams.has("monitorRun"));
	};
	const run = async (snap) => {
		await enqueue(snap);
		stage("open succeeded run");
		await page
			.getByRole("link", { name: "Решения и доказательства PR-22" })
			.waitFor({ timeout: 20000 });
		return page.url();
	};
	const a = await upload("a");
	stage("cancel queued request");
	const mode = (value) => setFixtureMode(fixture.pollControl, value);
	await mode("pause");
	await enqueue(a);
	await page.getByRole("button", { name: "Отменить запуск" }).press("Enter");
	await page.getByRole("link", { name: /^Отменён ·/ }).waitFor();
	await mode("ready");
	stage("first result");
	const runA = await run(a);
	const b = await upload("b");
	stage("revoked attempt");
	await mode("pause");
	await enqueue(b);
	await mode("monitor_denied");
	await mode("ready");
	await page.reload();
	await page.getByText(/Ошибка · verify ·.*ACCESS_REVOKED/).waitFor();
	stage("incremental result");
	const runB = await run(b);
	await page.getByText(/Пригодных переиспользовано 2, рассчитано 4/).waitFor();
	await page.getByText(/Новой поставки нет/).waitFor();
	for (const width of [320, 390, 1366, 1920]) {
		await page.setViewportSize({ width, height: 800 });
		assert(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= innerWidth + 2,
			),
		);
		if (media)
			await page.screenshot({
				path: join(media, `monitors-${engine}-${width}.png`),
			});
	}
	stage("historical result and browser history");
	await page.goto(runA);
	await page.getByText(/Пригодных переиспользовано 0, рассчитано 3/).waitFor();
	await page.goBack();
	assert.equal(page.url(), runB);
	await page.goForward();
	assert.equal(page.url(), runA);
	stage("revoke visible result");
	await mode("revoked");
	await page
		.getByRole("link", { name: "Решения и доказательства PR-22" })
		.waitFor({ state: "detached", timeout: 15000 });
	await mode("ready");
	stage("new version and archive");
	await page.goto(monitor);
	const custom = await saveAspect({
		...fixture.monitorDefinition,
		name: "Свой аспект " + engine,
	});
	await page.reload();
	await page
		.getByLabel("Сохранённый аспект")
		.selectOption(custom.aspect_id + "/" + custom.version_id);
	await page.getByRole("button", { name: "Сохранить редакцию" }).press("Enter");
	await page
		.getByRole("heading", { name: "Очередь · " + engine + " · v2" })
		.waitFor();
	await page
		.getByText(/Определение не поддерживается/)
		.first()
		.waitFor();
	assert(
		await page.getByRole("button", { name: "Проверить поставку" }).isDisabled(),
	);
	await page
		.getByRole("button", { name: "Архивировать монитор" })
		.press("Enter");
	await page.getByText(/В архиве\. Последняя/).waitFor();
}
