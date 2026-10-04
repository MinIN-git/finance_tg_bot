"""Разбор сообщений вида «350 кофе», «+150000 зп», «вчера 1200 такси #командировка»,
«5000 Карта -> Вклад»."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from .models import OpType
from .textutil import normalize

AMOUNT_RE = re.compile(
    r"^(?P<sign>[+-])?(?P<num>\d+(?:[.,]\d{1,2})?)(?P<mult>к|k|тыс)?(?:р|руб|₽|€|\$|eur|евро)?\.?$"
)
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?$")
RELATIVE_DATES = {"сегодня": 0, "вчера": 1, "позавчера": 2}
ARROW_RE = re.compile(r"\s*(?:->|→|=>)\s*")


class ParseError(ValueError):
    pass


@dataclass
class ParsedMessage:
    amount: Decimal
    type: OpType
    date: date
    comment: str = ""
    tags: list[str] = field(default_factory=list)
    account: str | None = None
    to_account: str | None = None


def parse_date(token: str, today: date) -> date | None:
    token = normalize(token)
    if token in RELATIVE_DATES:
        return today - timedelta(days=RELATIVE_DATES[token])
    m = DATE_RE.match(token)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    # Дата без года из будущего — значит, прошлый год (например, «30.12» в январе).
    if candidate > today:
        try:
            candidate = date(today.year - 1, month, day)
        except ValueError:  # 29.02
            return None
    return candidate


def parse_amount(token: str) -> tuple[str | None, Decimal] | None:
    m = AMOUNT_RE.match(normalize(token))
    if not m:
        return None
    value = Decimal(m.group("num").replace(",", "."))
    if m.group("mult"):
        value *= 1000
    return m.group("sign"), value


def find_account(query: str, accounts: Sequence[str]) -> str | None:
    q = normalize(query)
    for acc in accounts:
        if normalize(acc) == q:
            return acc
    matches = [acc for acc in accounts if normalize(acc).startswith(q)]
    return matches[0] if len(matches) == 1 else None


def _take_account_suffix(text: str, accounts: Sequence[str]) -> tuple[str | None, str]:
    low = normalize(text)
    for acc in sorted(accounts, key=len, reverse=True):
        a = normalize(acc)
        if low.endswith(a) and (len(low) == len(a) or low[-len(a) - 1].isspace()):
            return acc, text[: len(text) - len(a)].strip()
    return None, text


def _take_account_prefix(text: str, accounts: Sequence[str]) -> tuple[str | None, str]:
    low = normalize(text)
    for acc in sorted(accounts, key=len, reverse=True):
        a = normalize(acc)
        if low.startswith(a) and (len(low) == len(a) or low[len(a)].isspace()):
            return acc, text[len(a):].strip()
    return None, text


def parse_message(text: str, today: date, accounts: Sequence[str] = ()) -> ParsedMessage:
    text = text.strip()
    if not text:
        raise ParseError("Пустое сообщение")

    account: str | None = None
    to_account: str | None = None
    parts = ARROW_RE.split(text, maxsplit=1)
    is_transfer = len(parts) == 2
    if is_transfer:
        left, right = parts
        account, left = _take_account_suffix(left, accounts)
        to_account, right = _take_account_prefix(right, accounts)
        if account is None or to_account is None:
            names = ", ".join(accounts) or "—"
            raise ParseError(f"Для перевода укажите счета из списка: {names}")
        if account == to_account:
            raise ParseError("Счет списания и счет зачисления совпадают")
        text = f"{left} {right}"

    amount: Decimal | None = None
    sign: str | None = None
    op_date: date | None = None
    tags: list[str] = []
    rest: list[str] = []

    for token in text.split():
        if token.startswith("#") and len(token) > 1:
            tags.append(normalize(token[1:]))
            continue
        if token.startswith("@") and len(token) > 1 and not is_transfer:
            account = find_account(token[1:], accounts)
            if account is None:
                raise ParseError(f"Не знаю счет «{token[1:]}». Счета: {', '.join(accounts) or '—'}")
            continue
        if op_date is None:
            d = parse_date(token, today)
            if d is not None:
                op_date = d
                continue
        if amount is None:
            parsed = parse_amount(token)
            if parsed is not None:
                sign, amount = parsed
                continue
        rest.append(token)

    if amount is None:
        raise ParseError("Не нашел сумму")
    if amount <= 0:
        raise ParseError("Сумма должна быть больше нуля")

    if is_transfer:
        op_type = OpType.TRANSFER
    elif sign == "+":
        op_type = OpType.INCOME
    else:
        op_type = OpType.EXPENSE

    return ParsedMessage(
        amount=amount,
        type=op_type,
        date=op_date or today,
        comment=" ".join(rest),
        tags=list(dict.fromkeys(tags)),
        account=account,
        to_account=to_account,
    )
