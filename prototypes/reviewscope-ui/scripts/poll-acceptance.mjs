import assert from "node:assert/strict";
import { readFile, writeFile, rename } from "node:fs/promises";
import { setTimeout as delay } from "node:timers/promises";

export async function checkPoll(page, fixture) {
	const control = fixture.pollControl;
	async function mode(value) {
		await writeFile(control + ".next", value);
		await rename(control + ".next", control);
		for (let i = 0; i < 150; i++) {
			if ((await readFile(control + ".ack", "utf8").catch(() => "")) === value)
				return;
			await delay(60);
		}
		throw new Error("fixture_timeout");
	}
	const last = page.getByRole("link", { name: "Последняя успешная проверка" });
	const retry = page.getByRole("button", { name: "Повторить проверку" });
	const disconnected = page.getByText(/Связь прервана/);
	const old = await page
		.getByRole("link", { name: /^Завершена ·/ })
		.first()
		.getAttribute("href");
	// History may have been opened from a different source. Use the verified snapshot/run only.
	const target = new URL(old, page.url());
	for (const key of ["source", "project", "delivery"])
		target.searchParams.delete(key);
	await page.goto(target.href);
	await last.waitFor();
	await mode("pause");
	let launches = 0;
	const count = (req) => {
		if (
			req.method() === "POST" &&
			new URL(req.url()).pathname.endsWith("/runs")
		)
			launches++;
	};
	page.on("request", count);
	try {
		await retry.click();
		await page.waitForURL(
			(url) => url.searchParams.get("run") !== target.searchParams.get("run"),
		);
		await page.getByRole("button", { name: "Отменить проверку" }).waitFor();
		await last.waitFor();
		const active = page.url();
		// Interrupt one real status read while the synthetic worker still owns a queued run.
		await page.route("**/status", (r) => r.abort("failed"), { times: 1 });
		await disconnected.waitFor();
		assert.equal(await last.count(), 0);
		assert.equal(
			await page
				.getByRole("button", { name: /Запустить проверку|Повторить проверку/ })
				.count(),
			0,
		);
		await last.waitFor();
		await page.route("**/summary-metrics", (r) => r.abort("failed"), {
			times: 1,
		});
		await disconnected.waitFor();
		assert.equal(await last.count(), 0);
		await mode("ready");
		await retry.waitFor();
		await page.getByText(/^Завершена · publish/).waitFor();
		assert.equal(page.url(), active);
		assert.equal(await disconnected.count(), 0);
		assert.equal(
			new URL(await last.getAttribute("href"), active).searchParams.get("run"),
			new URL(active).searchParams.get("run"),
		);
		assert.equal(launches, 1);
		let reads = 0;
		const read = (req) => {
			if (req.url().endsWith("/status")) reads++;
		};
		page.on("request", read);
		await delay(1800);
		page.off("request", read);
		assert.equal(reads, 0);
		// Navigating within the cabinet starts a fresh observation after the terminal run.
		for (const failure of ["ledger", "revoked"]) {
			await mode(failure);
			const status = page.waitForResponse(
				(r) =>
					r.url().endsWith("/status") &&
					r.status() === (failure === "ledger" ? 503 : 404),
			);
			// The session endpoint itself is gated. Keep the active session and use history navigation.
			await page.evaluate(() => {
				history.pushState(
					null,
					"",
					location.href.replace(
						/run=[^&]+/,
						"run=00000000-0000-4000-8000-000000000000",
					),
				);
				dispatchEvent(new PopStateEvent("popstate"));
			});
			await status;
			await page.goBack();
			await (failure === "ledger"
				? disconnected
				: page.getByText("Проверка недоступна в выбранной области.")
			).waitFor();
			assert.equal(await last.count(), 0);
			await mode("ready");
			if (failure === "revoked")
				await page.getByRole("button", { name: "Обновить статус" }).click();
			await last.waitFor();
		}
		const wrong = new URL(active);
		wrong.searchParams.set("snapshot", "00000000-0000-4000-8000-000000000000");
		await page.goto(wrong.href);
		await page.getByText("Проверка недоступна в выбранной области.").waitFor();
		assert.equal(await last.count(), 0);
		let started, release;
		const pending = new Promise((resolve) => {
			started = resolve;
		});
		const hold = new Promise((resolve) => {
			release = resolve;
		});
		await page.route(
			"**/status",
			async (route) => {
				const answer = await route.fetch();
				started();
				await hold;
				await route.fulfill({ response: answer }).catch(() => {});
			},
			{ times: 1 },
		);
		await page.goto(active);
		await pending;
		await page
			.getByLabel("Рабочая область")
			.selectOption(fixture.emptyWorkspace);
		release();
		await page.getByText("Сохранённых заданий пока нет.").waitFor();
		assert.equal(await last.count(), 0);
		await page.goto(active);
		await last.waitFor();
		const guest = await page
			.context()
			.browser()
			.newContext({
				storageState: await page.context().storageState(),
				ignoreHTTPSErrors: true,
			});
		try {
			const tab = await guest.newPage();
			await tab.goto(active);
			await tab
				.getByRole("link", { name: "Последняя успешная проверка" })
				.waitFor();
			await guest.clearCookies();
			await tab.evaluate(() => {
				history.pushState(
					null,
					"",
					location.href.replace(
						/run=[^&]+/,
						"run=00000000-0000-4000-8000-000000000000",
					),
				);
				dispatchEvent(new PopStateEvent("popstate"));
			});
			await tab.getByRole("button", { name: "Войти", exact: true }).waitFor();
			assert.equal(
				await tab
					.getByRole("link", { name: "Последняя успешная проверка" })
					.count(),
				0,
			);
		} finally {
			await guest.close();
		}
	} finally {
		page.off("request", count);
		await mode("ready");
	}
}
