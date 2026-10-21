"""Нормализация цен поверх ответа AI и формат для отчёта."""

import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

from ttmon.ai.schema import Price

# обычный, неразрывный (U+00A0) и узкий неразрывный (U+202F) пробелы
_SPACES = " \u00a0\u202f"
# буквы, после которых «к» — начало слова, а не «тысяч»: русские + казахские
_LETTER = "а-яёәіңғүұқөһa-z"
# «12 500», «3.500», «11.000», «1,500,000»: разделитель тысяч → ровно три цифры в группе
_GROUPED = rf"\d{{1,3}}(?:[{_SPACES}.,]\d{{3}})+"
# дробная часть — 1–2 цифры («8.9к», «4990,00»); «9000,11.000» — это две цены через запятую
_DECIMAL = r"\d+(?:[.,]\d{1,2}(?![\d.,]\d))?"
_MULT = rf"к(?![{_LETTER}])|k(?![{_LETTER}])|тыс\w*|мың\w*|мын\w*|млн\w*"
_PRICE_RE = re.compile(
    rf"(?<!\d)(?<!\d\.)(?:(?P<grouped>{_GROUPED})|(?P<num>{_DECIMAL}))(?![\d])"
    rf"(?:[{_SPACES}]*(?P<mult>{_MULT}))?",
    re.IGNORECASE,
)
# не цены: количество в наборе («5шт», «2 штуки», «x2»), проценты скидки
_NOT_PRICE_RE = re.compile(
    rf"\d+[{_SPACES}]*(?:шт\w*|pcs|дана\w*)\.?|[xх×][{_SPACES}]*\d+|\d+[{_SPACES}]*%",
    re.IGNORECASE,
)
_MULTIPLIERS = (("млн", 1_000_000), ("мың", 1_000), ("мын", 1_000), ("тыс", 1_000))


def _multiplier(word: str) -> int:
    word = word.lower()
    if word in ("к", "k"):
        return 1_000
    return next((value for prefix, value in _MULTIPLIERS if word.startswith(prefix)), 1)


def amounts_in(text: str) -> list[int]:
    """Все цены в строке по порядку."""
    result = []
    for match in _PRICE_RE.finditer(_NOT_PRICE_RE.sub(" ", text)):
        if grouped := match.group("grouped"):
            digits = re.sub(rf"[{_SPACES}.,]", "", grouped)
        else:
            digits = match.group("num").replace(",", ".")
        try:
            value = Decimal(digits) * _multiplier(match.group("mult") or "")
        except InvalidOperation:
            continue
        if (amount := int(value)) > 0:
            result.append(amount)
    return result


def parse_amount(text: str) -> int | None:
    """Последняя цена в строке («Пижама 5шт 12500» → 12500); None, если цены нет."""
    amounts = amounts_in(text)
    return amounts[-1] if amounts else None


# типичная ошибка модели — потерянный множитель: «8.9к» → 89 или 8, «11.000» → 11
SUSPICIOUS_BELOW = 1_000
MIN_MISREAD_FACTOR = 10


def normalize(price: Price) -> Price:
    """Число от AI заменяется только в случае потерянного множителя: в raw ровно одна цена,
    AI дал меньше 1000, а raw — минимум в 10 раз больше. Иначе верим модели: raw бывает
    шумным («5000тысяч» → 5 000 000), диапазон «700 - 9 000» не трогает «от 700»."""
    if not price.raw or price.amount >= SUSPICIOUS_BELOW:
        return price
    amounts = amounts_in(price.raw)
    if len(amounts) != 1 or amounts[0] < price.amount * MIN_MISREAD_FACTOR:
        return price
    return price.model_copy(update={"amount": amounts[0]})


# неразрывный пробел внутри суммы: в ячейке с переносом строк «3 000 ₸» не рвётся посередине
NBSP = "\u00a0"


def format_amount(amount: int) -> str:
    return f"{amount:,}".replace(",", NBSP) + f"{NBSP}₸"


def format_prices(prices: Iterable[Price], no_price_text: str) -> str:
    """`Пижама 5 шт: 12 500 ₸; Трусы 25 шт: 5 500 ₸`. Не-тенге — с кодом валюты."""
    parts = []
    for price in prices:
        value = (
            format_amount(price.amount)
            if price.currency == "KZT"
            else f"{price.amount:,}{NBSP}{price.currency}".replace(",", NBSP)
        )
        parts.append(f"{price.item}: {value}" if price.item else value)
    return "; ".join(parts) if parts else no_price_text
