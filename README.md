# DBI Assistant — AI-администратор клиники (голосовой, без оператора)

Голос → понимание → Clinic Gateway → действие в БД → голосовой ответ.
LLM только предлагает смысл (`NLUResult`), workflow решает код.
WRITE — атомарно + идемпотентно; «вы записаны» только после `booking_created`.

## Быстрый старт (DoD до 13.10)

```bash
make up      # API на :8080
make test    # unit + eval 14 сценариев
make demo    # живой BOOK → STATUS → RESCHEDULE в терминале
```

Текстовое демо: http://127.0.0.1:8080/ → `/dialogue/turn`.
Голос — второй слой (`voice/runtime.py`: barge-in, generation cancel).

## Структура

См. `docs/PRODUCT_BRIEF.md`, `docs/ARCHITECTURE.md`, `docs/PRE_EXISTING_IP.md`.
