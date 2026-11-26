# Локальная регистрация и восстановление поставки

Выполняйте команды из checkout с установленным extra `operational`. Оператор работает через явный локальный Unix-сокет PostgreSQL, доверенный ОС-аккаунт и выделенную роль владельца схемы. DBA, ОС и каталог хранения входят в доверенную область. Одна такая роль привязывается к одному principal на источнике. Браузерные credentials и rs_runtime для CLI непригодны.

## До импорта

1. Проверьте разрешение на хранение файла и паспортов, внешний ключ источника, точки, фильтры, полноту и факт независимого получения. Используйте синтетические A/B для приёмки. Рабочие файлы и абсолютные пути держите вне Git. Закройте SQL echo, протоколирование параметров, shell tracing и запись истории с чувствительными значениями. [Настройки журналов и ролей](access-operations.md)
2. Остановите запись и сделайте проверенный архив прежней ревизии. Установите `PGHOST` в путь сокета, задайте `PGDATABASE`, `PGUSER` и закрытый passfile. Уберите `PGSERVICE` и `PGHOSTADDR`. Сверьте целевую БД в своей приватной сессии.
3. Примените миграцию. При LEGACY_PLACE_UNRESOLVED оператор приватно проверяет все старые версии и source_location. Для выявления проблем выполните запрос ниже на восстановленной копии. При неоднозначности требуется отдельный согласованный план исправления данных и новой миграции. До его проверки сохраните прежнюю базу и архив.

```bash
umask 077
python -m alembic -c operational/alembic.ini upgrade head
python -m alembic -c operational/alembic.ini current
```

```sql
SELECT i.workspace_id,i.source_id,i.id,count(DISTINCT v.external_place_id) AS places
FROM review_identity i LEFT JOIN review_version v
  ON (i.workspace_id,i.source_id,i.id)=(v.workspace_id,v.source_id,v.identity_id)
GROUP BY i.workspace_id,i.source_id,i.id
HAVING count(DISTINCT v.external_place_id)<>1 OR bool_or(v.external_place_id IS NULL);
```

4. Используйте существующие workspace/source/source_location. Ожидаемое значение PR-09 source берётся из source.external_source_id, а при NULL из source.source_key. Укажите подтверждённого principal с активным членством owner/analyst. В приватном окружении задайте переменные ниже. OBJECT_ROOT — абсолютный канонический путь на локальной файловой системе с hard link и fsync, в доверенном родительском каталоге, вне путей публичных артефактов. Пустой новый каталог создаёт оператор с режимом 0700. Все компоненты под корнем принадлежат оператору. Приватная колонка workspace.delivery_object_root задаёт один архив всем источникам и операторам workspace. Привязанный каталог меняют только при подтверждённом переносе полного архива.

```bash
mkdir -m 700 -- "$OBJECT_ROOT"
export OBJECT_ROOT WORKSPACE_ID SOURCE_ID PRINCIPAL_ID
psql -X -q -v ON_ERROR_STOP=1 <<'SQL'
\getenv w WORKSPACE_ID
\getenv s SOURCE_ID
\getenv p PRINCIPAL_ID
\getenv root OBJECT_ROOT
BEGIN;
UPDATE workspace SET delivery_object_root=:'root' WHERE id=:'w' AND delivery_object_root IS NULL;
INSERT INTO delivery_authorization(workspace_id,source_id,operator_login,principal_id)
SELECT id,:'s',session_user,:'p' FROM workspace WHERE id=:'w' AND delivery_object_root=:'root';
COMMIT;
SQL
```

При отсутствии согласования не создавайте эту привязку. Для отзыва разрешения удалите только соответствующую строку delivery_authorization. История поставок при этом сохраняется.

## Проверка и применение

Для синтетического A укажите пути ниже. Для B смените вход и паспорт, получите собственный идентификатор операции. Храните выданный `DELIVERY_ID` для всех повторов одной команды. Скрипт ожидает явный UUID даже при dry-run. В примере COVERAGE_FILE — новый приватный файл оператора. Null сохраняет неизвестное значение.

```bash
set -euo pipefail
umask 077
fixture=tests/data/fixtures/import_preview
INPUT="$fixture/a.csv"
MAPPING="$fixture/mapping.json"
PASSPORT="$fixture/a.passport.json"
FORMAT=csv
( set -C; cat > "$COVERAGE_FILE" <<'JSON'
{"coverage":"unknown","filters":null,"mode":null,"basis":null,"covered_from":null,"covered_until":null,"received_at":null}
JSON
)
preview_sha=$(python -IS scripts/preview_import.py --format "$FORMAT" --input "$INPUT" \
  --mapping "$MAPPING" --passport "$PASSPORT" | python -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')
args=(--workspace "$WORKSPACE_ID" --source "$SOURCE_ID" --delivery-id "$DELIVERY_ID"
      --input "$INPUT" --format "$FORMAT" --mapping "$MAPPING" --passport "$PASSPORT"
      --coverage-passport "$COVERAGE_FILE" --object-root "$OBJECT_ROOT" --preview-fingerprint "$preview_sha")
python -m operational.import_delivery "${args[@]}" --dry-run
python -m operational.import_delivery "${args[@]}" --apply
python -m operational.import_delivery "${args[@]}" --apply
```

Ожидаемые исходы для нового A — planned, applied, already_applied с 3 принятыми позициями. Последние две команды возвращают один snapshot ID. Смена байтов при повторе требует разбора delivery_conflict или preview_mismatch. Для full укажите объект filters, mode period/cumulative, основание basis и обе границы периода при period. Времена в covered_from, covered_until и received_at при наличии требуют смещения. Интервал полуоткрытый. Нужда в основании full не заменяет проверку оператором. Если сведения неизвестны, оставьте NULL.

