# PERSISTENCE & IDENTITY (PR3)

## Identity

- `identity/providers.py`: `IdentityProvider` protocol; `DemoIdentityProvider`
  (`DBI_IDENTITY=demo`, token `demo`); `TokenFileIdentityProvider`
  (`DBI_IDENTITY=tokenfile` + `DBI_TOKENS_FILE` JSON).
- Fail-closed: нет токена → 401, чужой scope → 403. `patient_id` из body
  не может переопределить идентифицированного пациента.
- WS `/voice/ws` (PR2) пока без auth: после мержа PR2 добавить `?token=`
  тем же провайдером. HTTP уже закрыт.

## Durability

| Слой | Дефолт | Durable-режим |
|---|---|---|
| Бронирования | SQLite `:memory:` | `DBI_DB_PATH=/data/dbi.sqlite` или `DBI_PG_URL` → `PostgresClinic` |
| Снапшоты диалога | `:memory:` | `DBI_SESSIONS_PATH=/data/sessions.sqlite` |
| Идемпотентность | UNIQUE навсегда | `purge_stale_idempotency_keys()` — только non-active старше 30 дней |

## Postgres

- `clinic_adapter/postgres.py`: тот же контракт, `SELECT ... FOR UPDATE`,
  `UniqueViolation` → вернуть запись победителя (кросс-процессные гонки).
- Даты — тот же TEXT-кодек, что SQLite: семантика фильтров идентична.
- `make up` → lite (SQLite). `make up-full` → Postgres (нужен `DBI_PG_PASSWORD`).
- CI гоняет PG-тесты на сервисе `postgres:16`; без `DBI_PG_URL` они скипаются.

## Redis

Отложен в PR4 (распределённые локи + resilience). Сессии переживают
рестарт уже сейчас через снапшоты; Redis — ускорение, а не условие выживания.
