import pytest

from ttmon.ai.prices import amounts_in, format_prices, normalize, parse_amount
from ttmon.ai.schema import Price


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("12500", 12500),
        ("12 500", 12500),
        ("12\u00a0500", 12500),
        ("12 500", 12500),
        ("3.500 тг", 3500),
        ("11.000тг", 11000),
        ("12,500", 12500),
        ("1.000.000", 1_000_000),
        ("12.5к", 12500),
        ("8.9к", 8900),
        ("8,9 к", 8900),
        ("кофта 7к", 7000),
        ("12,5 мың", 12500),
        ("5 мыңға", 5000),
        ("5мыңнан", 5000),
        ("5 тысяч", 5000),
        ("12 тысяч тенге", 12000),
        ("1.2 млн", 1_200_000),
        ("8 900 тг", 8900),
        ("8900₸", 8900),
        ("₸ 4 990", 4990),
        ("4990,00", 4990),
        ("3500тг", 3500),
        ("15000 тенге", 15000),
        ("Пижама 5шт 12500", 12500),
        ("2 шт по 3500", 3500),
        ("45 000 тенге за 2 штуки", 45000),
        ("25000тг/5шт", 25000),
        ("5000 x2", 5000),
        ("5000₸ -20%", 5000),
        ("Бағасы: 700 - 9 000", 9000),
        ("нет цены", None),
        ("", None),
    ],
)
def test_parse_amount(text: str, amount: int | None) -> None:
    assert parse_amount(text) == amount


@pytest.mark.parametrize(
    ("text", "amounts"),
    [
        ("ТРИКОТАЖНЫЕ ДВОЙКИ 9000,11.000тг", [9000, 11000]),
        ("700 - 9 000", [700, 9000]),
        ("5000,6000", [5000, 6000]),
    ],
)
def test_several_prices(text: str, amounts: list[int]) -> None:
    assert amounts_in(text) == amounts


@pytest.mark.parametrize(("raw", "amount", "fixed"), [("8.9к", 89, 8900), ("11.000тг", 11, 11000)])
def test_normalize_fixes_lost_multiplier(raw: str, amount: int, fixed: int) -> None:
    price = Price(item="Куртка", raw=raw, amount=amount, source="screen")
    assert normalize(price).amount == fixed


@pytest.mark.parametrize(
    ("raw", "amount"),
    [
        (None, 3500),
        ("цена в директ", 3500),
        ("3500тг", 3500),
        ("700 - 9 000", 700),  # диапазон: «от» не превращается в максимум
        ("9000,11.000тг", 11000),
        ("5000тысяч", 5000),  # шумный raw из реального ответа: модели верим
        ("12 500", 12000),  # AI ≥ 1000 — не трогаем, даже если raw другой
    ],
)
def test_normalize_keeps_amount(raw: str | None, amount: int) -> None:
    price = Price(raw=raw, amount=amount, source="screen")
    assert normalize(price).amount == amount


def test_format_prices() -> None:
    prices = [
        Price(item="Пижама 5 шт", amount=12500, source="screen"),
        Price(item="Трусы 25 шт", amount=5500, source="screen"),
    ]
    expected = "Пижама 5 шт: 12\u00a0500\u00a0₸; Трусы 25 шт: 5\u00a0500\u00a0₸"
    assert format_prices(prices, "не указана") == expected


def test_format_prices_without_item_and_currency() -> None:
    prices = [
        Price(amount=1500, source="voice"),
        Price(item="Кроссовки", amount=1200, currency="USD", source="chat"),
    ]
    expected = "1\u00a0500\u00a0₸; Кроссовки: 1\u00a0200\u00a0USD"
    assert format_prices(prices, "не указана") == expected


def test_format_prices_empty() -> None:
    assert format_prices([], "не указана") == "не указана"
