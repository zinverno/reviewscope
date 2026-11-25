# PR-21 · Переранжирование сохранённых кандидатов

Экспериментальный профиль `mmarco-context-logit-v1` упорядочивает фрагменты одного успешного bound-поиска. Вызов добровольный. Старый результат открывается самостоятельно. В кабинете профиль доступен в том же допущенном синтетическом источнике владельцу или аналитику. Панель показывает число версий и фрагментов, исходную редакцию определения, порядок до/после и ссылку на прежнее доказательство. Качество на реальных источниках остаётся неизвестным. [Измерения](rerank-results.md)

## Новый контракт

- `POST /api/v1/workspaces/{w}/snapshots/{s}/candidates/{parent}/rerank` принимает только `source_id`, UUID `idempotency_key` и точный `profile`. Штатные HTTPS, сессия, CSRF, роль, witness и admission обязательны. Ответ 202, при идентичном повторе 200. Тексты клиента, модельный путь и самостоятельный выбор версии отсутствуют в запросе. Приём на архивный аспект закрыт. Для свободного поиска привязка отсутствует и новый метод недоступен.
- `GET …/{parent}/rerank/{run}?source_id=…` разрешает результат через две независимые проверки байтов в брокере. Порядок по умолчанию определяется прежним поиском. В URL панели добавляется UUID `rerankRun`. При открытии фрагмента читается полная исходная версия через PR-19.
- Отдельный вид задания `snapshot_rerank_v1`, схема объекта `candidate-rerank-1`. Ссылки включают parent run, SHA исходного объекта и результата, snapshot/manifest, SHA запроса, project/source/aspect/version/digest. Артефакт хранит ссылки, исходные ранги, новые ранги и scores. Окна, тексты и исходные оценки каналов читаются из первоначальных записей. Миграция 0015 связывает запуски внешним ключом в одной области retention. Оператор фиксирует актуальную печать доступа для восстановления. Экспорт, удаление и восстановление используют прежний ledger.
- Кандидаты ограничены десятью разными версиями и 40 фрагментами. Перед публикацией worker сверяет manifest, исходные байты, все ссылки и текущий допуск, SQL отдельно сверяет контрольные суммы, состав пула и ранги. Начавшая запуск сессия должна оставаться действительной до публикации. Изменение v1 → v2 оставляет исходную ссылку на v1. Закрытие доступа, отзыв сессии, потеря lease, отмена и нарушение целостности закрывают публикацию. Успешное чтение требует действующей сессии и разрешения в момент чтения.
- Вход модели состоит из исходной query и полного окна `[context_start,context_end)` точной версии. Пределы в токенах со special tokens составляют 128 для запроса и anchor и 512 для полной пары. Тихого сокращения нет. Превышение любого из трёх пределов сохраняет исходный порядок с конкретной причиной. При отсутствии backend, весов, сетевой изоляции или при несовместимости профиля ответ содержит `BASELINE_RETAINED`, причину и пустой массив новых рангов. Отдельно доступны состояния очереди, `RESOURCE_LIMIT` и отказ в доступе.
- CPU FP32, batch 8, один исходный logit головы sequence classification. Отрицательные значения допустимы. Сортировка по убыванию score и по возрастанию fragment ID при равенстве. Первый фрагмент определяет ранг отзыва. Scores характеризуют релевантность для одного метода. Калибровка вероятности жалобы и соответствие шкал моделей не исследованы.
- Модель запускается только в дочернем процессе с анонимными каналами и online-disabled окружением. Процесс требует Linux network namespace с единственным loopback (или без интерфейсов). Функция разрешает только пять файлов закреплённой ревизии, сверяет размер и SHA-256 каждого, использует local-only safetensors без remote code. В ASGI веса не загружаются. Ресурсная граница worker — 120 с, 2 ГиБ дочернего RSS, 64 КиБ на объект. Persistent index отсутствует.
- Профиль E5 доступен в исследовательском benchmark. Входы документа и запроса имеют разные ключи и буквальные префиксы `passage: ` и `query: `. Для поиска по примеру сохраняется асимметричная постановка «пример ищет соответствующий пассаж». Используются masked mean, L2 и 384 числа float32. Для сопоставимости с PR-19 действует предел 128, включая префикс и служебные токены. Исходные MiniLM cache keys и отбор PR-19 имеют собственный контракт.

## Воспроизведение

Базовый код на `0913881c4ab6d44b11366647343be851027e15a2`, tree `e42faa3b2d3ebf3e2a0faab79fb7628c5cf2aa4b`. Перед первым ранжированием коммит `faf296c` закрепил 110 отзывов, 24 запроса, правила и обе ревизии. Точные SHA корпуса, протокола и старой регрессии находятся в [freeze manifest](../../tests/data/fixtures/rerank-freeze.sha256). Для MiniLM используется cache Hub PR-04 с ревизией `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`. Публичные E5 (MIT, `614241f622f53c4eeff9890bdc4f31cfecc418b3`) и mMARCO (Apache-2.0, `1427fd652930e4ba29e8149678df786c240d8825`) описаны в [manifest файлов](../../src/reviewscope/embeddings/rerank_models.json). [MiniLM](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/tree/e8f8c211226b894fcb81acc59f3b34ba3efd5f42) · [E5](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3) · [mMARCO](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/tree/1427fd652930e4ba29e8149678df786c240d8825)

Из корня checkout, с установленным Python-пакетом и его `embeddings` extra, подготовьте публичные файлы до обработки данных. Задайте `RS_RERANK_MODELS` как закрытый операторский каталог на диске вне Git. Имена подкаталогов совпадают с полными SHA. Проверенная конфигурация CPU использует sentence-transformers 6.1.0, transformers 5.17.0, tokenizers 0.23.2 и torch 2.14.0. Окружение worker получает путь только из конфигурации оператора.

```bash
python scripts/prepare_rerank_models.py "$RS_RERANK_MODELS"
sha256sum -c tests/data/fixtures/rerank-freeze.sha256
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
export PYTHONPATH="$PWD/src:$PWD"
unshare --user --map-current-user --net python -m scripts.benchmark_rerank validation_data/benchmarks/pr21-fresh
python -m scripts.report_rerank validation_data/benchmarks/pr21-fresh validation_data/benchmarks/pr21-summary
unshare --user --map-current-user --net python -m scripts.profile_rerank
unshare --user --map-current-user --net python scripts/benchmark_retrieval.py --minilm
unshare --user --map-current-user --net python scripts/benchmark_candidates.py --minilm
bash operational/test-isolated.sh -s --tb=short -k 'derived_rerank or aspect_restore'
```

Для полных штатных групп, включая PostgreSQL 0013 generic/custom, см. [.github/workflows/ci.yml](../../.github/workflows/ci.yml). У браузерной проверки отдельные тестовые CA, кластер и namespace. Команды обработки данных не требуют внешних запросов. Настройка рабочей станции может потребовать оператора для создания network namespace.

## Handoff

Следующему этапу доступны immutable aspect/version/digest, исходный anchor и соседний контекст, оба ранжирования, привязки к артефактам и авторская разметка. Подсчёт жалоб по полному корпусу требует отдельной оценки stance и атомарной публикации частями. Открыты `REAL_LABELS_NOT_AVAILABLE`, `BLOCKED_REAL_SOURCE`, `BLOCKED_CLIENT_PILOT`, `BACKUP_ERASURE_NOT_VERIFIED`, `OWNER_EXPORT_REQUIRES_OPERATOR`, `ANALYTICS_OUTPUT_SCALING_DEBT`, `UX_REJECTED`. Продуктовый редизайн запланирован после PR-33.
