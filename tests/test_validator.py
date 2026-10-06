from datetime import datetime

from api.validator import validate


def test_exact_allowed_datetime_passes():
    allowed = [datetime(2026, 10, 20, 18, 0)]
    ok, text = validate(
        "Вы выбрали 20 октября в 18:00.",
        allowed,
        [],
    )
    assert ok
    assert "18:00" in text


def test_cross_combined_day_and_time_is_blocked():
    allowed = [
        datetime(2026, 10, 20, 18, 0),
        datetime(2026, 10, 21, 19, 15),
    ]
    ok, text = validate(
        "Вы выбрали 20 октября в 19:15.",
        allowed,
        [],
    )
    assert not ok
    assert "Уточню данные" in text


def test_unknown_standalone_time_is_blocked():
    ok, _ = validate(
        "Есть вариант в 17:45.",
        [datetime(2026, 10, 20, 18, 0)],
        [],
    )
    assert not ok


def test_booking_claim_requires_real_booking_id_allowlist():
    ok, _ = validate(
        "Готово! Вы записаны к врачу на 20 октября в 18:00.",
        [datetime(2026, 10, 20, 18, 0)],
        [],
    )
    assert not ok


def test_grounded_booking_claim_passes():
    ok, text = validate(
        "Готово! Вы записаны на 20 октября в 18:00. Номер записи AABC123.",
        [datetime(2026, 10, 20, 18, 0)],
        ["AABC123"],
    )
    assert ok
    assert "AABC123" in text
