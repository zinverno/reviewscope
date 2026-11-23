# PR-08 · Обновление и восстановление оператором

Используйте выделенную БД и отдельные libpq service/passfile с правами 0600 для владельца схемы и приложения. В тестовом кластере роли называются `rs_migration` и `rs_runtime`. Владелец схемы в рабочем контуре может иметь другое имя. Не передавайте DSN с паролем, токен, пароль пользователя или байты ключа в аргументах, shell history, SQL-консоли с записью истории и CI.

1. Остановите доступ к обновляемому экземпляру, зафиксируйте текущую ревизию. Создайте закрытый custom dump PR-07 и отдельную копию объектов, сверьте SHA по инструкции PR-07. Проверьте восстановление в отдельную БД. Сохраните исходный экземпляр до проверки результата. [Резервные копии PR-07](operational-schema.md)
2. DBA создаёт отдельный LOGIN `rs_runtime` с NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, NOREPLICATION, без владения объектами и без членства в других ролях. Например, `createuser --login --no-superuser --no-createdb --no-createrole --pwprompt rs_runtime` при новом выделенном кластере. Ограничьте pg_hba этой БД и доверенным локальным процессом. Выполнение миграций требует владельца схемы с правами менять ACL БД и public. Миграция отзывает TEMP у PUBLIC в выделенной БД и CREATE у PUBLIC в public.
3. Настройте закрытый ключ сервера (32 случайных байта) вне репозитория. В закрытом каталоге задайте переменную `RS_ACCESS_KEY_FILE` с новым путём. Команда ниже откажется перезаписывать существующий файл. Храните отдельную защищённую резервную копию ключа. Его потеря требует перевыпуска паролей, смена ключа также обрывает старые браузерные сессии.

```bash
umask 077
python -c 'import os,secrets; p=os.environ["RS_ACCESS_KEY_FILE"]; f=open(p,"xb"); f.write(secrets.token_bytes(32)); f.close()'
python -m pip install -e '.[operational,access,dev]'
python -m alembic -c operational/alembic.ini current
python -m alembic -c operational/alembic.ini upgrade 0002_principals
```

4. Приватно проверьте старые UUID с владельцем. Для количества нерешённых связей используйте запрос ниже в операторской сессии. Для каждого подтверждённого прежнего UUID создайте аккаунт через `python -m reviewscope.access.operator --verified-legacy-id "$verified_uuid"`. Имя и пароль вводятся интерактивно. Оператор сохраняет имеющиеся роли, состояния и UUID. Если личность установить нельзя, оставьте контур на промежуточной ревизии с закрытым runtime. Нельзя назначать случайного владельца, стирать строки или вручную ставить Alembic stamp.

```sql
SELECT count(DISTINCT m.principal_id)
FROM workspace_membership m LEFT JOIN principal p ON p.id=m.principal_id
WHERE p.id IS NULL;
```

```bash
python -m alembic -c operational/alembic.ini upgrade head
python -m alembic -c operational/alembic.ini current
```

5. В новом workspace оператор запускает `python -m reviewscope.access.operator --workspace "$workspace_uuid" --role owner`. Для нового аналитика или читателя укажите analyst или viewer. Для ранее подтверждённого аккаунта оператор может создать строку workspace_membership с его исходным UUID и согласованной ролью в приватной транзакции. Не выдавайте приложению роль оператора. Owner управляет состоянием существующего членства через PATCH. Чтобы закрыть аккаунт целиком, оператор переводит principal.state в inactive. Триггер отзывает действующие сессии.
6. Включайте доступ после проверки конфигурации журналов. Выключите access log, SQL echo, трассировку тел/параметров и запись секретных аргументов в прокси, APM и DB-аудит. Для выделенной БД DBA проверяет `log_statement=none`, `log_duration=off`, `log_min_duration_statement=-1`, `log_min_duration_sample=-1`, `log_transaction_sample_rate=0`, `log_parameter_max_length=0`, `log_parameter_max_length_on_error=0`, `log_min_error_statement=panic`. Параметры отладочных расширений и сборщик журналов также под контролем DBA. Сохраните обычные инфраструктурные сигналы без текста запросов.
7. Выберите libpq-сервис приложения. `RS_ACCESS_HTTP_LOOPBACK=1 python -m reviewscope.access` запускает один локальный процесс на 127.0.0.1:8001. По умолчанию API принимает только HTTPS. Для локального TLS задайте пути `RS_ACCESS_TLS_CERT` и `RS_ACCESS_TLS_KEY` к доверенному сертификату с IP SAN 127.0.0.1 и закрытому ключу, затем запустите `python -m reviewscope.access` без HTTP-исключения. Не включайте пересылку заголовков или внешний bind.
8. Проверьте вход двух синтетических операторских аккаунтов, совпадение чужого/неизвестного 404, отзыв членства и штатную выдачу 401 после logout. Автоматический воспроизводимый вариант создаёт свой Unix-сокет, две отдельные роли и временные БД — `bash operational/test-postgres.sh -s`. Для проверки штатного TLS-запуска нужны openssl и свободный порт 127.0.0.1:8001. Скрипт не принимает адрес существующей БД.

## Восстановление после PR-08

Архив содержит логины, соли, верификаторы, хэши сессий, аудит и сами операционные данные. Храните dump и копии объектов приватно. Для резервирования заморозьте изменения. Для восстановления предварительно создайте runtime-роль в целевом кластере, восстановите базу в новый пустой экземпляр от имени доверенного владельца схемы. Сохраните ACL из архива. Флаг `--no-privileges` из старой инструкции PR-07 здесь непригоден. При восстановлении без `--create` отдельно примените ACL самой новой базы. Не подключайте приложение до завершения сверки.

```bash
pg_dump -Fc -f "$backup_dir/operational.dump" "$PGDATABASE"
createdb -T template0 "$restore_db"
pg_restore --single-transaction --exit-on-error --no-owner -d "$restore_db" "$backup_dir/operational.dump"
PGDATABASE="$restore_db" psql -X -v ON_ERROR_STOP=1 -v target="$restore_db" <<'SQL'
BEGIN;
REVOKE TEMPORARY ON DATABASE :"target" FROM PUBLIC;
UPDATE access_session SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL;
COMMIT;
SQL
PGDATABASE="$restore_db" python -m alembic -c operational/alembic.ini current
```

Сверьте UUID, состав snapshot_member, manifest, SHA объектов, аккаунты, состояния и события членства. Учтите все отзывы доступа и изменения удостоверений после точки копии до открытия API. При неизвестном актуальном статусе сохраните закрытый доступ. Повторите вход и двухстороннюю проверку изоляции на восстановленном экземпляре. Для отката используйте отдельную проверенную предмиграционную копию с прежней версией приложения. Обе новые ревизии forward-only.