## Проверка отказа и восстановление

Для БД с ревизией `0006_retention` действуют подготовка нового поколения и активация по квитанции. Команды привязки root в этом историческом разделе относятся к более ранней ревизии. [Действующая процедура восстановления](retention-operations.md#восстановление-из-независимой-копии)

Ниже только одноразовый синтетический кластер. Проверки моделируют ошибку SQL после записи объектов, испорченный manifest, потерю объекта перед COMMIT и конфликт данных с тем же token. Они проверяют отсутствие частичного batch, version и snapshot, затем штатный повтор. Запуск всего файла также проверяет A/B, pg_dump, отдельный каталог файлов и pg_restore в новую базу.

```bash
bash operational/test-postgres.sh -s -k 'scope_failures_and_rollback or precommit_validation'
bash operational/test-postgres.sh -s -k 'two_deliveries_restore_every_version_and_byte'
```

Для закрытого восстановления остановите все процессы записи до окончания dump и копирования. Для каждой используемой директории объектов сделайте отдельную копию на независимый носитель. Следующие команды показывают один корень и одну область, каталог BACKUP должен быть новым, приватным и вне OBJECT_ROOT. Заранее создайте роли, включая rs_runtime, и выберите свободное имя RESTORE_DB в доверенном кластере. Закройте подключение приложений до проверки всех областей, настроек журналов, ACL и отзыва сессий по инструкции PR-08.

```bash
set -euo pipefail
umask 077
mkdir -m 700 -- "$BACKUP"
pg_dump -Fc -f "$BACKUP/database.dump"
cp -a -- "$OBJECT_ROOT" "$BACKUP/objects"
createdb "$RESTORE_DB"
pg_restore --single-transaction --exit-on-error --no-owner -d "$RESTORE_DB" "$BACKUP/database.dump"
export PGDATABASE="$RESTORE_DB" RESTORED_ROOT
cp -a -- "$BACKUP/objects" "$RESTORED_ROOT"
psql -X -q -v ON_ERROR_STOP=1 <<'SQL'
\getenv w WORKSPACE_ID
\getenv s SOURCE_ID
\getenv root RESTORED_ROOT
SELECT format('REVOKE TEMP ON DATABASE %I FROM PUBLIC',current_database()) \gexec
UPDATE access_session SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL;
UPDATE workspace SET delivery_object_root=:'root' WHERE id=:'w';
SQL
```

Следующая проверка использует только восстановленную БД и копию файлов. Выполните её для каждой подтверждённой пары workspace/source. Защитите список квитанций как часть приватного архива. В ходе synthetic restore теста полные строки import_batch, import_row, review_identity, review_version, snapshot и snapshot_member также сравниваются между исходной и новой БД. Для старых объектов DuckDB и артефактов запустите сверку по инструкции PR-07. [Архив и SHA PR-07](operational-schema.md)

```bash
psql -XAt -F '|' -v ON_ERROR_STOP=1 -v w="$WORKSPACE_ID" -v s="$SOURCE_ID" \
  > "$BACKUP/receipts.psv" <<'SQL'
SELECT id,delivery->>'format',file_sha256,delivery->>'mapping_sha256',delivery->>'passport_sha256',
       delivery->>'coverage_sha256',delivery->>'fingerprint'
FROM import_batch WHERE workspace_id=:'w' AND source_id=:'s' AND delivery IS NOT NULL ORDER BY id;
SQL
objects="$RESTORED_ROOT/$WORKSPACE_ID/sha256"
while IFS='|' read -r token fmt file_hash mapping_hash passport_hash coverage_hash fingerprint; do
  python -m operational.import_delivery --dry-run --workspace "$WORKSPACE_ID" --source "$SOURCE_ID" \
    --delivery-id "$token" --format "$fmt" --object-root "$RESTORED_ROOT" \
    --input "$objects/$file_hash" --mapping "$objects/$mapping_hash" --passport "$objects/$passport_hash" \
    --coverage-passport "$objects/$coverage_hash" --preview-fingerprint "$fingerprint" |
    python -c 'import json,sys; assert json.load(sys.stdin)["outcome"] == "already_applied"'
done < "$BACKUP/receipts.psv"
```

Сверьте число проверенных квитанций с инвентарём перед резервированием. При расхождении изолируйте восстановленную копию и сохраните оба архива. Потерянный файл восстанавливают из известной копии с тем же SHA. Возможный orphan после SQL rollback сохраняйте для инвентаризации. Порядок безопасной очистки — решение PR-12.

При разборе конкуренции операторского повторного импорта с вычислением PR-22 используйте тест порядка блокировок на отдельном кластере. Команда, условия изоляции и наблюдаемый результат приведены в [проверке PR-26](saved-monitors-results.md). Внешнее удаление отзыва нельзя установить по его отсутствию в очередной поставке.


### Ручной цикл монитора

В 0021 оператор предоставляет отдельный `monitor_allowed` для точного checksum синтетической поставки при действующих evidence/candidate/decision допусках. После предпросмотра и регистрации операторский и браузерный импортеры получают workspace SHARE до source. В кабинете выберите сохранённое определение PR-20, источник и снимок. Доступный предыдущий успешный запуск можно выбрать явно. «Проверить поставку» ставит обычную задачу PR-22 в действующую очередь. В истории доступны обработанные знаменатели, происхождение строк и исходные доказательства. Последнюю регистрацию, полученное от источника время и coverage показывает паспорт.

Для резервного копирования, owner export и очистки используйте существующие процедуры с текущим seal версии 0021. Миграция сохраняет прежние объекты и оставляет новый допуск выключенным. Проверки роли, ledger и независимого хранилища выполняются также при восстановлении. [Контракт и команды](saved-monitors-contract.md)
