from datetime import date
from decimal import Decimal

import pytest

from bot.models import OpType
from bot.parser import ParseError, parse_date, parse_message

TODAY = date(2026, 10, 4)
ACCOUNTS = ["Карта", "Наличные", "Вклад", "Тинькофф Black"]


def p(text):
    return parse_message(text, TODAY, ACCOUNTS)


def test_simple_expense():
    r = p("350 кофе")
    assert r.amount == Decimal(350)
    assert r.type == OpType.EXPENSE
    assert r.date == TODAY
    assert r.comment == "кофе"
    assert r.tags == []


def test_income_with_plus():
    r = p("+150000 зп")
    assert r.type == OpType.INCOME
    assert r.amount == Decimal(150000)
    assert r.comment == "зп"


def test_amount_after_comment():
    r = p("кофе с собой 350")
    assert r.amount == Decimal(350)
    assert r.comment == "кофе с собой"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("вчера 1200 такси", date(2026, 10, 3)),
        ("позавчера 1200 такси", date(2026, 10, 2)),
        ("Сегодня 1200 такси", TODAY),
        ("1200 такси 1.10", date(2026, 10, 1)),
        ("5.10 800 аптека", date(2025, 10, 5)),  # будущая дата без года -> прошлый год
        ("05.09.2026 800 аптека", date(2026, 9, 5)),
        ("05.09.25 800 аптека", date(2025, 9, 5)),
    ],
)
def test_dates(text, expected):
    assert p(text).date == expected


def test_decimal_amounts():
    assert p("99,90 молоко").amount == Decimal("99.90")
    assert p("350.50 кофе").amount == Decimal("350.50")  # 350.50 не дата
    assert p("1.5к ремонт").amount == Decimal(1500)
    assert p("2к ремонт").amount == Decimal(2000)
    assert p("500р такси").amount == Decimal(500)


def test_tags():
    r = p("4500 отель #командировка #Казань")
    assert r.tags == ["командировка", "казань"]
    assert r.comment == "отель"


def test_transfer():
    r = p("50000 Карта -> Вклад")
    assert r.type == OpType.TRANSFER
    assert (r.account, r.to_account) == ("Карта", "Вклад")
    assert r.amount == Decimal(50000)


def test_transfer_default_account():
    r = p("50000 -> вклад")
    assert r.type == OpType.TRANSFER and (r.account, r.to_account) == ("Карта", "Вклад")
    r = p("30000 вклад ->")
    assert (r.account, r.to_account) == ("Вклад", "Карта")
    with pytest.raises(ParseError):
        p("50000 -> вкдад")          # опечатка: ни одного известного счета


def test_transfer_multiword_and_comment():
    r = p("вчера 10000 Тинькофф Black → наличные снял в банкомате")
    assert (r.account, r.to_account) == ("Тинькофф Black", "Наличные")
    assert r.comment == "снял в банкомате"
    assert r.date == date(2026, 10, 3)


@pytest.mark.parametrize("text", ["", "кофе", "1000 Карта -> Сбер", "1000 Карта -> Карта", "0 кофе"])
def test_errors(text):
    with pytest.raises(ParseError):
        p(text)


def test_only_first_number_is_amount():
    r = p("350 кофе 2 шт")
    assert r.amount == Decimal(350)
    assert r.comment == "кофе 2 шт"


def test_parse_date_invalid():
    assert parse_date("31.02", TODAY) is None
    assert parse_date("кофе", TODAY) is None
