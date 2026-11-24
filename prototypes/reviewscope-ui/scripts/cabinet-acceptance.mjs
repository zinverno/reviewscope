import {
	checkPublicReviewer,
	checkReviewerWidths,
	checkSnapshotReviewer,
} from "./reviewer-context-acceptance.mjs";
import { checkSignals } from "./signals-acceptance.mjs";
import { checkSources } from "./overview-sources-acceptance.mjs";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, X509Certificate } from "node:crypto";
import { readFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium, firefox } from "playwright";
import { checkPoll } from "./poll-acceptance.mjs";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const fixture = JSON.parse(input),
	origin = "https://127.0.0.1:8001";
const media = process.env.RS_TEST_BROWSER_OUT;
if (media) await mkdir(media, { recursive: true, mode: 0o700 });
let stage = "launch";
const observed = [];
const snapshots = new Map();
async function test(type) {
	stage = type.name() + " launch";
	const cert = process.env.NODE_EXTRA_CA_CERTS;
	if (!cert) throw new Error("test certificate required");
	let browser;
	if (type.name() === "firefox") {
		const profile = join(process.env.HOME, "fixture-firefox");
		await mkdir(profile, { mode: 0o700 });
		execFileSync("certutil", [
			"-N",
			"--empty-password",
			"-d",
			"sql:" + profile,
		]);
		execFileSync("certutil", [
			"-A",
			"-n",
			"disposable-cabinet",
			"-t",
			"C,,",
			"-i",
			cert,
			"-d",
			"sql:" + profile,
		]);
		const persistent = await type.launchPersistentContext(profile);
		browser = persistent.browser();
	} else {
		const pin = createHash("sha256")
			.update(
				new X509Certificate(
					await readFile(process.env.RS_FIXTURE_CERT),
				).publicKey.export({ type: "spki", format: "der" }),
			)
			.digest("base64");
		browser = await type.launch({
			args: ["--ignore-certificate-errors-spki-list=" + pin],
		});
	}
	try {
		const context = await browser.newContext({
			locale: "ru-RU",
			reducedMotion: "reduce",
			viewport: { width: 1366, height: 768 },
		});
		await context.route("**/*", (route) =>
			new URL(route.request().url()).origin === origin
				? route.continue()
				: route.abort(),
		);
		let page = await context.newPage();
		page.setDefaultTimeout(10000);
		page.on("response", (r) => {
			const path = new URL(r.url()).pathname;
			observed.push([
				path.endsWith("/login")
					? "auth"
					: path.includes("/assets/")
						? "asset"
						: r.request().resourceType(),
				r.status(),
			]);
		});
		page.on("pageerror", (e) => observed.push(["pageerror", e.name]));
		stage = "login";
		for (let i = 0; i < 25; i++) {
			try {
				await page.goto(origin + "/cabinet/");
				break;
			} catch {
				await new Promise((r) => setTimeout(r, 120));
			}
		}
		stage = "login form";
		await page.getByLabel("Учётная запись").fill(fixture.account);
		await page.getByLabel("Пароль").fill(fixture.password);
		await page.getByRole("button", { name: "Войти" }).press("Enter");
		stage = "permitted source";
		await page.getByLabel("Рабочая область").selectOption(fixture.w);
		await page.getByLabel("Источник", { exact: true }).selectOption(fixture.s);
		const content = await context.storageState();
		// Record only after authentication and never persist browser storage or private headers.
		const review = await browser.newContext({
			storageState: content,
			locale: "ru-RU",
			reducedMotion: "reduce",
			...(media
				? { recordVideo: { dir: media, size: { width: 1200, height: 800 } } }
				: {}),
		});
		await review.route("**/*", (route) =>
			new URL(route.request().url()).origin === origin
				? route.continue()
				: route.abort(),
		);
		const url = page.url();
		await context.close();
		page = await review.newPage();
		await page.goto(url);
		review.setDefaultTimeout(10000);
		review.on("response", (r) =>
			observed.push([
				new URL(r.url()).pathname.endsWith("/preview")
					? "preview"
					: r.request().resourceType(),
				r.status(),
			]),
		);
		async function submit(name, total, accepted, quarantined, duplicate) {
			stage = "preview " + name;
			if (await page.getByRole("link", { name: "Новая поставка" }).count())
				await page.getByRole("link", { name: "Новая поставка" }).click();
			await page.getByLabel("p1", { exact: true }).check();
			await page.getByLabel("p2", { exact: true }).check();
			const json = name === "json",
				dir = json ? fixture.json : fixture.csv;
			await page
				.getByLabel("Файл", { exact: true })
				.setInputFiles(join(dir, json ? "data" : name + ".csv"));
			await page
				.getByLabel("Формат", { exact: true })
				.selectOption(json ? "json" : "csv");
			const details = page.getByText("Сопоставление и паспорт", {
				exact: true,
			});
			if (
				!(await page.getByLabel("Mapping (JSON)", { exact: true }).isVisible())
			)
				await details.click();
			stage = "mapping";
			await page
				.getByLabel("Mapping (JSON)", { exact: true })
				.fill(
					await readFile(join(dir, json ? "mapping" : "mapping.json"), "utf8"),
				);
			stage = "passport";
			await page
				.getByLabel("Паспорт (JSON)", { exact: true })
				.fill(
					await readFile(
						join(dir, json ? "passport" : name + ".passport.json"),
						"utf8",
					),
				);
			stage = "preview button";
			await page
				.getByRole("button", { name: "Предпросмотр", exact: true })
				.click();
			stage = "preview response " + name;
			await page
				.getByRole("heading", { name: "Диагностика поставки" })
				.waitFor();
			const counts = await page.locator("dl dd").allTextContents();
			assert.deepEqual(
				counts,
				[total, accepted, quarantined, duplicate].map(String),
			);
			stage = "apply " + name;
			await page
				.getByRole("button", { name: "Подтвердить и создать снимок" })
				.click();
			await page.getByRole("link", { name: "Открыть снимок" }).waitFor();
			const target = await page
				.getByRole("link", { name: "Открыть снимок" })
				.getAttribute("href");
			snapshots.set(name, new URL(target, origin).searchParams.get("snapshot"));
		}
		if (type === chromium) {
			await submit("a", 3, 3, 0, 0);
			stage = "cancel and return";
			await page.getByRole("button", { name: "Запустить проверку" }).click();
			await page.getByRole("button", { name: "Отменить проверку" }).waitFor();
			const saved = page.url();
			await page.close();
			page = await review.newPage();
			await page.goto(saved);
			await page.getByRole("button", { name: "Отменить проверку" }).click();
			await page.getByRole("button", { name: "Повторить проверку" }).click();
			await page.waitForURL(
				(url) =>
					url.searchParams.get("run") !==
					new URL(saved).searchParams.get("run"),
			);
			const active = page.url();
			await page.close();
			page = await review.newPage();
			await page.goto(active);
			await page
				.getByRole("link", { name: "Последняя успешная проверка" })
				.waitFor({ timeout: 25000 });
			await page.getByText(/С пригодным текстом 3 из 3/).waitFor();
			await submit("b", 6, 6, 0, 0);
			await submit("json", 12, 6, 5, 1);
			for (const name of ["b", "json"]) {
				const u = new URL("/cabinet/", origin);
				u.search = new URLSearchParams({
					workspace: fixture.w,
					source: fixture.s,
					snapshot: snapshots.get(name),
				});
				await page.goto(u.href);
				await page.getByRole("button", { name: "Запустить проверку" }).click();
				await page
					.getByRole("link", { name: "Последняя успешная проверка" })
					.waitFor({ timeout: 25000 });
			}
		} else {
			stage = "saved delivery";
			await page.getByText(/Сохранённые поставки/).click();
			await page
				.getByRole("link", { name: /Снимок ·/ })
				.first()
				.click();
			await page
				.getByRole("heading", { name: "Диагностика поставки" })
				.waitFor();
		}
		stage = type.name() + " polling recovery";
		const polling = await review.newPage();
		await polling.goto(origin + "/cabinet/?workspace=" + fixture.w);
		await checkPoll(polling, fixture);
		await polling.close();
		if (media)
			await polling
				.video()
				.saveAs(join(media, "poll-recovery-" + type.name() + ".webm"));
		stage = type.name() + " responsive";
		for (const width of [1920, 1366, 390, 320]) {
			stage = type.name() + " viewport " + width;
			await page.setViewportSize({
				width,
				height: width === 1920 ? 1000 : 768,
			});
			assert.ok(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth,
				),
			);
			await page.keyboard.press("Tab");
			stage = type.name() + " screenshot " + width;
			if (media)
				await page.screenshot({
					path: join(media, type.name() + "-" + width + ".png"),
					fullPage: true,
				});
		}
		const evidence = async (snapshot, position) => {
			const dest = new URL("/cabinet/", origin);
			for (const [name, value] of Object.entries({
				view: "reviews",
				workspace: fixture.w,
				source: fixture.s,
				snapshot,
			}))
				dest.searchParams.set(name, value);
			if (position) dest.searchParams.set("position", String(position));
			await page.goto(dest.href);
			await page
				.getByRole("heading", { name: "Отзывы и доказательства" })
				.waitFor();
		};
		stage = type.name() + " historical review";
		for (const [delivery, version, beginning] of [
			["a", 1, "На табло"],
			["b", 2, "Уточню"],
		]) {
			await evidence(snapshots.get(delivery), 1);
			await page
				.getByRole("link", { name: `Позиция 1 · r1 · версия ${version}` })
				.click();
			await page.locator("blockquote").waitFor();
			assert.ok(
				(await page.locator("blockquote").innerText()).startsWith(beginning),
			);
			if (delivery === "a")
				await page.getByText(/Исторический снимок/).waitFor();
			await page.goBack();
			assert.equal(new URL(page.url()).searchParams.get("position"), "1");
		}
		await evidence(snapshots.get("b"), 5);
		await page
			.getByRole("link", { name: /Позиция 5 · без внешнего ID/ })
			.click();
		await page.getByText("Точный фрагмент не размечен.").waitFor();
		await evidence(snapshots.get("json"), 1);
		await page
			.getByText("Подтверждённых отзывов по выбранным условиям нет.")
			.waitFor();
		stage = type.name() + " long review";
		await evidence(fixture.longSnapshot);
		await page.getByRole("link", { name: "Следующая страница" }).click();
		await page.getByRole("link", { name: /Позиция 26 ·/ }).waitFor();
		await page.goBack();
		await page.getByRole("link", { name: /Позиция 1 · long/ }).click();
		await page.locator("blockquote").waitFor();
		assert.ok(
			(await page.locator("blockquote").innerText()).includes(
				"<b>Синтетический текст</b>",
			),
		);
		assert.equal(await page.locator("blockquote b").count(), 0);
		assert.equal(
			await page
				.getByRole("link", { name: "Открыть проверенную ссылку источника" })
				.getAttribute("href"),
			"https://example.test/review/long",
		);
		for (const width of [1920, 1366, 390, 320]) {
			await page.setViewportSize({
				width,
				height: width === 1920 ? 1000 : 768,
			});
			await page
				.getByRole("heading", { name: "Полный текст" })
				.scrollIntoViewIfNeeded();
			assert.ok(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth,
				),
			);
			if (media)
				await page.screenshot({
					path: join(media, "evidence-" + type.name() + "-" + width + ".png"),
				});
		}
		stage = type.name() + " topic and term sources";
		const sourcesTab = await review.newPage();
		await checkSources(sourcesTab, origin);
		await sourcesTab.close();
		if (media)
			await sourcesTab
				.video()
				.saveAs(join(media, "overview-sources-" + type.name() + ".webm"));
		stage = type.name() + " scoped overview";
		for (const [name, n, den] of [
			["a", 3, 3],
			["b", 6, 6],
			["json", 6, 12],
		]) {
			const u = new URL("/cabinet/", origin);
			u.search = new URLSearchParams({
				view: "overview",
				workspace: fixture.w,
				source: fixture.s,
				snapshot: snapshots.get(name),
			});
			await page.goto(u.href);
			await page
				.getByText(n + " принятых отзывов из " + den + " позиций поставки")
				.waitFor();
			await page.getByText(/Покрытие неизвестно/).waitFor();
			await page.getByText(/ANALYSIS_NOT_AVAILABLE/).waitFor();
		}
		await page.getByText("Причины исключений и предупреждения").click();
		await page
			.getByRole("rowheader", { name: "Конфликт идентификаторов" })
			.waitFor();
		await page.getByRole("link", { name: "Только с текстом" }).click();
		await page.getByText(/FILTER_NOT_COMPUTED/).waitFor();
		assert.equal(
			await page.getByRole("heading", { name: "Позиции поставки" }).count(),
			0,
		);
		await page
			.getByRole("link", { name: "Открыть отзывы этой выборки" })
			.click();
		await page.getByText(/Отзывов в выборке:/).waitFor();
		await page.goBack();
		await page.getByRole("link", { name: "Сбросить фильтры" }).click();
		await page.getByText("6 принятых отзывов из 12 позиций поставки").waitFor();
		for (const width of [1920, 1366, 390, 320]) {
			await page.setViewportSize({ width, height: 768 });
			assert.ok(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth,
				),
			);
			if (media)
				await page.screenshot({
					path: join(media, "overview-" + type.name() + "-" + width + ".png"),
				});
		}
		await page
			.getByRole("link", {
				name: "Открыть отдельный обзор публичной синтетики",
			})
			.click();
		await page.getByText(/204 отзывов/).waitFor();
		await page.getByLabel("Точка демо").selectOption("p2");
		await page
			.getByRole("heading", { name: "Discover · все точки демо" })
			.waitFor();
		assert.equal(new URL(page.url()).searchParams.get("snapshot"), null);
		await page.goBack();
		await page.getByText(/204 отзывов/).waitFor();
		await page.getByLabel("Ранжировать по").selectOption("five");
		await page.getByRole("columnheader", { name: "Доля 5 ★" }).waitFor();
		await page
			.getByText("Тональность по оценке и ключевые слова по звёздам")
			.click();
		await page.getByText(/Положительная группа/).waitFor();
		if (media)
			await page.screenshot({
				path: join(media, "discover-" + type.name() + "-320.png"),
			});
		stage = type.name() + " canonical graph scope";
		const cabinet = (params) =>
			origin + "/cabinet/?" + new URLSearchParams(params);
		const chosen = {
			view: "graph",
			workspace: fixture.w,
			snapshot: snapshots.get("a"),
			source: fixture.s,
		};
		await page.goto(cabinet(chosen));
		await page.getByText(/CANONICAL_GRAPH_NOT_AVAILABLE/).waitFor();
		assert.equal(await page.locator(".direct-map").count(), 0);
		const foreignPath =
			"/api/v1/workspaces/" +
			fixture.otherWorkspace +
			"/snapshots/" +
			snapshots.get("a");
		const foreign = await review.request.get(origin + foreignPath);
		const unknown = await review.request.get(
			origin + foreignPath.replace(fixture.otherWorkspace, crypto.randomUUID()),
		);
		assert.equal(foreign.status(), 404);
		assert.deepEqual(await foreign.json(), await unknown.json());
		await page.goto(cabinet({ ...chosen, run: crypto.randomUUID() }));
		await page.getByText("Снимок или запуск недоступны.").waitFor();
		await page.goto(cabinet(chosen));
		await page
			.getByRole("link", { name: "Исследовать повторы публичной синтетики" })
			.click();
		const graphBase = page.url();
		const p1 = JSON.parse(await readFile("src/demo-graph/p1.json", "utf8"));
		const resolveGroup = (id) => p1.groups.find((g) => g.id === id);
		const getPair = async (family, left, right) => {
			const url = new URL(graphBase);
			for (const [key, value] of Object.entries({ family, a: left, b: right }))
				url.searchParams.set(key, String(value));
			await page.goto(url.href);
			await page
				.getByRole("heading", { name: "Отзыв " + left, exact: true })
				.waitFor();
			assert.deepEqual(
				await page
					.locator(".duplicate-investigation blockquote")
					.allTextContents(),
				[p1.reviews[left].text, p1.reviews[right].text],
			);
		};
		stage = type.name() + " graph evidence";
		for (const [kind, g, l, r] of [
			["exact", 9, "rbp032", "rbp038"],
			["fuzzy", 4, "rbp008", "rbp007"],
			["near", 4, "rbp001", "rbp017"],
			["semantic", 4, "rbp000", "rbp001"],
		]) {
			await getPair(g, l, r);
			await page
				.getByRole("status")
				.filter({ hasText: "Прямая связь: " + kind })
				.waitFor();
		}
		for (const [l, r, direct] of [
			["rbp011", "rbp013", true],
			["rbp013", "rbp018", true],
			["rbp011", "rbp018", false],
		]) {
			await getPair(4, l, r);
			await page
				.getByRole("status")
				.filter({
					hasText: direct
						? "Прямая связь:"
						: "Прямой связи между выбранными отзывами в результате нет.",
				})
				.waitFor();
		}
		const long = p1.groups.find((g) => g.members.includes("r00065"));
		const peer = long.members.find((id) => id !== "r00065");
		await getPair(long.id, "r00065", peer);
		assert.ok(
			(await page.locator("blockquote").first().innerText()).length >= 350,
		);
		await page.goto(graphBase);
		const table = page.getByRole("table", { name: "Прямые пары" });
		await table.waitFor();
		assert.equal(await page.locator(".direct-map svg").count(), 0);
		assert.equal(await table.locator("tbody tr").count(), 20);
		await page.getByLabel("Показать карту этой страницы").check();
		assert.equal(await page.locator(".direct-map line").count(), 10);
		const actualEdges = resolveGroup(4).edges;
		const displayed = await page
			.locator(".direct-map svg a")
			.evaluateAll((links) =>
				links.map((a) => {
					const u = new URL(a.getAttribute("href"), location.href);
					return [u.searchParams.get("a"), u.searchParams.get("b")];
				}),
			);
		assert.deepEqual(
			displayed,
			actualEdges.slice(0, 10).map((e) => e.slice(0, 2)),
		);
		for (const width of [1920, 1366, 390, 320]) {
			await page.setViewportSize({ width, height: 768 });
			await page.locator(".direct-map").scrollIntoViewIfNeeded();
			assert.ok(
				await page.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth,
				),
			);
			if (media)
				await page.screenshot({
					path: join(media, "graph-" + type.name() + "-" + width + ".png"),
				});
		}
		await page.getByRole("link", { name: "Следующие пары" }).click();
		await page.getByRole("link", { name: "Предыдущие пары" }).waitFor();
		assert.equal(
			await table.locator("tbody tr").first().locator("a").innerText(),
			actualEdges[20].slice(0, 2).join(" · "),
		);
		await page.goBack();
		await table.waitFor();
		await page.getByLabel("Тип прямой связи").selectOption("near");
		await page.waitForURL((u) => u.searchParams.get("kind") === "near");
		await table.waitFor();
		const kinds = await table
			.locator("tbody tr td:nth-child(2)")
			.allTextContents();
		assert.ok(kinds.length > 0 && kinds.every((k) => k === "near"));
		await page.getByLabel("Точка графа").selectOption("p2");
		await page
			.getByText("Детектор не выделил семейств в этой точке.")
			.waitFor();
		const wrong = new URL(graphBase);
		wrong.searchParams.set("result", "0".repeat(64));
		await page.goto(wrong.href);
		await page
			.getByText("Для выбранного снимка и результата граф недоступен.")
			.waitFor();
		const mixed = new URL(graphBase);
		mixed.searchParams.set("snapshot", snapshots.get("a"));
		await page.goto(mixed.href);
		await page
			.getByText("Для выбранного снимка и результата граф недоступен.")
			.waitFor();
		stage = type.name() + " public reviewer navigation";
		const reviewerTab = await review.newPage();
		const reviewerProfile = await checkPublicReviewer(
			reviewerTab,
			origin,
			media,
		);
		await reviewerTab.close();
		if (media)
			await reviewerTab
				.video()
				.saveAs(join(media, "reviewer-context-" + type.name() + ".webm"));
		stage = type.name() + " reviewer widths";
		await checkReviewerWidths(browser, origin, reviewerProfile, content);
		stage = type.name() + " signal analysis";
		// Capture only the packaged public explanation. Snapshot results use an unrecorded context.
		await review.close();
		const signalsContext = await browser.newContext({
			storageState: content,
			locale: "ru-RU",
			reducedMotion: "reduce",
		});
		await signalsContext.route("**/*", (route) =>
			new URL(route.request().url()).origin === origin
				? route.continue()
				: route.abort(),
		);
		signalsContext.setDefaultTimeout(10000);
		const signalsPage = await signalsContext.newPage();
		await checkSignals(
			signalsPage,
			origin,
			fixture,
			snapshots,
			(phase) => {
				stage = type.name() + " signals " + phase;
			},
			media,
			content,
		);
		stage = type.name() + " scoped reviewer navigation";
		await checkSnapshotReviewer(signalsPage, origin, fixture, snapshots);
		stage = type.name() + " exact candidate";
		const candidatePage = await signalsContext.newPage();
		await candidatePage.goto(
			origin +
				"/cabinet/?" +
				new URLSearchParams({
					workspace: fixture.w,
					source: fixture.s,
					snapshot: snapshots.get("a"),
					view: "reviews",
				}),
		);
		await candidatePage
			.getByRole("link", { name: "Поиск кандидатов по снимку" })
			.click();
		await candidatePage
			.getByLabel("Формулировка или пример")
			.fill("Ждать не пришлось");
		await candidatePage
			.getByRole("button", { name: "Найти кандидатов" })
			.press("Enter");
		await candidatePage
			.getByRole("link", { name: "Фрагмент 1 · отзыв 1", exact: true })
			.click({ timeout: 30000 });
		await candidatePage
			.getByRole("table", { name: "Ранжированные кандидаты" })
			.getByRole("columnheader", { name: "Версия и границы" })
			.waitFor();
		await candidatePage.locator("mark").waitFor();
		assert.equal(
			await candidatePage.locator("mark").textContent(),
			"Ждать не пришлось.",
		);
		await candidatePage.reload();
		await candidatePage.locator("mark").waitFor();
		for (const width of [320, 390, 1366, 1920]) {
			await candidatePage.setViewportSize({ width, height: 768 });
			assert(
				await candidatePage.evaluate(
					() => document.documentElement.scrollWidth <= innerWidth + 1,
				),
			);
		}
		assert(!decodeURIComponent(candidatePage.url()).includes("пришлось"));
		await candidatePage
			.getByText("Полный текст версии", { exact: true })
			.click();
		await candidatePage
			.getByRole("link", { name: "Вернуться к списку кандидатов" })
			.click();
		await candidatePage
			.getByRole("link", { name: "Фрагмент 1 · отзыв 1", exact: true })
			.waitFor();
		const originalLink = await candidatePage
			.getByRole("link", { name: "Фрагмент 1 · отзыв 1", exact: true })
			.getAttribute("href");
		const originalDetail = new URL(originalLink, origin);
		if (media)
			await candidatePage
				.getByRole("table", { name: "Ранжированные кандидаты" })
				.screenshot({
					path: join(media, "candidates-" + type.name() + ".png"),
				});
		stage = type.name() + " invalid fragment";
		const invalidFragment = new URL(originalDetail);
		invalidFragment.searchParams.set("fragment", "0".repeat(64));
		await candidatePage.goto(invalidFragment.href);
		await candidatePage.getByRole("alert").first().waitFor();
		assert.equal(await candidatePage.locator("mark").count(), 0);
		await candidatePage.goto(originalDetail.href);
		await candidatePage.locator("mark").waitFor();
		stage = type.name() + " empty lexical result";
		await candidatePage.getByLabel("Формулировка или пример").fill("鯨鯨鯨");
		await candidatePage
			.getByRole("button", { name: "Найти кандидатов" })
			.click();
		await candidatePage
			.getByText(/В выдаче 0 разных отзывов и 0 фрагментов/)
			.waitFor({ timeout: 30000 });
		stage = type.name() + " missing offline model";
		await candidatePage
			.getByLabel("Формулировка или пример")
			.fill("Готовый заказ");
		await candidatePage.getByLabel("Метод поиска").selectOption("dense");
		await candidatePage
			.getByRole("button", { name: "Найти кандидатов" })
			.click();
		await candidatePage
			.getByText(/NOT_RUN_MISSING_(WEIGHTS|BACKEND)/)
			.waitFor({ timeout: 30000 });
		stage = type.name() + " session revocation";
		await candidatePage.goto(originalDetail.href);
		await candidatePage.locator("mark").waitFor();
		const logoutTab = await signalsContext.newPage();
		await logoutTab.goto(origin + "/cabinet/");
		await logoutTab.getByRole("button", { name: "Выйти" }).click();
		await logoutTab.getByRole("button", { name: "Войти" }).waitFor();
		await candidatePage.bringToFront();
		await candidatePage
			.locator("mark")
			.waitFor({ state: "detached", timeout: 15000 });
		await candidatePage.reload();
		await candidatePage.getByRole("button", { name: "Войти" }).waitFor();
		assert.equal(await candidatePage.locator("mark").count(), 0);
		await signalsContext.close();
	} finally {
		await browser.close();
	}
}
try {
	await test(chromium);
	await test(firefox);
} catch (error) {
	// Playwright call logs can include form values and absolute upload paths.
	const category = /permission denied|EACCES/i.test(error.message)
		? "permission"
		: /executable doesn't exist|executable not found/i.test(error.message)
			? "missing executable"
			: /sandbox|user namespace|EPERM/i.test(error.message)
				? "sandbox"
				: /target.*closed|process.*exit/i.test(error.message)
					? "process closed"
					: error.name;
	console.error(
		"cabinet acceptance failed at " +
			stage +
			" (" +
			category +
			") " +
			JSON.stringify(observed.slice(-20)),
	);
	process.exitCode = 1;
}
