# PRE-EXISTING IP (фиксация до 13.10.2026)

Конкурсный проект: `/DBI` (этот репозиторий, https://github.com/Belvist/DBI-).
Всё в нём создано под хакатон и подпадает под условия отчуждения 13–31.10.

Pre-existing (чужой IP, НЕ отчуждается, только идеи/паттерны, без копипаста):
- IMVERA voiseup (закрытый репо, читался read-only из /Users/earflow/Downloads/voiseup):
  `voice_runtime` идеи (VAD/endpointing/turn-FSM/generation cancel/barge-in/trace),
  STT/TTS worker-контракты, WS-gateway паттерн, Planner→State→Renderer→Validator
  разделение, QuestionLedger-идея, аудио-контракт PCM16 20ms.
- SHA/tag voiseup на момент старта DBI зафиксировать здесь перед первым коммитом:

```
voiseup SHA: ad5aad75037ac7b2df048b8cd190cafbd5c754bc
voiseup tag: pre-hackathon-2026-10-12 (поставить в voiseup до 13.10: git tag pre-hackathon-2026-10-12 <SHA>)
дата фиксации: 2026-10-06
```

Граница: из voiseup не скопирован ни один файл целиком; `voice/runtime.py`
написан заново (~60 строк) и не импортирует voiseup.
