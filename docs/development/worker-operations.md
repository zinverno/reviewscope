# Операторская процедура PR-11

## Подготовка и миграция

Проверенная исходная точка — main `9747cbdb71e66c1bbcbe49101ac13bdf5fe68ea0`. Родитель squash — `81e9bd53edd2de040da0d860b89efd6926ea04a1`. Принятый head PR-10 `ca6371ad7c4f5319e5c35e0fc1fad11e6dbf7067` и squash имеют tree `bf67e66418b4ca311efaa021cac7d62347813530`. Для разработки создан пустой от пользовательских изменений worktree `feat/pr11-background-jobs`. Локальная проверка использует Python 3.14.7 и PostgreSQL 18.6, CI использует Python 3.12 и PostgreSQL 16.

1. Остановите writers. Сделайте и восстановите независимую предмиграционную копию PostgreSQL и каждого приватного object root по процедуре PR-10. Сравните UUID снимков и артефактов, manifest, размер, SHA и членства. Исходные архивы остаются у оператора. [Восстановление поставок](snapshot-operations.md)
2. DBA создаёт отдельный login в доверенном кластере. Миграционный оператор и HTTP-процесс сохраняют прежние роли. В тестовом кластере trust действует только на его приватном Unix-сокете. Для рабочей установки требуется локальная аутентификация и разделение ОС-доступа по правилам PR-08. HTTP-процессу закрывают файловую систему архива и секреты worker. Worker использует ОС-аккаунт владельца приватных объектов PR-10, доверенный DBA и отдельную SQL-роль.

```sql
CREATE ROLE rs_worker LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
```

3. Из checkout в окружении с extras operational и access установите libpq-параметры мигратора. Маршрут Alembic проходит `0001_operational → 0002_principals → 0003_workspace_access → 0004_delivery_history → 0005_background_jobs`. Для старой базы действуют остановки при неподтверждённом membership и неоднозначной точке. Перед запуском проверьте каждое такое соответствие. 0005 расширяет analysis_run, сохраняет UUID, время, версии и ссылки на результат. Старые runs с пустым job_kind продолжают прежние разрешённые переходы. Новая ручная запись доверенного оператора прежнего формата требует явного legacy_manual_v1. Worker забирает только заявки с request_key. При неудачном upgrade DDL и штамп откатываются вместе.
4. Выполните команды под миграционной ролью. Возврат версии приложения с прежней схемой выполняют восстановлением пары «БД + файлы» в независимый контур. Зафиксируйте, какие операции после резервной копии потребуют отдельной сверки.

```bash
python -m alembic -c operational/alembic.ini upgrade head
python -m alembic -c operational/alembic.ini current
```

## Роли и запуск

`rs_runtime` использует проверенные enqueue_job и cancel_job, читаемые поля статуса и политику RLS опубликованного артефакта. У роли отсутствует табличная запись. `rs_worker` получает только USAGE схемы, чтение штампа и EXECUTE четырёх операций claim_job, worker_step, job_input, job_versions. Чтение входа требует совпадения workspace, run, действующего случайного nonce и активного заявителя. Роль worker не получает таблицы отзывов, учётные данные пользователя или права администратора. Все definer-функции используют фиксированный pg_catalog,public,pg_temp, закрытый для CREATE runtime/worker public и явные GRANT. Непосредственные помощники закрыты для PUBLIC. Дополнительные роли и пользовательские GRANT проверяются при развёртывании. База и учётные записи предназначены только для этого сервиса.

Из корня checkout выберите доверенный Unix-сокет PostgreSQL и базу, уберите PGSERVICE и PGHOSTADDR. Настройте закрытый passfile при необходимости. Worker сам указывает rs_worker. Команды запускаются раздельно. Для TLS сервиса 8001 нужны существующие настройки сертификата и ключа PR-08.

```bash
python -m reviewscope.access
python -m operational.worker
```

Начните с одного worker, допустимы два. Запуск `python -m operational.worker --once` делает один poll. В daemon-режиме пустая очередь проверяется раз в секунду. Планировщик мониторинга и загрузка моделей этому процессу не требуются. Порог SQL statement_timeout — 10 с. Перезапуск после завершения с worker_unavailable выполняет оператор/супервизор. Логи и PostgreSQL-параметры должны исключать SQL и пользовательские значения. [Конфигурация доступа](access-operations.md)

**После SIGKILL или разрыва соединения** проверьте состояние БД и доступность того же object root, запустите polling-процесс. Дождитесь истечения lease, задержки повторной попытки и terminal-статуса. Тот же UUID заявки позволяет дождаться результата через HTTP. После третьего отказа исследуйте основание приватно. Новую заявку после исправления причины создают с новым ключом. SIGTERM проверяется между порциями. Закрытие браузера не посылает отмену.

