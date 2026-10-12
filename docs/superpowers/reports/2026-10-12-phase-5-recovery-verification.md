# Phase 5 — Recovery Verification

Дата: 2026-10-12 (Europe/Moscow)

## Проверенное состояние

После переустановки Windows восстановлена точка продолжения проекта: Phase 1–5
реализованы; Phase 6 product implementation ещё не начата. Финальная функциональная
приёмка Phase 5 находится в `cfdf872` от 2026-10-04. Проверена последующая миграция
на Python 3.14 (`c735b85`) вместе с исправленным README (`d487981`).

Незакоммиченных изменений до начала восстановления не было. К этому отчёту
product code, dependency lockfile, схемы и данные пользовательских баз не изменены.

## Среда и изоляция

- Host: Windows, Python 3.14.8, существующее project `.venv`.
- Docker Engine 29.8.2; Docker Compose 5.6.0; Docker Desktop запущен.
- Docker CLI найден в `%LOCALAPPDATA%/Programs/DockerDesktop/resources/bin`;
  в PATH текущей сессии изначально отсутствовал.
- API image заново собран из текущего Dockerfile с frozen lockfile.
- PostgreSQL `17.11-bookworm`, ClickHouse `26.3.25.2-jammy`, Redis `8.2.9-bookworm`.
- Compose project: `nba-phase5-recovery-a2b31ca2`, отдельные свежие named volumes.
- Свободные loopback ports: API 59326, PostgreSQL 59327, ClickHouse 59328, Redis 59329.

Host-side TEST_* URLs и Compose ports указывали на одни и те же изолированные сервисы.
Live-canary opt-ins были отключены. NBA.com не вызывался в этом verification workflow.

## Результаты

| Проверка | Результат |
|---|---|
| `docker compose up -d --build --wait` | Все четыре сервиса healthy |
| `/health/ready` | ready; PostgreSQL, ClickHouse, Redis up |
| `alembic upgrade head`, два запуска в API container | Оба успешны; head `0005_feature_engineering` |
| `python -m scripts.migrate_clickhouse`, два запуска | Оба успешны; все четыре schemas applied |
| `python -m scripts.build_dataset --help` в API container | Успешно |
| `id -u` в API container | 999; непривилегированный runtime |
| Unit/API перед восстановлением infrastructure | 1672 passed, 38.94 s |
| Dedicated ClickHouse migration suite с destructive opt-in | 22 passed, 23.56 s |
| Полный non-live pytest после migrations | 2149 passed, 18 skipped, 847.31 s |
| Ruff check | All checks passed |
| Ruff format check | Все файлы форматированы |
| Strict mypy `src scripts tests` | No issues found in 213 source files |

В полном прогоне 18 skips состоят из трёх opt-in live-canary modules и 15 destructive
migration cases. Эти 15 cases выполнены в отдельной suite на свежих stores; её число
22 включает также non-destructive cases, поэтому складывать 2149 и 22 как число
уникальных тестов нельзя.

Полный прогон включает реальные integration и E2E tests Phase 1–5, в том числе
`tests/e2e/test_feature_engineering.py`: stored ingestion → possessions → lineups →
feature dataset, exact two-table snapshot readback, causal exclusions и invalid recovery.

## Команды воспроизведения

Использовать новый isolated Compose project и свободные порты; TEST_* URLs должны
совпадать с соответствующими published ports. Не применять destructive opt-in к
существующим пользовательским базам.

```powershell
docker compose up -d --build --wait
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/alembic upgrade head
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse
docker compose exec -T api /app/.venv/bin/python -m scripts.migrate_clickhouse

# Только на только что созданных пустых isolated stores:
$env:TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS = '1'
.\.venv\Scripts\python.exe -m pytest tests/integration/test_clickhouse_migrations.py -q -m integration -rs
Remove-Item Env:TEST_DESTRUCTIVE_CLICKHOUSE_MIGRATIONS

# Live-canary opt-ins остаются отключёнными:
.\.venv\Scripts\python.exe -m pytest -q -m 'not live' --tb=short -rs
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe src scripts tests
git diff --check
```

Cleanup target проверен по exact resolved Compose project name. Временные
containers/network/volumes удалены только для этого project. Повторные `docker ps -a`,
`docker volume ls` и `docker network ls` с project label подтвердили их отсутствие.

## Практические ограничения и продолжение

Эта проверка подтверждает работоспособность кода и pipeline на fixtures и реальных
stores. Она не подтверждает доступность текущих NBA provider endpoints, качество
данных всех сезонов, наличие старых исторических данных после переустановки или
точность будущих прогнозов.

Исторические планы используют неотмеченные checkboxes и старую версию Python;
они остаются планами, а не журналом актуального runtime. README и header Phase 5 spec
обновлены, а этот отчёт служит явным verification checkpoint.

Следующий milestone — Phase 6 historical/live/shrinkage baselines. Пользователь
согласовал pure-калькулятор, offline CLI и JSONL-экспорт. Written spec находится в
`docs/superpowers/specs/2026-10-12-phase-6-baselines-design.md`; дальнейший implementation
plan создаётся после review этого документа.

Существующий default join-version mismatch между `FeatureVersions()` и settings
не исправлялся: новая фаза обязана явно передавать все configured feature versions
при чтении published snapshots, как зафиксировано в draft spec.
