# PR-12. Испытание локального профиля

NEEDS_RETENTION_CONTRACT_REVIEW. Все новые данные — вымышленные. Исходная база PR-11 — `9747cbdb71e66c1bbcbe49101ac13bdf5fe68ea0`. GitHub подтвердил squash PR #13 в `2defefa4138096d72e0ce4e7ea9ad5a3c8809f4f`. Принятый HEAD `94c6bd8309166c21078c7ee9753ebcb0bac62795` и squash имеют одного родителя и дерево `a74c3a345195c62d3d71cf23828268c3fdd33dc9`; diff пуст.

## До миграции

Python 3.14.7, PostgreSQL 18.6, фактическая ревизия в специально созданной БД — 0005_background_jobs. Каталог показал 20 обычных таблиц, включая alembic_version. Вне RLS — alembic_version, principal, password_credential, access_session. Остальные 16 включали RLS. У runtime были только выборочные столбцы workspace, workspace_membership, source, snapshot, analysis_run, artifact и штампа Alembic, EXECUTE access_actor, login_salt, open_session, close_session, change_member, enqueue_job, cancel_job. У worker — штамп и EXECUTE claim_job, job_input, job_versions, worker_step. Ни один из этих логинов не владеет операционными объектами.

## Воспроизведение

`bash operational/test-postgres.sh -s` создаёт отдельный кластер и приватный сокет, ограниченные LOGIN-роли и новую БД для каждого теста. Сценарий owner_lifecycle_restore создаёт второй postmaster, независимо восстанавливает старый dump и копию объектов, проверяет закрытие по старой сессии и старому lease, актуальность доступа, удаление source/project/workspace и целостность B. Граничный сценарий использует синтетический источник ровно 4 МиБ с 10 000 позициями. Для PostgreSQL 16 в CI используется тот же runner. CI и замеры привязаны к коммитам в Draft PR.

Проверенные живые пути и исключения перечислены в [матрице хранения](adr-retention.md). BACKUP_ERASURE_NOT_VERIFIED, OWNER_EXPORT_REQUIRES_OPERATOR, BLOCKED_REAL_SOURCE и BLOCKED_CLIENT_PILOT. Договорные сроки и физическая ротация в реальном месте развёртывания не проверялись. Визуальный кабинет загрузки запланирован на отдельный PR-13.
