# NEXUS 3.0 · Phase 1: контракт перехода

Исходный код контракта: `2edf3aa70eefb0628fa88997a1d1c095b133e007`. Это решения для миграции, а не описание уже работающего сервера 3.0. Проверки исходной системы — в [Phase 0](README.md).

### ADR 1. Два процесса, один владелец операции

Оставить `nexus-domain` (состояния, нормализация плана, hash, retry) без I/O; `nexus-core` — решения над доменом; выделить `nexus-storage` под транзакции SQLx. `nexus-cli` сейчас **оракул**, а не замена Python HTTP CLI. Границы будущих API и worker — библиотеки прикладных операций в одном workspace, а не отдельные сетевые сервисы. Не вызывать Rust через subprocess на каждую заявку. Отдельный процесс для тестового драйвера допустим **только** в parity-тесте.

Порядок: read-only сравнение → две отдельные базы с одними сценариями → конкурирующие реализации в **тестовой** БД → выключить прежнего владельца операции → включить нового только после acceptance. Сравнить не только возвращаемый JSON, но и committed rows, историю, счётчики и отсутствие повторных внешних эффектов. Никакой shadow-записи в реальный OpenHands, GitHub или production. Сохранять transport/HTTP CLI, пока их замена не проходит те же тесты.

### ADR 2. Схема и идентичность

| Сегодня | Переходный контракт | Проверка |
|---|---|---|
| `workflows`, `tasks`, `conversations`, `task_events`, `sr_tool_calls` | Старый Sr/Jr и локальный SQLite-адаптер не являются `nexus_runs`. Не объединять очереди или пространства ID | `test_dispatcher`, `test_sr_runtime`, `test_cloud_dispatcher`, `test_run_binding` |
| `nexus_projects`, members, project repositories, repository accounts/allowlists | UUID и principal на диске и в JSON неизменны. Доступ к чужому ID — тот же 404; ветку и аккаунт выбирать в контексте проекта | `test_project_isolation_routes`, `test_project_repositories`, `test_dispatch_authorization` |
| `nexus_goals`, `nexus_runs`, `nexus_run_tasks` | Уникальность (run, task_key); зависимости в JSONB. Компактные индексы `usize` и adjacency list — только внутри одного проверенного DAG; не менять внешние UUID. Не заменять SQL in-memory снимком | `test_lab_store`, `test_rust_parity`, `test_cancel_lock_order` |
| `nexus_task_executions`, stops, pending starts, cleanup, run reports | Не терять account/conversation/branch, повторно привязывать сохранённый start; отчёт на run идемпотентен. Pending stop держит ветку **между** проектами/прогонами | `test_start_handover`, `test_branch_write_lock`, `test_run_reports` |
| Chat, attachments, `nexus_files`, workspaces | `name/content` (opaque download) и `filename/data` (File Lab JSON preview) сосуществуют. Оба формата и авторизацию оставить. Никакого доступа к произвольному пути ОС | `test_chat_repository`, `test_lab_files`, `test_file_lab` |
| Accounts, sessions, notifications, audit, project gate, `nexus_evidence` | Не перекодировать encrypted values, не пересоздавать ключи, не переподписывать evidence. Сохранить canonical JSON, `format_version`, `seq`, prev/entry hash, signature и область действия. Непроверенное не становится verified | `test_evidence_trust`, `test_lab_evidence`, `test_quality_gate_policy`, `test_backup_restore` |

**Не использовать стандартное обнаружение `sqlx::migrate!` для старых файлов.** Есть пропуски 0012/0013 и пары 0025, 0037, 0038. `schema_migrations(filename,checksum,applied_at)` сравнивается по полному имени и SHA-256 UTF-8 SQL с нормализацией переводов строки; есть одна явно зарегистрированная коррекция 0037. Переименование/перенумерация неприменимы. Пока runner один — Python; будущий Rust runner обязан прочесть старый ledger, отказать при неизвестном расхождении, пройти холодную/повторную/прерванную миграции и восстановление. Не давать API и worker одновременно применять DDL.

### ADR 3. Durable claim не равен прогнозу `claimable`

Под READ COMMITTED в одной транзакции: заблокировать run (state, cycle budget, resume_at, deadline), при лимите — взять **тот же** advisory lock глобальной вместимости, затем взять ровно одну due-задачу `FOR UPDATE SKIP LOCKED`. Проверять подтверждённые зависимости в этом run; in-flight writer, abandoned conversation (включая **собственную**) и requested stop искать для repo/branch **во всех** run. При наличии repo — тот же ключ branch advisory lock и повторное чтение после ожидания. Пустой repo не является общей веткой. Инкременты iteration/total_iterations и attempt/lease_token, очистка pending_start и обновление owner/expiry — один commit. Потерянный ответ на commit не основание повторить внешний dispatch.

Завершение/renew/revoke — сравнение токена в самом UPDATE. Просроченная, но ещё не отозванная lease и уже отозванная lease — разные состояния. Не запускать вторую conversation для уже связанной задачи. LISTEN/NOTIFY в будущем лишь будит; worker обязательно повторяет SQL poll после таймера/разрыва соединения. Общий лимит работ/лимиты аккаунта — не число ядер. Отдельная ограниченная очередь CPU-задач (архив, SelfQA), дедлайны и отказ при перегрузке. Числа выбрать сравнением p99, RSS, очереди pool и RPS на целевом хосте. Не добавлять Redis/другой брокер или lock-free коллекции без измерений.

