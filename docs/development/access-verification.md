# PR-08 · Матрица проверки

**NEEDS_AUTHZ_REVIEW**, 23 ноября 2026. Владелец zinverno слил PR-07 #9 в main. База `5b2033a05d2bdbb527a74863b71b25e4d56aec8a`, дерево `d85e5ca1254447fd6501b9640a27e2befd3e8738` совпало с деревом проверенного `f9cf14f9289f24ec74dfbca4f720b5ddbc58e0e3`.

Исходный SHA реализации и операционных тестов — `73e0194903c8edf31c17bbd71aa5afb7fb3d056f`. Локально PostgreSQL 18.6 x86_64, Python 3.14.7, psycopg 3.3.9, SQLAlchemy 2.1.9, Alembic 1.20.1, argon2-cffi 25.1.0, FastAPI 0.165.0. Кластер использует приватный Unix-сокет. Миграции выполнял rs_migration, приложение и отрицательные SQL-проверки подключались как отдельный LOGIN rs_runtime. Все данные синтетические. Матрица кодов по ролям находится в [контракте доступа](access-contract.md).

| Проверка | Наблюдение | Результат |
| --- | --- | --- |
| Два аккаунта и два workspace | В обоих source_key=same, внешний отзыв r1 и локальная точка p1. Свои source/snapshot/run видны списком и по ID, чужой и случайный UUID дали одинаковое тело 404 в обоих направлениях. Страница за концом пуста. | PASS |
| Изменение роли | Один principal одновременно owner в B и viewer/analyst в A. PATCH в A для обеих младших ролей дал 403. Owner A смог изменить членство, отзыв дал 404 на следующем GET в A. БД сохранила субъекта, актёра, прежнюю роль и новое состояние. | PASS |
| Сессии и мутации | Неверный пароль и неизвестная учётная запись дали одинаковый 401. Повреждение cookie, отзыв, истечение и деактивация закрыли доступ. Деактивированная и затем включённая учётная запись потребовала нового входа. Мутации без разрешённого Origin и CSRF-заголовка дали 403. | PASS |
| Ограничитель | Пять попыток в окне разрешены обработчику, шестая до границы в 60 секунд дала 429, после границы прошёл вход. Для проверки точной границы тест управлял монотонными часами. | PASS |
| SQL без привилегий | Прямой SELECT чужого UUID источника, снимка и запуска вернул 0 строк, в том числе после смены рабочего контекста. Доступ к тексту, верификатору, digest сессии, массиву версий, DDL и записи запрещён. Установка UUID без действующей сессии дала 0 строк. | PASS |
| Пул и отказ | Два одновременных пользователя работали на двух разных backend PID. После SQL-ошибки оба PID переиспользованы с пустым контекстом, в том числе backend откатившейся транзакции — другим пользователем. | PASS |
| Неготовность | Пустая БД, неверная ревизия, повреждение используемого столбца, выключенная RLS, недоступный сокет и отсутствие файла ключа дали 503 при попытке чтения с cookie. | PASS |
| Миграция и восстановление | При неизвестном прежнем principal upgrade остановился. После проверенного сопоставления повторный upgrade сохранил строки и исходные UUID. Архив восстановлен с ACL в новую БД, прежняя сессия отозвана, новый вход видел только свой workspace. Backfill PR-07 сверил 1058 версий, 12 точек, manifest 8cacefacc9feb493c7482b491728c6a315680110d7adddafbb2af3a88ea1ce5a и байты объектов. | PASS |
| Оператор и TLS | CLI прошла обычный разбор аргументов и операторский сценарий с подставленным интерактивным вводом. Отдельный процесс штатного сервера принял реальный HTTPS-вход по временному доверенному сертификату на loopback. Запрос без cookie дал 401, с выданной Secure cookie — свой workspace. Журнал просмотрен на присутствие тестового пароля, логина, UUID и токена. | PASS |
| Офлайновый HTTP | Отсутствие permissive CORS, запрет лишних полей, ограничение тела, 401/403/404, серверный request ID, no-store, редактированные логи. Smoke запрещает импорт torch, transformers и sentence_transformers. | PASS |
| Регрессии Python | В ходе реализации весь Python-набор прошёл локально с кэшированным MiniLM и отключённой сетью моделей — 620 passed. Сюда входят Streamlit, прежний API, embeddings и retrieval benchmark. Отдельный операционный прогон финального кода — 16 passed. | PASS |
| React | Дерево прототипа 7ee273e5842b013d447ee3153180b45d05c26a2f совпадает с main. Node 24.14.1, lint, typecheck, 2 unit-теста, build, 11 браузерных тестов Chromium и 11 Firefox, включая reduced motion. | PASS |

Итоговый SHA ветки и ссылки на GitHub Actions приведены в описании Draft PR. Проверка не включает публичное размещение, независимый аудит, реальные отзывы или клиентский пилот. **BLOCKED_REAL_SOURCE**, **BLOCKED_CLIENT_PILOT**.

## Воспроизведение

В новом checkout с Python 3.12+, инструментами PostgreSQL, openssl и свободным локальным портом 8001:

```bash
python -m pip install -r requirements.txt
python -m pip install -e '.[operational,api,access,dev]'
python scripts/build_public_demo_dataset.py --verify
ruff check .
git diff --check
HF_HUB_OFFLINE=1 python -m pytest tests/test_api.py tests/test_access.py
bash operational/test-postgres.sh -s
```

Для полного прогона нужны зависимости embeddings и доступные веса MiniLM. В отдельном тестовом checkout подготовьте сгенерированные фикстуры и локальную копию синтетической базы:

```bash
python -m pip install -e '.[embeddings]'
python scripts/generate_demo_data.py
cp -n demo/reviewscope_demo.duckdb data/reviewscope.duckdb
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest -o addopts= -q -rs
cd prototypes/reviewscope-ui
npm ci
npm run lint && npm test && npm run build
npx playwright install chromium firefox
npm run test:browser && BROWSER=firefox npm run test:browser
```

В локальной sandbox первый запуск PostgreSQL не смог открыть Unix-сокет, браузерный запуск не смог открыть loopback, а два pytest с ASGI зависли. Их остановили. Результаты PASS относятся к повторным завершившимся прогонам с локальными сокетами. Известные предупреждения локального набора — совместимость Starlette TestClient с httpx и переименование метода sentence-transformers.
