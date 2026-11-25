import assert from "node:assert/strict";
import { readFile, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export async function checkHumanReview(
	browser,
	owner,
	machineURL,
	fixture,
	media,
	stage,
) {
	const origin = new URL(machineURL).origin;
	async function session(options) {
		const c = await browser.newContext({
			locale: "ru-RU",
			reducedMotion: "reduce",
			...options,
		});
		await c.route("**/*", (route) =>
			new URL(route.request().url()).origin === origin
				? route.continue()
				: route.abort(),
		);
		c.setDefaultTimeout(12000);
		return c;
	}
	const context = await session({ storageState: owner }),
		analyst = await session({});
	const page = await context.newPage();
	const section = page.getByRole("region", {
		name: "Ручная проверка",
		exact: true,
	});
	const button = (name) => section.getByRole("button", { name, exact: true });
	const link = (name) => section.getByRole("link", { name, exact: true });
	const queue = section.getByRole("list", { name: "Версии для проверки" });
	const history = section.getByRole("list", { name: "История решений" });
	const editor = section.getByRole("form", { name: "Решение человека" });
	const frozen = section.getByRole("article", { name: "Проверенная редакция" });
	async function admission(value) {
		const path = fixture.pollControl;
		await writeFile(path + ".next", value);
		await rename(path + ".next", path);
		for (let n = 0; n < 150; n++) {
			if ((await readFile(path + ".ack", "utf8").catch(() => "")) === value)
				return;
			await delay(60);
		}
		throw new Error("disposable admission control timeout");
	}
	async function publish() {
		await link("Сохранённые редакции").press("Enter");
		await button("Зафиксировать проверенную редакцию").press("Enter");
		await frozen.waitFor();
		return page.url();
	}
	async function checkWidths() {
		for (const width of [320, 390, 1366, 1920]) {
			await page.setViewportSize({ width, height: 768 });
			assert(
				await page.evaluate(
					() =>
						matchMedia("(prefers-reduced-motion: reduce)").matches &&
						document.documentElement.scrollWidth <= innerWidth + 1,
				),
			);
		}
	}
	async function openMissedComplaint() {
		const candidates = await queue
			.getByRole("link")
			.evaluateAll((nodes) => nodes.map((node) => node.href));
		for (const href of candidates.slice(0, 2)) {
			await page.goto(href);
			await editor.waitFor();
			if ((await editor.locator("blockquote").innerText()).startsWith("🍒"))
				break;
		}
		assert((await editor.locator("blockquote").innerText()).startsWith("🍒"));
	}
	async function save(action, label, reason, code) {
		await editor.getByLabel("Действие").selectOption(action);
		if (label) await editor.getByLabel("Исход человека").selectOption(label);
		await editor.getByLabel("Причина проверки").fill(reason);
		const [res] = await Promise.all([
			page.waitForResponse(
				(r) =>
					r.request().method() === "POST" &&
					new URL(r.url()).pathname.endsWith("/events"),
			),
			button("Сохранить решение").press("Enter"),
		]);
		assert.equal(res.status(), code);
	}
	try {
		stage("open machine result and complete control plan");
		await page.goto(machineURL);
		await page
			.getByRole("link", { name: "Ручная проверка", exact: true })
			.press("Enter");
		await link("Контрольные выборки").press("Enter");
		await section.getByLabel("Размер выборки").fill("27");
		await button("Создать контрольную выборку").press("Enter");
		await section.getByText("По фильтрам 27 из 27", { exact: false }).waitFor();
		stage("fixed plan first page");
		await checkWidths();
		const sampleURL = page.url(),
			ids = await queue.getByRole("link").allTextContents();
		assert.equal(ids.length, 25);
		await link("Следующие 25").press("Enter");
		await link("Предыдущие 25").waitFor();
		stage("fixed plan last page");
		const last = await queue.getByRole("link").allTextContents();
		assert.equal(last.length, 2);
		assert(last.every((id) => !ids.includes(id)));
		await page.goBack();
		await queue.waitFor();
		assert.deepEqual(await queue.getByRole("link").allTextContents(), ids);
		await page.goForward();
		await link("Предыдущие 25").waitFor();
		await page.goto(sampleURL);
		await queue.waitFor();
		stage("select negative from original source");
		await openMissedComplaint();
		await checkWidths();
		const versionURL = page.url(),
			sel = new URL(versionURL).searchParams;
		const root =
			origin +
			"/api/v1/workspaces/" +
			sel.get("workspace") +
			"/snapshots/" +
			sel.get("snapshot") +
			"/decisions/" +
			sel.get("decisionRun") +
			"/human";
		const headers = { "X-RS-CSRF": "1", Origin: origin };
		stage("second authenticated session");
		const logged = await analyst.request.post(
			origin + "/api/v1/session/login",
			{
				headers,
				data: {
					login: "human-review-analyst",
					password: fixture.humanAnalystPassword,
				},
			},
		);
		assert.equal(logged.status(), 204);
		stage("analyst confirmation owner stale form and conscious dispute");
		const committed = await analyst.request.post(
			root + "/reviews/" + sel.get("decisionReview") + "/events",
			{
				headers,
				data: {
					source_id: sel.get("source"),
					idempotency_key: crypto.randomUUID(),
					expected_event_id: null,
					action: "confirm",
					outcome: "not_mentioned",
					reason: "Контроль автора синтетического примера",
					spans: [],
					selection: "sample",
					plan_id: sel.get("reviewPlan"),
				},
			},
		);
		assert.equal(committed.status(), 201);
		await save("dispute", null, "Синтетический разбор", 409);
		await button("Перечитать историю").press("Enter");
		await history.getByText("Контроль автора синтетического примера").waitFor();
		await save(
			"dispute",
			null,
			"Требуется уточнение синтетической позиции",
			201,
		);
		await history
			.getByText("Требуется уточнение синтетической позиции")
			.waitFor();
		stage("resolve missed complaint with Unicode source interval");
		await editor.getByLabel("Действие").selectOption("correct");
		await editor.getByLabel("Исход человека").selectOption("complaint");
		await editor.getByLabel("Начало 1").fill("2");
		await editor.getByLabel("Конец 1").fill("9");
		assert.equal(
			await editor.getByLabel("Предпросмотр цитаты 1").textContent(),
			"У кассы",
		);
		await save(
			"correct",
			"complaint",
			"Свидетельство в исходном тексте очереди",
			201,
		);
		await history
			.getByText("Свидетельство в исходном тексте очереди")
			.waitFor();
		assert.equal(await history.getByRole("listitem").count(), 3);
		stage("freeze first revision select negative and confirm another review");
		const first = await publish();
		await frozen
			.getByRole("heading", { name: "По меткам человека жалоб — 1" })
			.waitFor();
		await frozen
			.getByText(
				/^Просмотрено 1 из 27\. Разрешено 1, в споре 0, без проверки 26/,
			)
			.waitFor();
		await page.goto(sampleURL);
		await queue.waitFor();
		await section.getByLabel("Состояние проверки").selectOption("unreviewed");
		await section.getByLabel("Машинный исход").selectOption("not_mentioned");
		await openMissedComplaint();
		await save("confirm", null, "Проверка следующей синтетической записи", 201);
		await history
			.getByText("Проверка следующей синтетической записи")
			.waitFor();
		await editor.getByLabel("Действие").selectOption("correct");
		await editor.getByLabel("Исход человека").selectOption("complaint");
		await editor.getByLabel("Начало 1").fill("2");
		await editor.getByLabel("Конец 1").fill("9");
		await save(
			"correct",
			"complaint",
			"Уточнённая позиция второго синтетического отзыва",
			201,
		);
		await history
			.getByText("Уточнённая позиция второго синтетического отзыва")
			.waitFor();
		const second = await publish();
		assert.notEqual(second, first);
		await frozen
			.getByRole("heading", { name: "По меткам человека жалоб — 2" })
			.waitFor();
		await frozen
			.getByText(
				/^Просмотрено 2 из 27\. Разрешено 2, в споре 0, без проверки 25/,
			)
			.waitFor();
		const resultPage = await context.newPage();
		await resultPage.goto(first);
		await resultPage
			.getByText(
				/^Просмотрено 1 из 27\. Разрешено 1, в споре 0, без проверки 26/,
			)
			.waitFor();
		await resultPage.close();
		stage("viewport keyboard and synthetic aggregate media");
		await checkWidths();
		if (media) {
			const engine = browser.browserType().name();
			const receipt = {
				engine,
				run_id: sel.get("decisionRun"),
				plan_id: sel.get("reviewPlan"),
				eligible: 27,
				conflict_status: 409,
				revisions: [],
			};
			for (const [index, url] of [first, second].entries()) {
				const id = new URL(url).searchParams.get("reviewResult");
				const r = await context.request.get(root + "/results/" + id, {
					params: { source_id: sel.get("source") },
				});
				assert.equal(r.status(), 200);
				const result = await r.json();
				assert.equal(result.id, id);
				assert.equal(result.summary.complaints, index + 1);
				assert.equal(result.summary.eligible, 27);
				receipt.revisions.push({
					id,
					digest: result.digest,
					rows_sha256: result.rows_sha256,
					complaints: result.summary.complaints,
					resolved: result.summary.resolved,
				});
			}
			assert.equal(
				receipt.revisions[0].rows_sha256,
				receipt.revisions[1].rows_sha256,
			);
			assert.notEqual(receipt.revisions[0].digest, receipt.revisions[1].digest);
			await writeFile(
				join(media, "human-review-receipt-" + engine + ".json"),
				JSON.stringify(receipt, null, 2) + "\n",
				{ mode: 0o600 },
			);

			await frozen.screenshot({
				path: join(media, "human-review-result-" + engine + ".png"),
			});
			const recording = await session({
				storageState: owner,
				recordVideo: { dir: media, size: { width: 1200, height: 800 } },
			});
			try {
				stage("record first frozen revision");
				const report = await recording.newPage();
				assert.equal((await report.goto(first)).status(), 200);
				await report
					.getByRole("article", { name: "Проверенная редакция" })
					.waitFor();
				stage("record second frozen revision");
				assert.equal((await report.goto(second)).status(), 200);
				await report
					.getByRole("article", { name: "Проверенная редакция" })
					.waitFor();
				const video = report.video();
				await recording.close();
				await video.saveAs(
					join(media, "human-review-revisions-" + engine + ".webm"),
				);
				await video.delete();
			} finally {
				await recording.close();
			}
		}
		stage("revoke human-only permission then re-open fixed revision");
		await admission("human_revoked");
		try {
			await frozen.waitFor({ state: "detached", timeout: 15000 });
			await page.reload();
			await section.getByRole("alert").waitFor();
			assert.equal(await frozen.count(), 0);
		} finally {
			await admission("ready");
		}
		await page.goto(second);
		await frozen.waitFor();
		return second;
	} finally {
		await context.close();
		await analyst.close();
	}
}