### ADR 4. HTTP, MCP и клиенты

* Сначала воспроизвести `web_auth.py`/`web_server.py`: redirect `/`→`/login`, публичные `/healthz` и `/readyz` (503 при отсутствии БД), Strict session cookie, bearer principal, CSRF на запись, давность/отзыв, 404/401/403/409, content-type, CSP, gzip/cache static, бинарные загрузки. Проверять status, заголовки и тело, не только happy-path JSON. Shadow-read не должен создавать запись/фоновую задачу. Python пока отвечает за весь HTTP; маршрутизация на Axum только по прошедшим parity группам.
* `orchestration/mcp_server.py`: newline JSON-RPC stdio и `POST /mcp` на той же модели principal/role/scope. Сохранить initialize/tools/list/call, номер протокола, схемы, error codes и ответ на notifications; не давать удалённому клиенту привилегии локального owner. Для каждого tool — тот же прикладной код, что у HTTP/CLI. Контракт: `tests/test_cli_mcp.py`, `test_cli_client.py`, `test_route_auth.py`.
* Direct/App, pool, retry/429, timeout и stop — разные транспортные контракты. Подтверждение принятого start хранится до получения conversation id. Сначала оба stand-in HTTP стенда; затем разрешённые тестовые аккаунты, отказ первого, привязка ко второму, результат и очистка только собственных ресурсов. SelfQA: отдельный ограниченный исполнитель без production credentials, проверка точного commit; не смешивать результат агента с независимым verdict.
* Одна серверная Studio, без замены JS/CSS. Новые SSE-события — добавка с cursor/reconnect и fallback к существующему polling. Для Tauri 2 — согласованный HTTPS origin, browser session, никакого bearer в localStorage; удалённой странице **по умолчанию никакого IPC**. Разрешать конкретную ОС-операцию только после проверки origin и выбора пользователем. Не кэшировать приватные ответы в service worker. Девять исходных PNG — в `studio/`; сверка на том же Chromium/шрифтах, затем реальное устройство.

### Проверяемый снимок wire protocol

[Ответы исходного сервера](evidence/wire_contract.json) получены по loopback HTTP и через отдельный stdio-процесс MCP, не из описания API. Новый кластер на `170eaf96e23b56a89adca0a3c0c168eb3261b142` дал те же данные: 9 HTTP cases и 5 MCP-сообщений. В файле — status, разрешённый набор response headers, JSON или размер/хэш байтов; ни cookies, ни значения bearer не сохраняются. Для неавторизованного `/` — **303** и Location, не 200 с новым dashboard; API без auth — 401, чужой проект — 404, некорректное JSON-тело `/mcp` — 400. Для четырёх запросов с id ответы stdio и JSON-RPC/HTTP совпадают, включая схемы/полномочия всех 19 tools и ошибку −32004; notification — пустой HTTP 202 и **нет** строки в stdout. Это выбранная регрессия, не исчерпывающая OpenAPI-спецификация и не утверждение об SSE/batch support.

```sh
# Из корня измеряемого checkout, в новую output file.
tools/with_disposable_postgres.sh python reports/nexus3/capture_wire.py /tmp/wire.json \
    --compare reports/nexus3/evidence/wire_contract.json
```

### Порядок допуска

| Фаза | Измеримый результат до переключения | Откат |
|---|---|---|
| 2 — Storage/core | PostgreSQL differential: Python↔Rust claim, same/cross-run locks, global limit, lease expiry, stale completion, kill before/after commit; затем adoption, durable/bounded retries и отчёт. Нет потерянных ID, двойных стартов или бюджетов | Остановить кандидата, не удалять rows; Python подхватывает только по установленным lease/recovery правилам |
| 3 — I/O | Direct/App и GitHub подтверждены **отдельно** от fakes, два разрешённых аккаунта, File Lab и scope, реальные SelfQA/verdict, целостный отчёт | Прежний транспорт/адаптер; не повторять уже принятый запрос |
| 4 — API | Negative auth/isolation, API/CLI/MCP parity, Chromium E2E и visual diff; webhook/stream disconnection без потери событий | Маршрут обратно на Python; тот же session store |
| 5 — Native | Linux→DEB на Debian 13, Windows→NSIS/WebView2, Android SDK/NDK→release APK с внешним ключом; install, upgrade, login, file/notification/session tests, реальный Android. Не раздавать ключи PR jobs | Предыдущий проверенный клиент и совместимый API |
| 6 — Perf/security | Сравнить одинаковые данные/нагрузки в idle, moderate, saturation на 4 CPU/8 GB; профили SQL/allocations, p99, backpressure, длительный soak и crash recovery | Оставить измеренно лучший совместимый путь |
| 7 — RC | Подписанные артефакты и SHA, проверенные systemd API/worker + Caddy + PG, graceful stop, мониторинг, проверенный restore и инструкция переключения; разрешение владельца отдельно | Без destructive rollback; сверить запись после cutover |

Phase 1 меняет контрактные документы и тестовые условия допуска, не ресурсы backend. Ускорение/RAM delta от этого дизайна не заявляются.
