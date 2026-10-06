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

## Замена данных 13.10
`MockSqliteClinic` → `DbiApiClinic`: реализовать 7 методов протокола,
маппинг специальностей/дат, `PRE_EXISTING_IP.md` фиксирует границу IP.
