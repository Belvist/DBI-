# ARCHITECTURE — DBI Assistant

```text
VOICE/TEXT → NLU (deterministic + LLM proposer abstain-by-default)
  → DialogueEngine (ELICIT→SEARCH→PROPOSE→CONFIRM→COMMIT, код решает)
  → ClinicAdapter READ (find_*) / WRITE (create/reschedule, atomic+idempotent)
  → Validator (никаких слотов вне allow-list)
  → Renderer (русские фразы только из фактов БД)
  → TTS / dashboard
```

## Invariants
- `canonical_assistant_text` содержит только факты из adapter-ответа.
- WRITE без `idempotency_key` запрещён (проверяется pydantic min_length).
- COMMIT = re-check + `BEGIN IMMEDIATE` + dedup + insert (см. `mock_sqlite.py`).
- LLM не меняет состояние; `fuse()` отдаёт приоритет детерминированному парсеру.
- READ/WRITE разделены в `clinic_adapter/base.py` (Protocol).

## Voice (тонкий слой)
`voice/runtime.py`: turn ownership + `Generation.cancel()` на barge-in.
Идеи из IMVERA `call_actor` (отмена генерации, trace), код новый и маленький.
STT/TTS — интерфейсы; дефолт текст, чтобы DoD не зависел от GPU.
WS `/voice/ws`: auth через subprotocol `dbi-voice, <token>`, `patient_id`
в URL нет; блокирующий turn едет в `anyio.to_thread`, event loop не ждёт БД.

## Production runtime (PR4)
- `config/settings.py`: `DBI_ENV=production` отказывается стартовать с
  demo-identity и всегда запрещает seed.
- Postgres: pool (`DBI_PG_POOL_MAX`), схема через `migrations/*.sql`
  (`clinic_adapter/migrate.py`, таблица `schema_migrations`).
- Сессии: Redis-снапшоты (`DBI_REDIS_URL`) + per-patient локи
  (Redis, fallback — процессные) + TTL снапшотов + bounded in-memory cache.
- Resilience: `ResilientAdapter` (retry транзиентных + circuit breaker);
  бизнес-ошибки (`SlotUnavailable`) не ретраятся и не триггерят breaker;
  падение backend → `temporarily_unavailable()`, ноль выдуманных фактов.
- Наблюдаемость: `/ready` (503 при мёртвом backend), `/metrics`
  (Prometheus-текст: turns, bookings, races, adapter_errors, breaker_opens,
  auth_failures, fallbacks).

## Замена данных 13.10
`MockSqliteClinic` → `DbiApiClinic`: реализовать 7 методов протокола,
маппинг специальностей/дат, `PRE_EXISTING_IP.md` фиксирует границу IP.
