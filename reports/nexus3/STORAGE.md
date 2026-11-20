# Phase 2 — что именно работает в Rust

**Решение: оставить единственного рабочего исполнителя на Python.** В ветке `arena/6f80793e-python-dispetcher` проверяется библиотека PostgreSQL, а не новый API или production worker. `claim_probe` — драйвер из `examples/` для испытаний. Его нельзя использовать как демон для обработки очереди; он намеренно не вызывает внешние сервисы. Старые API, миграции и файлы Studio не заменены.

## Граница реализации

| Операция | Реализация / граница |
|---|---|
| Выдать одну задачу | `rust/crates/nexus-storage/src/lib.rs::claim_task`. Одна SQLx-транзакция; UUID проекта и run, общий и веточный advisory locks **с теми же ключами, что у Python**, `FOR UPDATE SKIP LOCKED`, повторная проверка занятой ветки после ожидания. Проверяются часы PostgreSQL, цикл, зависимости и бюджет. Grant = UUID задачи и новый `lease_token`, только после commit. Ошибка БД — `Err`, отсутствие доступной работы — `Ok(None)`. |
| Не расходиться по правилу допуска | `orchestration/sql/{task_due,dependencies_verified,branch_free}.sql` загружаются Python один раз и входят в Rust через `include_str!`. Проверен собранный Python wheel: все три файла включены. `nexus-core::claimable` теперь учитывает собственную abandoned conversation; это по-прежнему **прогноз по одному run**, не способ выдать lease. |
| Отозвать потерянный старт | `nexus-storage::reclaim_expired` и `src/reclaim_expired.sql`: блокировка run перед задачами, ограниченная вызывающим batch, только истекшие и **не привязанные к conversation** задачи. Неисчерпанная попытка → pending; исчерпанная bounded → failed; исчерпанная durable → новый cycle. Маркер хранит предыдущие attempt/cycle. В каждом случае снимается owner и повышается fence. Возвращаются ID, не новые права на запись. Сохранённый `pending_start` со старым токеном не действует. Внешний stop и adoption остаются работой Python. |
| Что не перенесено | `pending_start` follow/adopt, запись выполнения и результата, ветвление retry/failover, подтверждение внешнего stop, SelfQA и финальный отчёт **не выполняются Rust-библиотекой**. У Rust нет самостоятельного poll/notify worker, Axum API, нового MCP-транспорта, публичного auth boundary или migration runner. Лимит batch не доказывает backpressure всей системы. |

Не обновлялись таблицы или записанные миграции. Лимиты 0/8 в тестах — свойства конкретных сценариев, не настройки сервера. Cargo.lock включает обычные registry checksums. Сборка проверяется на stable в hosted CI; готовые Rust-источники зависимостей из случайных mirrors или самодельные stubs не использовались.

## Проверки и измерения

