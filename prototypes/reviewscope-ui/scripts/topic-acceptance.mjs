import assert from "node:assert/strict";
import { join } from "node:path";

export async function checkTopics(page, start, historical, browser, media, mark) {
 await page.goto(start);
 await page.getByRole("button",{name:"Исследовать все пригодные тексты"}).click();
 await page.getByRole("link",{name:"крышка стакана",exact:true}).click();
 const initial = page.url();
 await page.getByRole("heading",{name:"Репрезентативные версии"}).waitFor();
 await page.getByRole("link",{name:/^[0-9a-f-]{36}$/}).first().click();
 assert.match(await page.locator("blockquote").innerText(),/крышка стакана/i);
 mark("historical evidence");
 await page.getByLabel("UUID исторического снимка этого источника").fill(historical);
 await page.getByRole("link",{name:"Открыть выбранную историю"}).click();
 await page.getByRole("button",{name:"Искать аналогии в истории"}).click();
 await page.getByRole("link",{name:/Историческая версия/}).first().click();
 assert.match(await page.locator("blockquote").innerText(),/бумажная крышка стакана/i);
 await page.getByRole("button",{name:"Подтвердить аналогию"}).first().click();
 await page.getByText("Проблема наблюдалась в выбранной истории",{exact:true}).waitFor();
 if(media)await page.screenshot({path:join(media,"topic-history-"+browser+".png")});
 mark("merge and split");
 await page.goto(initial);
 await page.getByRole("link",{name:"Все кандидаты результата"}).click();
 const list=page.url();
 await page.getByLabel("Объединить крышка стакана").check();
 await page.getByLabel("Объединить звонок двери").check();
 await page.getByLabel("Причина объединения").fill("Проверить границы двух групп");
 await page.getByRole("button",{name:"Объединить выбранные кандидаты"}).click();
 await page.getByRole("link",{name:"Производный кандидат",exact:true}).click();
 const union=page.url();
 await page.getByText("Все версии (6)",{exact:true}).click();
 await page.getByRole("checkbox",{name:/В первую часть/}).first().check();
 await page.getByLabel("Причина решения").fill("Отделить выбранную версию");
 await page.getByRole("button",{name:"Разделить отмеченные и остальные версии"}).click();
 const children=page.getByRole("link",{name:/Производная редакция/});
 await children.first().waitFor();
 const next = await children.nth(1).getAttribute("href");
 await children.first().click();
 await page.getByLabel("Причина решения").fill("После просмотра оставляем открытым вопрос о теме");
 await page.getByRole("button",{name:"Отклонить кандидата"}).click();
 await page.getByText("Отклонён",{exact:true}).waitFor();
 mark("confirm definition");
 await page.goto(new URL(next,union).href);
 await page.getByLabel("Причина решения").fill("Область описана по просмотренным версиям");
 await page.getByText("Создать определение и подтвердить",{exact:true}).click();
 for(const [label,value] of Object.entries({"Название":"Разлив из стакана","Границы проблемы":"Протечка закрытого стакана","Включающий пример":"Напиток пролился через крышку","Исключающий пример":"Брызги из открытой бутылки"})){
  await page.getByRole("textbox",{name:label,exact:true}).fill(value);
 }
 await page.getByRole("button",{name:"Сохранить новое определение"}).click();
 await page.getByRole("link",{name:"Подтверждённая редакция определения"}).waitFor();
 await page.goto(initial);
 await page.getByRole("link",{name:/Производная редакция/}).waitFor();
 assert.equal(await page.getByRole("heading",{name:"Репрезентативные версии"}).count(),1);
 if(media)await page.screenshot({path:join(media,"topic-lineage-"+browser+".png")});
 await page.goto(list);
}
