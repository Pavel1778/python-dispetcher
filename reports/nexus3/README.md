# NEXUS 3.0 — исходный baseline и допуск к миграции

**Это не release candidate.** Предмет Phase 0 — действующая платформа при входе в миграцию, не обещания в документах 2.0. Исходный `main` и начальный HEAD ветки `arena/6f80793e-python-dispetcher`: **`2edf3aa70eefb0628fa88997a1d1c095b133e007`**. Production, его БД, аккаунты OpenHands и пользовательские репозитории не использовались. Ниже `VERIFIED` всегда ограничено указанным стендом и проверкой.

**Продолжение после merge #54:** [Phase 2: надёжная отправка OpenHands, crash-тесты и стоимость Python worker](dispatch/README.md) — отдельный review gate в draft PR #55. Цифры и статусы ниже относятся к предыдущему этапу.

**Навигация:** [ADR и контракты (Phase 1)](COMPATIBILITY.md) · [две транзакции Rust, тесты и ограничения (Phase 2)](STORAGE.md) · [PR #54, теперь merged](https://github.com/Pavel1778/python-dispetcher/pull/54). Документ различает исходный baseline, проверенные изменения в ветке и условия будущего cutover.

**Итог на последней ревизии продукта `170eaf96e23b56a89adca0a3c0c168eb3261b142`:** локальная полная suite — **2089 passed, 32 skipped in 464.82s**; [hosted CI](https://github.com/Pavel1778/python-dispetcher/actions/runs/44786382445) — **2102 passed, 19 skipped, 1 warning in 564.65s**, отдельный Linux/PG17 Rust job — locked build, unit tests и **22 passed in 2.22s** в Python↔Rust gate. Результаты **не складываются**: отдельный job исполняет и часть ранее проверенных parity cases. Разбивка по четырём CI runs, пяти local suites, ревизиям и пропускам — [checks.json](evidence/checks.json). В Python CI job 13 из 19 skips — тесты storage probe, которые выполняет Rust job; локально дополнительно нет Rust toolchain в PATH suite и Android SDK. Не выдаём skip реального cloud task за pass.

Две операции Rust проверены транзакционно, но последний тест на клонах одной БД дал медиану **488.0 успешных выдач/с у Rust и 500.35 у Python**. Экономия памяти драйвера не доказывает ускорения всего worker. Поэтому операционное решение — **не переключать планировщик**. Разрешения на rollout или RC нет.

## 1. Что сохранили

* Начальный `git status --porcelain` пуст. Checkout shallow; отсутствие локального remote-tracking ref не означает отсутствие ветки на сервере. `git ls-remote`, `gh pr list --state all`, `gh api …/actions/runs` проверены заново. [Снимок](evidence/refs.json): 8 remote heads, 17 tags (без peeled псевдорефов), 53 PR на момент входа: 40 merged, 12 closed, 1 open. PR #53 уже в исходном SHA. #52 (`5c001f90f5b90b25dadd1cb2412dc85df3b62015`) **не предок** main; деревья тоже не совпадают. Не закрывать и не удалять его ветку на основании старого отчёта.
* Сделан read-only полный mirror, из него — git bundle remote heads + tags + HEAD, не bundle shallow-клона. В **пустом** bare repo `git bundle verify`: complete history. Новое восстановление из bundle: `git fsck --full` успешно, все 25 head/tag ref идентичны, 385 достижимых коммитов, восстановленный HEAD равен исходному. Локальная копия `.restore-points/nexus3/baseline.bundle` (gitignored), SHA-256 `ad07a39f08ea55d13dc38e7df79a5e76666dad0cc4a33837e2d7c9316a73dea5`. Это не off-site backup и не копия внешних проектов/БД/LFS/issues.
* Миграции применены штатным `PostgresStore.apply_migrations` в пустом PostgreSQL 16.2. Первый проход: **43** файла; второй: **0**; `verify_schema()` → `[]`. [Фактический каталог](evidence/catalog.json): 51 таблица, 603 столбца, 164 индекса, 89 FK. RLS на 50 таблицах; оставшаяся — `schema_migrations`. Сам по себе флаг RLS не доказывает изоляцию приложения. Все 43 SHA-256 сверены с файлами; [catalog.sql](catalog.sql) читает только метаданные в одной read-only транзакции.
* До изменений UI сохранены [девять PNG и манифест](studio/manifest.json). Реальные login, home, chat, файлы с кодом, пустая вкладка агента, settings (1440×900) и home, chat, history drawer (390×844). Данные тестовые, настоящий HTTP, session/CSRF и PG; только внешние сервисы — локальные doubles. Повторный capture с новым кластером, логином и conversation id: **9/9 pixel-identical**, без маскирования DOM. Это проверка воспроизводимости **до миграции**, не Android device test и не сравнение с Rust API.

## 2. Что сейчас обслуживает запросы

| Тракт | Установлено по коду | Что нельзя потерять / где тест |
|---|---|---|
| API/Studio | `web_server.App`, `WebServer` и `web_auth` в Python, не Axum. HTML/static и PWA с того же origin; проектный URL/hash, mobile drawers, polling. `/readyz` делает SQL, `/healthz` нет | Cookie, CSRF, bearer, права на каждую запись; JS без нового фреймворка. `test_route_auth`, `test_project_isolation_routes`, `test_browser_e2e` |
| Хранилище и задачи | `cloud_store.PostgresStore` (psycopg pool; min 1/max 8 по умолчанию); legacy `tasks/workflows` отдельно от `nexus_runs/nexus_run_tasks`. SQLite `control_plane` — отдельный локальный путь. Нельзя считать его PG-очередью | Idempotency ключи, зависимость по task_key в рамках run, `lease_token`, ordered locks, истечение и ветки. `test_transactions`, `test_race_protection`, `test_lab_store`, `test_cancel_lock_order` |
| Worker | `lab_worker` + `LabScheduler`, бывает inline или внешним. Пробуждение не заменяет периодический sweep. Durable cycles и bounded deadline различаются | Reclaim только не привязанного remote start. Следить за уже созданным разговором после рестарта. Не объявлять готовым по одному ответу агента |
| Интеграции | `cloud_direct` управляет sandbox/agent-server; `cloud_client` — App V1. `openhands_pool/router`, persisted account selection/fallback, rate limits; GitHub identity/branch authorization и confirmations раздельны | `test_cloud_direct`, `test_openhands_failover`, `test_github_confirmations`, `test_start_handover`. Локальные doubles не подтверждают доступность облака |
| Результат | `workflow_verifier`, `nexus_selfqa`, `nexus_evidence` / Ed25519, `nexus_reports`: проверки конкретного коммита, проектная политика auto/on/off, подпись/цепочка, идемпотентный отчёт в chat | `test_quality_gate_policy`, `test_lab_evidence`, `test_selfqa`, `test_run_reports`. Проверки репозитория исполняют код — изоляция процесса/секретов обязательна перед расширением concurrency |
| CLI/MCP | Python HTTP CLI `nexus` и MCP NDJSON stdio + `POST /mcp`; 19 зарегистрированных tools, объявленная ревизия `2024-11-05`. Не произвольная консоль | Principal и membership для ID, протокол и ошибки JSON-RPC. `test_cli_client`, `test_cli_mcp` |
| Rust | На исходном SHA только domain/core/CLI: решения о состоянии, DAG/plan hash, retry, snapshot eligibility. `benchmarks/rust-db` — исследовательский SQLx **benchmark**, не worker или API | `test_rust_parity` сравнивает часть решений и две DB-ситуации. Снимок одного run не видит requested stops, других runs и глобальной вместимости. Он не может разрешать реальную запись |
| Пакеты | `packaging/android` — Java WebView + aapt/d8/apksigner, `debian` — launcher default browser, `windows` — Win32 launcher via MinGW. Не Tauri 2. В исходном CI два Ubuntu jobs, нет Windows runner/NSIS acceptance | Не переименовывать пакеты 2.0 в 3.0. Нужны Debian 13, WebView2/Windows, release signing и физический Android |

## 3. Измерения — Python baseline, не ускорение

Однопроходные независимые локальные прогоны, без одновременного pytest. Debian 12 x86-64, 2 vCPU, MemTotal 4,034,452 KiB, Python 3.11.2, psycopg 3.3.9, PostgreSQL 16.2 в отдельном процессе на том же хосте, fsync по умолчанию, база на приватном Unix socket, HTTP через loopback. Это **не** целевой dedicated 4 CPU / 8 GB. Сравнивать на том же хосте с тем же инструментом и dataset, не с числами из старых веток.

| Нагрузка / операция | p50 / p95 / p99, мс | Throughput | CPU процесса¹ / RSS после |
|---|---:|---:|---:|
| Idle, 3 секунды после прогрева | — | 0 клиентских запросов | 2.39% / 57.67 MiB |
| Список чатов, warm sequential (40) | 1.885 / 2.168 / 2.410 | — | — |
| Moderate: 5 клиентов × 20 чтений | 9.611 / 14.186 / 17.030 | 499.6 req/s, 0 ошибок | 106.09% / 58.07 MiB |
| Stress: 50 × 20 чтений | 11.599 / 1022.368 / **3046.876** | 231.1 req/s, 0 ошибок | 44.83% / 58.67 MiB |
| Старт run с 40 задачами (40 стартов) | 4.757 / 5.466 / 9.093 | — | — |
| Успешный claim (40) | 2.672 / 3.459 / 4.659 | — | — |

¹ `time.process_time` / wall ×100%, 100% = одно ядро. Это **весь Python benchmark process** (NEXUS, нагрузчик, HTTP doubles), без PostgreSQL и браузера; RSS до/после окна, не peak host memory. Нет измеренного изменения RAM/CPU продукта 3.0. Хвост порядка секунд при 50 клиентах требует отдельно разделить ожидание соединения/accept, thread scheduling и PG pool; причина ещё не доказана.

* [HTTP JSON](evidence/api_bench.json): 3 проекта × 10 чатов × 40 сообщений. Не усреднять p99 отдельных endpoints. Малая выборка — baseline для повторения, не SLO.
* [Scheduler JSON](evidence/scheduler_bench.json): для каждого из 1/5/10/25 workers — новый run с **500 различными успешно захваченными ID**; **315.8 / 343.8 / 331.1 / 311.5 claims/s**. Пустой repo, без внешнего выполнения, `NEXUS_LAB_MAX_ACTIVE_TASKS=0` только для измерения; это не рекомендация отключить лимит. Исправлен **инструмент**: старый `--repeat` игнорировался, промах мог попасть в latency, исключение фонового потока не проваливало benchmark. Поэтому не заявляем выигрыш от сравнения с прежним JSON.
* [Сценарии](evidence/e2e_scenarios.json): `send_message` 15.37 / 16.53 мс (p50/p95, n=5), `start_run` 26.21 / 27.25 (n=3). Не latency реального LLM/OpenHands. Production-like successful verified tasks/hour, аллокации, device timings, длительный soak и полный расход PostgreSQL на целевом хосте — **NOT VERIFIED**.

## 4. Воспроизведение без риска для данных

**Не передавать тестам адрес существующей БД:** в suite есть сбросы схемы и изменения cluster-wide ролей. Из корня на Linux под непривилегированным пользователем: `initdb`, `pg_ctl`, `createdb`, `dropdb`, `psql`, `pg_dump` и `pg_restore` одной major-версии на PATH; Python venv с `requirements.txt` + `requirements-dev.txt`, Node, Git, Chromium. В данном прогоне PG 16.2/tools из `pgserver==0.1.4`; можно использовать системный PostgreSQL. Wrapper создаёт кластер/домашний каталог в новой mode-0700 директории `/tmp`, не открывает TCP, не передаёт DSN, git/provider credentials вызывающего и убирает только свой кластер.

```sh
W=tools/with_disposable_postgres.sh
ruff check .
$W python -m pytest -q -p no:cacheprovider -rfEs
$W python -m pytest -q tests/test_backup_restore.py tests/test_self_hosting.py
$W python benchmarks/api_bench.py --repeat 40 --resources --out /tmp/api.json
$W python benchmarks/scheduler_bench.py --repeat 40 --out /tmp/scheduler.json
$W python benchmarks/e2e_scenarios.py --out /tmp/scenarios.json
$W sh -ec 'python -c "import os; from orchestration.cloud_store import PostgresStore; s=PostgresStore(os.environ[\"TEST_SUPABASE_DB_URL\"]); assert not s.apply_migrations() and not s.verify_schema(); s.close()"; psql "$TEST_SUPABASE_DB_URL" -XqAt -v ON_ERROR_STOP=1 -f reports/nexus3/catalog.sql'
$W python tools/studio_snapshots.py /tmp/new-studio --compare reports/nexus3/studio
```

Для `--compare` нужен Pillow. Визуальная конфигурация: Linux x64 `@sparticuz/chromium@153.0.0` (headless-shell 153.0.8010.0), Playwright 1.65.2, шрифты Studio и bundled Open Sans/fontconfig. Для этого варианта развернуть пакет через `chromium.executablePath()` с AL2023 libraries, передать `E2E_CHROMIUM_EXECUTABLE`, `E2E_CHROMIUM_LD_LIBRARY_PATH` и `FONTCONFIG_PATH`. Стандартный Playwright Chromium тоже делает capture; для побитового сравнения нужны одинаковые сборка/шрифты. Время сообщения фиксировано **в тестовых данных**, не изменением HTML/CSS.

## 5. Результаты и допуск

Исходный CI **main**, не CI этой ветки: [run 44762662171](https://github.com/Pavel1778/python-dispetcher/actions/runs/44762662171), проверены jobs и check annotations (`136378955258`, `136378955839`). Ubuntu 26.04 / PG17, Chromium 159.0.8154.6: **2097 passed, 6 skipped, 1 warning in 567.09s**; отдельный Rust job: release build и `cargo test` success, Python/Rust parity **9 passed in 0.71s**. В исходном workflow Cargo запускался без `--locked`. Это не доказательство пригодности нового worker.

Отдельно, **локально** (одинаковый Python/PG/browser, не hosted CI):

* В первом прогоне с UTF8 encoding, но `LC_CTYPE=C`: **2081 passed, 19 skipped, 3 failed in 479.05s**. Именно поиск по кириллице (browser, service и HTTP) не совпал с `lower()` в БД. Все три теста прошли в кластере `C.UTF-8` (**3 passed in 3.86s**), код поиска и assertions не менялись. Collation действующей БД надо обследовать, а не менять production locale на месте.
* Для контроля снят отдельный чистый checkout того же SHA и той же ветки. Полный прогон C.UTF-8: **2082 passed, 19 skipped, 2 errors in 461.81s**. Обе ошибки setup одного DEB fixture: наш wrapper распространил `umask 077` на `dpkg-deb`, который требует control dir ≥0755. Wrapper теперь держит кластер приватным, но возвращает дочернему процессу исходный umask. Повтор **на исходном SHA**: `tests/test_deb_package.py` **5 passed in 0.15s**. Это не выдаётся за полностью зелёный первый прогон.
* Контрольный **полный** локальный прогон на чистом `2edf3aa…` с окончательным wrapper, после исправления обеих проблем окружения: **2084 passed, 19 skipped in 442.05s**.
* 19 skips: 9 Rust parity без собранного бинарника в тестовой среде, 4 Android SDK, 3 MinGW, 1 transaction-mode pooler, 1 local agent-server, 1 live development task. Ни один skip не засчитан за поддержку платформы. `ruff check .` — без замечаний.

Локальный чистый Cargo home: `cargo test --locked --offline` не смог разрешить `serde_json`. Crates.io downloads не входят в разрешённые сетевые источники. Локальные исходники из tarball и замены зависимостей не принимались за locked build. Версия компилятора сама по себе ничего не доказывает.

**Найденный дефект oracle:** при последней исчерпанной попытке durable-задачи `reclaim_expired` открывал новый цикл без отзыва owner/token. На реальной БД воспроизведено позднее связывание conversation уже после reclaim, до нового claim. Исправлены поля в том же UPDATE, без DDL. Новый тест проверяет также durable stop, удержание ветки и выдачу нового токена. До изменения — `1 failed` (`'late-start' is None` ложно); после — **140 passed** в смежном наборе (26.52s, локально). Нельзя переносить дефект как контракт совместимости.

**Restore**: исходная проверка после `pg_restore` открывала store с автоматической миграцией. Теперь на восстановленной копии она запрещена. Проверяются полный ledger с SHA, project/repo, обе модели задач, token/зависимости, история чата, права и байты файла, параметр quality gate и две подписи/цепочка Ed25519 с отдельно сохранённым публичным ключом. Ещё один дефект: `sed` не разбирает libpq DSN с повторным dbname. `scripts/nexus.sh restore` теперь выбирает БД парсером libpq, сверяет фактический `current_database()` и, при наличии sidecar, checksum выбранного дампа **до** restore. Расширенная проверка скрипта и dump/restore: **13 passed in 2.17s**. Это восстановление fixture, **не production backup**. План для оператора, включая остановку writers и безопасный возврат: [BACKUP_AND_RESTORE](../../docs/BACKUP_AND_RESTORE.md).

План по каждой группе API, типу данных, поставщику и платформе, решения и порядок отката — [Phase 1 / ADR](COMPATIBILITY.md). Не переходить к rollout по зелёному unit-тесту. Портирование OpenHands, Axum/Tower, Rust MCP, Tauri и сборки/установки APK, DEB, NSIS EXE остаются самостоятельными gates. Внешние блокеры — разрешённые тестовые cloud accounts, ключ release signing вне репозитория, физическое устройство и целевые ОС. До этих проверок статуса «NEXUS 3.0 готов» нет.

## 6. Дополнительные проверки исходного SHA и ветки

Исходный checkout `2edf3aa…` проверялся тем же инструментом, что и **Python-путь** на `fa1dae8d387a5e0a9a66de8fd447dd743c042be7`. Это две отдельные серии, на свежих кластерах, без одновременного нагрузчика/pytest. Исполняемый Rust в этих локальных числах **не участвует**.

| Сценарий | Исходный Python | Python после исправлений | Что проверялось |
|---|---:|---:|---|
| 4 параллельных run × chain из 40 задач, 1000 посторонних pending задач | 160 / 0.5688 s; 0.2440 CPU-s | 160 / 0.5570 s; 0.2355 CPU-s | Общий limit 8; разные ветки; строго все 160 ID по одному разу, после своего предка. Синтетический `completed_no_changes`, **не** 160 проверенных работ. |
| Тёплый `EXPLAIN (ANALYZE, BUFFERS)` настоящего SELECT кандидата | 0.270 ms; shared hits 126 | 0.270 ms; shared hits 126 | Для одного grant с веткой и глобальным лимитом — **8** прикладных execute без BEGIN/COMMIT. Разбор запроса, время отдельных execute и полное дерево плана в JSON, а не предлагаемая наугад смена индекса. |
| Restart: новый процесс → сохранён зависимый start | 204.16–205.00 ms | 205.09–205.67 ms | По 3 SIGKILL штатного **Python** worker после привязки первой conversation. На каждый run ровно 2 старта, обе подписи task/commit и подпись решения ACCEPTED проверены публичным ключом. |
| Запрос → видна подписанная приёмка двух задач | 0.8201–0.8310 s | 0.7671–0.8293 s | Внешние планировщик/OpenHands/GitHub — локальные doubles. Экстраполяция из 6 результатов: 8720.9 / 8939.3 fixture tasks/h, **не** час наблюдения, не время реального агента и не выполнение проекта. |

Данные: [DAG/SQL до](evidence/queue_baseline.json) и [после](evidence/queue_candidate.json); [restart до](evidence/restart_baseline.json) и [после](evidence/restart_candidate.json). Небольшое расхождение времени одного прогона — не доказательство выигрыша. В локальном плане фильтрация по run использует bitmap/index scan; есть и последовательные сканы подветок. Нельзя переносить решение об индексах с 1200 fixture tasks на production без повторного плана и данных о кардинальности.

[Короткая нагрузка на исходном SHA](evidence/short_soak_baseline.json): 5 читателей, запрошены 2 минуты, последняя точка на 122.1 s, File Lab burst и один принудительный обрыв соединений PG. 74 985 успешных чтений; p50/p95/p99 успешных — 7.79/12.77/16.73 ms. **Одна ошибка запроса**, не скрыта. Снимки до/после обрыва: 61 618 → 74 969 чтений без перезапуска. RSS benchmark-процесса от 58.4 до 61.7 MiB; в нём находятся и клиенты, и растущий список latency, это не профиль аллокаций сервера. Ни OOM, ни зависания за окно не наблюдалось; многочасовая стабильность и RTO при падении всего хоста этим не доказаны.

```sh
# Из корня измеряемого checkout; HARNESS можно взять из ветки с инструментами.
HARNESS=/absolute/path/to/benchmarks/forensic_workload.py
WRAPPER=/absolute/path/to/tools/with_disposable_postgres.sh
$WRAPPER python "$HARNESS" queue --runs 4 --depth 40 --out /tmp/queue.json
$WRAPPER python "$HARNESS" restart --repeat 3 --out /tmp/restart.json
$WRAPPER python benchmarks/soak_test.py --minutes 2 --readers 5 --out /tmp/soak.json
```

**Целостность evidence.** У двух разных записей одного scope раньше мог оказаться один и тот же predecessor: подписи готовились параллельно, а последний hash читался до транзакции INSERT. Для v2 project и v1 unscoped из четырёх одновременно готовых подписей три связи на каждом стенде оказались нарушены (`2 failed in 0.38s` до исправления). Исправление `1b1ebafcd95c9421fae1a4004f5bfc184fa520d6`: одна блокировка на scope, чтение tail **после** ожидания, расчёт ссылки и INSERT с seq — в одной транзакции. Подписываемые байты и прежние записи не изменяются. Фикс с сервисными/лабораторными тестами: **28 passed in 0.99s**; в ветке совместно с restore, lease и handover: **95 passed in 19.34s**. Это предупреждает новые разветвления; не переписывает старые цепочки и не сертифицирует произвольную БД. Для проверки больших историй до cutover нужен полный проход каждой области: существующий `EvidenceService.index_integrity` читает ограниченную выборку (до 1000 записей).

**Восстановление credential:** на `170eaf96e23b56a89adca0a3c0c168eb3261b142` в drill добавлена запись OpenHands-аккаунта с одноразовым значением, привязкой к проекту и реальным шифрованием через штатный account service. Endpoint для заполнения — локальный double. После `pg_restore` совпадает digest ciphertext; чужой principal и другой ключ не дают использовать credential. С отдельно удержанным правильным ключом совпадает digest расшифрованного значения. Сеть для этой проверки не нужна. Restore + self-hosting: **13 passed in 2.60s**. Не доказательство действительности какой-либо внешней учётной записи.

**UI после исправлений:** на `fa1dae8…` ещё один отдельный `studio_snapshots.py --compare`: **9/9 одинаковы по всем RGB-пикселям** с исходными эталонами. [Перечень сравнения](evidence/studio_comparison.json). В diff нет `orchestration/studio/` и `migrations/`. После переноса API, для настоящего активного run и на физических клиентах проверку нужно выполнить заново.

## 7. Состав поставки и границы приёмки

| Этап | Изменённые файлы | Результат |
|---|---|---|
| 0 · baseline | `reports/nexus3/{catalog.sql,evidence/,studio/}`, `reports/nexus3/capture_wire.py`, `tools/{with_disposable_postgres.sh,studio_snapshots.py}`, `benchmarks/{api_bench,scheduler_bench,forensic_workload}.py` | Локальная воспроизводимая точка отсчёта, не спецификация производительности dedicated host. |
| Preservation / корректность oracle | `scripts/nexus.sh`, `docs/BACKUP_AND_RESTORE.md`, `orchestration/{nexus_lab_store,cloud_store,nexus_evidence}.py`, `tests/{test_backup_restore,test_self_hosting,test_lab_store,test_evidence_trust}.py` | Проверенный scratch restore, fence при durable rollover и конкурентная append-only запись evidence. Ни исправления production данных, ни автоматической ротации настоящих ключей. |
| 1 · проект | [`COMPATIBILITY.md`](COMPATIBILITY.md), `docs/RUST_MIGRATION.md` (уточнение границы старого отчёта) | Владение данными, контракты и порядок переключения заданы; проектирование не засчитывается за реализацию. Нет измеримого RAM/CPU эффекта от документа. |
| 2 · storage (часть) | `rust/{Cargo.toml,Cargo.lock,crates/nexus-storage/}`, `rust/crates/nexus-core/{src,tests}/scheduler.rs`, `orchestration/sql/*.sql`, `pyproject.toml`, `tests/test_rust_claim.py`, `benchmarks/compare_claims.py`, `.github/workflows/tests.yml` | [Транзакционный grant/reclaim на реальном PG и ограничения](STORAGE.md); нет handoff полномочий на исполнение. |
| 3–7 | **Нет релизных файлов этих фаз.** | Реальные Direct/App + смена двух аккаунтов, SelfQA/отчёты в новом worker, совместимый Axum/MCP, Tauri 2, install/upgrade на Debian 13/Windows/Android, прогон на целевом хосте и RC **NOT VERIFIED**. Нет release APK/DEB/EXE от 3.0. |

Точки истории ветки: база `2edf3aa70eefb0628fa88997a1d1c095b133e007`; baseline/restore `e4eb5f309892a818fbe461a08cdb551a7811566e`; ADR `3543860d126ada14f6fdd773e41c5c6d68a07fcc`; первый SQLx gate `a59c00c0ae17117b01b0b6b3da699b6bd0b06261`; целостность evidence `1b1ebafcd95c9421fae1a4004f5bfc184fa520d6`; recovery/сравнение `fa1dae8d387a5e0a9a66de8fd447dd743c042be7`; последняя ревизия ядра и тестов продукта `170eaf96e23b56a89adca0a3c0c168eb3261b142`. Последующий commit с отчётами, reference-данными и инструментом чтения wire contract не меняет этот код. Рабочая ветка и draft PR не являются разрешением на merge, deployment или миграцию существующей БД.

Отдельные блокеры нативной приёмки: в этом сеансе нет подключённого физического Android, release signing identity и Debian 13/Windows-системы с проведённой install/upgrade-проверкой. Основной CI использует **Ubuntu**, а не Windows runner. Отсутствие Tauri-кода/отдельных build jobs — ещё не выполненная работа фазы 5, **не** доказательство, что hosted runner недоступен. Ключ подписи нельзя давать непроверенному PR. Для фаз 3 и 6 нужны разрешённые тестовые внешние аккаунты/репозиторий и целевая машина; реальные пользовательские данные для измерений не брались.
