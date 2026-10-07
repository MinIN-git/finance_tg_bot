from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

MONTHS_NOMINATIVE = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]
MONTHS_DATIVE = [
    "январю", "февралю", "марту", "апрелю", "маю", "июню",
    "июлю", "августу", "сентябрю", "октябрю", "ноябрю", "декабрю",
]
MONTHS_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

NBSP = " "


def normalize(text: str) -> str:
    """Нижний регистр и «ё» -> «е»; длина строки не меняется."""
    return text.lower().replace("ё", "е")


def fmt_money(value: Decimal, currency: str = "", signed: bool = False, exact: bool = False) -> str:
    """«12 400», «+650», «99,90» (копейки — только с exact=True, в отчетах суммы целые)."""
    q = value.quantize(Decimal("0.01") if exact else Decimal(1), rounding=ROUND_HALF_UP)
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


def fmt_k(value: Decimal, currency: str = "", signed: bool = False) -> str:
    """Суммы в итогах: «837 тыс», «+47 тыс», «1 506 тыс», мелочь — как есть («650»)."""
    if abs(value) < 1000:
        return fmt_money(value, currency, signed)
    thousands = (value / 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    body = fmt_money(thousands, signed=signed)
    return f"{body}{NBSP}тыс{NBSP}{currency}" if currency else f"{body}{NBSP}тыс"


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def fmt_short_date(d: date) -> str:
    return d.strftime("%d.%m")


def month_title(d: date) -> str:
    return f"{MONTHS_NOMINATIVE[d.month - 1]} {d.year}"


def fmt_percent(value: Decimal | float) -> str:
    return f"{round(float(value))}%"


def fmt_trip_range(start: date, end: date) -> str:
    """«13–16.10», «30.12–2.01», «3.10»."""
    if start == end:
        return f"{start.day}.{start:%m}"
    if (start.month, start.year) == (end.month, end.year):
        return f"{start.day}–{end.day}.{end:%m}"
    return f"{start.day}.{start:%m}–{end.day}.{end:%m}"
