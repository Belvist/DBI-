# PERSISTENCE & IDENTITY (PR3)

## Identity

- `identity/providers.py`: `IdentityProvider` protocol; `DemoIdentityProvider`
  (`DBI_IDENTITY=demo`, token `demo`); `TokenFileIdentityProvider`
  (`DBI_IDENTITY=tokenfile` + `DBI_TOKENS_FILE` JSON).
- Fail-closed: нет токена → 401, чужой scope → 403. `patient_id` из body
  не может переопределить идентифицированного пациента.
- WS `/voice/ws` авторизован через subprotocol `dbi-voice, <bearer-token>`
  (токен НЕ в URL/query — не попадает в логи). `patient_id` из URL убран:
  пациент определяется только IdentityProvider. Bearer-токены обязаны быть
  subprotocol-safe (рекомендация: hex).

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

Снапшоты с TTL + per-patient локи с heartbeat-продлением lease
(атомарный Lua compare-and-expire — redis-py держит токен thread-local,
`reacquire()` из другого потока молча умирал). Без Redis — процессные
локи (только single-replica).

## Fail-safe исходы

- READ-упал → «ничего не записано и не изменено» (честно: мутаций не было).
- WRITE-упал → «не удалось подтвердить, могла сохраниться» + ретрай бьёт
  в ТОТ ЖЕ слот: взятый слот даёт SlotUnavailable → re-search, дубля нет.
- Production требует `DBI_PG_URL` всегда и Redis либо `DBI_SINGLE_REPLICA=1.
- Миграции сериализованы advisory lock; `/ready` проверяет и Redis.
