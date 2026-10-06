# PRE-EXISTING IP / CHRONOLOGY

> Это техническая фиксация хронологии, а не юридическое заключение. Юридический
> эффект определяется правилами ProductHack и подписанными участником документами.

## DBI repository

Repository: `https://github.com/Belvist/DBI-`.

Базовый DBI-каркас начал создаваться **до старта хакатона 13.10.2026**.
На 06.10.2026 зафиксирован commit:

```
49abf5d2f4c188c4169872eadbb84a2a3d73f6b2
feat: DBI clinic assistant core — atomic booking, dialogue engine, 14-scenario eval
```

Этот commit и его содержимое существуют до конкурсного периода. Не следует
описывать их в документации как автоматически «отчуждаемые»: этот файл только
фиксирует факт и дату существования кода. Изменения, созданные в конкурсный
период, должны быть отделимы по истории Git.

Перед 13.10.2026 рекомендуется дополнительно поставить неизменяемый tag:

```
pre-hackathon-2026-10-12
```

и сохранить архив/commit SHA вне конкурсного репозитория.

## IMVERA / voiseup

Отдельная pre-existing технология, существовавшая до DBI-проекта:

- VAD / endpointing / turn-FSM patterns;
- generation cancellation / barge-in pattern;
- STT/TTS worker contracts;
- WS gateway pattern;
- Planner → State → Renderer → Validator separation;
- trace / idempotency patterns;
- PCM16 20 ms audio contract.

Зафиксированный SHA voiseup на момент подготовки DBI:

```
ad5aad75037ac7b2df048b8cd190cafbd5c754bc
```

Рекомендуемый tag до старта:

```
pre-hackathon-2026-10-12
```

## Boundary

DBI не должен импортировать или копировать закрытые файлы voiseup целиком.
Допустимо заново реализовывать общие инженерные паттерны. `voice/runtime.py`
должен оставаться самостоятельной реализацией.

После 13.10 любые новые конкурсные изменения следует вести отдельными commit'ами,
чтобы граница между pre-existing кодом и конкурсным результатом была проверяема.
