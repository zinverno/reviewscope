import assert from "node:assert/strict";
import { readFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium, firefox } from "playwright";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const fixture = JSON.parse(input),
	origin = "https://127.0.0.1:8001";
const media = process.env.RS_TEST_BROWSER_OUT;
if (media) await mkdir(media, { recursive: true, mode: 0o700 });
let stage = "launch";
const observed = [];
async function test(type) {
	const browser = await type.launch();
	try {
		const context = await browser.newContext({
			ignoreHTTPSErrors: true,
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
		await page.getByLabel("Источник", { exact: true }).selectOption(fixture.s);
		const content = await context.storageState();
		// Record only after authentication and never persist browser storage or private headers.
		const review = await browser.newContext({
			storageState: content,
			ignoreHTTPSErrors: true,
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
		stage = "responsive";
		for (const width of [1920, 1366, 390, 320]) {
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
			if (media)
				await page.screenshot({
					path: join(media, type.name() + "-" + width + ".png"),
					fullPage: true,
				});
		}
		await review.close();
	} finally {
		await browser.close();
	}
}
try {
	await test(chromium);
	await test(firefox);
} catch (error) {
	// Playwright call logs can include form values and absolute upload paths.
	console.error(
		"cabinet acceptance failed at " +
			stage +
			" (" +
			error.name +
			") " +
			JSON.stringify(observed.slice(-20)),
	);
	process.exitCode = 1;
}
