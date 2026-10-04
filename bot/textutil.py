from __future__ import annotations

from datetime import date
from decimal import Decimal

MONTHS_NOMINATIVE = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]
MONTHS_DATIVE = [
    "январю", "февралю", "марту", "апрелю", "маю", "июню",
    "июлю", "августу", "сентябрю", "октябрю", "ноябрю", "декабрю",
]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

NBSP = " "


def normalize(text: str) -> str:
    """Нижний регистр и «ё» -> «е»; длина строки не меняется."""
    return text.lower().replace("ё", "е")


def fmt_money(value: Decimal, currency: str = "", signed: bool = False) -> str:
    q = value.quantize(Decimal("0.01"))
    sign = ""
    if q < 0:
        sign = "−"
    elif signed and q > 0:
        sign = "+"
    q = abs(q)
    if q == q.to_integral_value():
        body = f"{int(q):,}"
    else:
        body = f"{q:,.2f}".replace(".", "#")
    body = body.replace(",", NBSP).replace("#", ",")
    return f"{sign}{body}{NBSP}{currency}" if currency else f"{sign}{body}"


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def fmt_short_date(d: date) -> str:
    return d.strftime("%d.%m")


def month_title(d: date) -> str:
    return f"{MONTHS_NOMINATIVE[d.month - 1]} {d.year}"


def fmt_percent(value: Decimal | float) -> str:
    return f"{round(float(value))}%"