```sql
SELECT status, stage, reason, attempt, count(*)
FROM analysis_run WHERE request_key IS NOT NULL GROUP BY 1,2,3,4 ORDER BY 1,2,3,4;
```

Полный пример запроса, состояний и числового результата получен по TLS на 127.0.0.1:8001 на синтетике A. Чтобы воспроизвести проверку через HTTP, используйте операторски созданную сессию PR-08, точный Origin и X-RS-CSRF: 1, отправьте объект request на POST /api/v1/workspaces/{workspace}/runs. После 202 завершите клиент, выполните worker, затем GET /api/v1/workspaces/{workspace}/runs/{run_id}/status. При повторном POST ожидается 200 с теми же UUID и SHA. Другой авторизованный участник той же области может получить статус. [Полный синтетический обмен](worker-example.json)

## Только синтетическая проверка и восстановление

Для БД с ревизией `0006_retention` действуют подготовка нового поколения и активация по квитанции. Команды привязки root в этом историческом разделе относятся к более ранней ревизии. [Действующая процедура восстановления](retention-operations.md#восстановление-из-независимой-копии)

```bash
bash operational/test-postgres.sh -s
python -m pytest tests/test_api.py tests/test_access.py
python scripts/build_public_demo_dataset.py --verify
ruff check .
git diff --check
```

- `test_claim_crash_orphan_stale_and_recovery` останавливает процесс после fsync и hard link, посылает SIGKILL и дожидается реального истечения lease. Старая попытка получает stale. Новый процесс находит те же байты и фиксирует один результат.
- `test_slot_limit_and_cancel_publish_linearization` синхронизирует конкурентные вызовы перед commit. `test_cancellation_publication_race_and_failure` отдельно фиксирует сторону отмены, порчу файла и ошибку БД при обновлении run после вставки artifact. Сравнивайте и terminal-статус, и число строк artifact.
- `test_bounded_delivery_resources` сначала реально регистрирует вход 4 МиБ / 10 000 позиций. Затем отдельный worker проверяет все версии и записывает сводку. Сборка A, повтор после SIGKILL, параллельная B и граничный вход выводят wall time, строки, длительности этапов, Linux VmHWM worker и его PostgreSQL backend в КиБ. При недоступном /proc показание равно null. Отдельно учитывайте shared_buffers, другие процессы кластера, ОС и второй worker. Сумма максимумов RSS отдельных процессов не является замером одновременной физической памяти узла.
- `test_summary_disconnect_replay_parallel_restore` выполняет pg_dump, копирует объекты в независимый root, выполняет pg_restore в пустую БД с восстановлением ACL. В новом кластере заранее создайте обе ограниченные роли и назначьте доверенного владельца схемы/функций. Закройте подключения приложений до проверки. Отзовите TEMP у PUBLIC на новой БД, отключите прежние сессии, перепривяжите приватный корень для каждого workspace. [Последовательность команд](snapshot-operations.md)
- На восстановленной копии повторите сверку всех source/config SHA и проекции из версий по PR-10. Для каждого succeeded проверьте FK workspace/run/artifact, SHA, размер и идентичность результата относительно snapshot и его manifest. Сверьте всех участников с проверенным состоянием перед копированием. Проверьте 401 со старой сессией, новый login, чтение, отказ чужому workspace и выделение работы под rs_worker. Повтор прежнего ключа возвращает прежний run. Новый синтетический запуск даёт те же числовые поля. Контрольный запрос ниже находит битые ссылки в метаданных. Файлы отдельно перечитывают из восстановленного root.

```sql
SELECT count(*) AS broken_links FROM analysis_run j
LEFT JOIN artifact a ON (a.workspace_id,a.run_id,a.id)=(j.workspace_id,j.id,j.output_artifact_id)
LEFT JOIN snapshot s ON (s.workspace_id,s.id)=(j.workspace_id,j.snapshot_id)
WHERE j.request_key IS NOT NULL AND j.status='succeeded' AND
 (a.id IS NULL OR s.id IS NULL OR a.storage_state<>'available' OR
  s.manifest_sha256<>snapshot_digest(s.workspace_id,s.source_id,s.batch_id,s.duckdb_sha256,s.filters,s.version_ids));
```

До открытия восстановленной БД разберите потерянные/повреждённые байты, проверьте контрольные суммы относительно независимого архива. При расхождении исходный контур сохраняется. Осиротевшие объекты остаются в приватном каталоге до решения PR-12. Публичных ссылок и политики удаления в этом этапе нет.

OpenAPI выдаёт GET /openapi.json процесса 8001. CI прикладывает полный workspace-openapi.json из того же checkout. Тест test_worker_openapi_and_legacy_wire проверяет обязательные поля, статусы HTTP и прежнюю форму Run.