* На `a59c00c0ae17117b01b0b6b3da699b6bd0b06261`: [CI](https://github.com/Pavel1778/python-dispetcher/actions/runs/44783858567), locked release build, Rust tests, 9 прежних parity + 4 новых PG-сценария — **13 passed in 1.33s**. Ровно 12 уникальных задач и 12 инкрементов бюджета при одновременной работе шести Python/SQLx-драйверов; foreign project, branch lock между runs, global capacity, requested stop. Ошибка записи fence откатывает предшествующее изменение run. Полный CI Python job: **2100 passed, 10 skipped, 1 warning in 546.21s**. Отдельный локальный полный прогон той же ревизии: **2087 passed, 23 skipped in 442.08s** (нет собранных Rust-драйверов в локальной suite).
* На `fa1dae8d387a5e0a9a66de8fd447dd743c042be7`: [SQLx job](https://github.com/Pavel1778/python-dispetcher/actions/runs/44785548428/job/136459321657), `rustc 1.100.0`, PG 17.12, **18 passed in 1.96s**. Для обеих политик и исчерпанной/неисчерпанной попытки сравниваются **сохранённые поля** после Python и Rust, поздний bind и stop. Проверены лимит размера sweep, повторный пустой sweep, изоляция по проекту, сохранение живой lease и уже работающей conversation. Это не тест живого облака.
* [Сырые данные первой серии](evidence/claim_comparison_ci.json). Медианы **четырёх отдельных прогонов** (не объединённые перцентили):

| Драйвер | committed grants/s | p50 / p95 / p99 одной серии, мс (медианы) | peak RSS драйвера | CPU U+S / 200 задач |
|---|---:|---:|---:|---:|
| Python/psycopg | 406.35 | 2.00 / 3.70 / 16.40 | 42.64 MiB | 0.25 s |
| Rust/SQLx | 307.90 | 2.23 / 4.90 / 24.85 | 2.97 MiB | 0.05 s |

Один раннер/кластер, восемь отдельных **физических клонов одной fixture БД** (вплоть до UUID), порядок PR/RP чередуется. В каждом 200 committed grants проверены SQL после выхода драйвера. Один прогретый connection, кеши prepared statements выключены, пустой repo и нет лимита active; startup/teardown вне времени операции, но включены в `/usr/bin/time` CPU и peak RSS. Rust выдаёт только grant, Python материализует полную строку. Ни PG, ни реальные API/worker, ни файлы/внешние вызовы в RSS не входят. Разброс существенный: в одной Python серии p95 44.19 мс. Первая серия **не подтвердила** ускорение этой транзакции от переноса, хотя у драйвера меньше CPU и RSS. Не применять коэффициент к продукту.

* Следующее изменение `170eaf96e23b56a89adca0a3c0c168eb3261b142`: выставляем READ COMMITTED прямо в `BEGIN`, а не отдельным запросом. Логика замков и атомарности та же. Добавлены реальные `SIGKILL` клиента, ожидающего branch lock (ожидание подтверждает `pg_blocking_pids`), пропуск занятой task row и случай старого bounded run с NULL deadline. **Уменьшение числа команд не равно измеренному ускорению.** [CI последнего кода](https://github.com/Pavel1778/python-dispetcher/actions/runs/44786382445/job/136462233575): release build и Rust unit tests под `--locked`, затем **22 passed in 2.22s**, без пропусков. Драйвер был действительно убит до commit и не оставил израсходованного бюджета или lease.
* [Четыре новые парные серии из CI](evidence/claim_comparison_final_ci.json) на `170eaf9…`: медианы Python **500.35 grants/s**, p50/p95/p99 **1.911/2.107/4.914 ms**, peak **42.62 MiB**, U+S **0.240 s**; Rust **488.0 grants/s**, **1.986/2.145/3.516 ms**, **2.90 MiB**, **0.045 s**. В этом опыте разница для драйвера: **−39.72 MiB**, **−0.195 CPU-s на процесс с 200 задачами**, но не больший throughput всей системы. Разные независимые hosted runner для двух SHA — не парный before/after изменения `BEGIN`; у Python на них тоже разные результаты. Не менять production-путь по микробенчмарку.

### Воспроизведение

На Linux: Rust stable, обычный registry access или проверенный cache, Python с dev/test deps, GNU time и Linux PG tools. Нужен полный checkout, поскольку SQL предикаты берутся из того же дерева, что и Python.

```sh
(cd rust && cargo build --workspace --release --locked && \
    cargo build -p nexus-storage --release --example claim_probe --locked && \
    cargo test --workspace --locked)
export NEXUS_CLAIM_PROBE="$PWD/rust/target/release/examples/claim_probe"
export NEXUS_RUST_BIN="$PWD/rust/target/release/nexus-rs"
tools/with_disposable_postgres.sh python -m pytest -q tests/test_rust_parity.py tests/test_rust_claim.py
tools/with_disposable_postgres.sh python benchmarks/compare_claims.py --tasks 200 --rounds 4 --out /tmp/grants.json
```

### До допуска worker

1. Подтверждать совместимость **полного** миграционного ledger и структуры БД, а не только факт подключения. Не запускать параллельных DDL writers.
2. Проверить принятую внешнюю заявку на всех границах сбоя: до записи квитанции, после записи до bind, после bind и при запоздалом stop. Перенести ровно-однократные изменения в execution history/notifications и сверить Python↔Rust по строкам, не только по HTTP-ответу.
3. Отдельная проверка полного scheduler tick с настоящей проверкой результата/репозитория, late responses, bounded poll/notify и graceful shutdown. Потом сравнение CPU, PG, RSS, p99 и работы/час всего процесса под одними и теми же нагрузками на 4 CPU / 8 GB. До этого **нет допуска** к замене штатного worker или к выбору окончательных лимитов.
