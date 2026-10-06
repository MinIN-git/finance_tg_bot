from datetime import date

from decimal import Decimal

from bot import reports
from bot.models import BalanceEntry, Operation, OpType

E, I, T = OpType.EXPENSE, OpType.INCOME, OpType.TRANSFER


def op(d, t, amount, cat="", tags=()):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat, tags=list(tags))


def test_date_helpers():
    assert reports.month_end(date(2026, 2, 10)) == date(2026, 2, 28)
    assert reports.month_end(date(2024, 2, 10)) == date(2024, 2, 29)
    assert reports.month_end(date(2026, 12, 5)) == date(2026, 12, 31)
    assert reports.add_months(date(2026, 1, 15), -1) == date(2025, 12, 1)
    assert reports.add_months(date(2026, 11, 30), 2) == date(2027, 1, 1)
    assert reports.week_start(date(2026, 10, 4)) == date(2026, 9, 28)  # воскресенье -> понедельник


def test_summarize_basic():
    ops = [
        op(date(2026, 9, 1), I, 100000, "Зарплата"),
        op(date(2026, 9, 2), E, 30000, "Ипотека"),
        op(date(2026, 9, 3), E, 5000, "Продукты домой"),
        op(date(2026, 9, 4), E, 3000, "Продукты домой"),
        op(date(2026, 9, 5), T, 50000),               # перевод не учитывается
        op(date(2026, 10, 1), E, 999, "Продукты домой"),  # вне периода
    ]
    s = reports.summarize(ops, date(2026, 9, 1), date(2026, 9, 30))
    assert s.income == 100000
    assert s.expense == 38000
    assert s.balance == 62000
    assert s.expense_by_category == [("Ипотека", Decimal(30000)), ("Продукты домой", Decimal(8000))]
    assert s.count == 4


def test_tags():
    ops = [
        op(date(2026, 9, 2), E, 4500, "Досуг", ["командировка"]),
        op(date(2026, 9, 3), E, 1500, "Еда вне дома", ["командировка"]),
        op(date(2026, 9, 3), E, 100, "Еда вне дома"),
    ]
    s = reports.summarize_month(ops, date(2026, 9, 15))
    assert s.tags["командировка"].expense == 6000
    assert s.tags["командировка"].count == 2


def _history():
    ops = []
    # июнь–август: расход 40к, 50к, 60к; доход 100к
    for month, expense in ((6, 40000), (7, 50000), (8, 60000)):
        ops.append(op(date(2026, month, 1), I, 100000, "Зарплата"))
        ops.append(op(date(2026, month, 10), E, expense, "Продукты домой"))
    ops.append(op(date(2026, 9, 1), I, 120000, "Зарплата"))
    ops.append(op(date(2026, 9, 10), E, 60000, "Продукты домой"))
    ops.append(op(date(2026, 9, 11), E, 30000, "Досуг", ["командировка"]))
    return ops


def test_shift_months():
    assert reports.shift_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert reports.shift_months(date(2026, 10, 1), -3) == date(2026, 7, 1)


NB = "\u00a0"


def _snap(d, **amounts):
    names = {"card": "Карта", "dep": "Вклад", "cash": "Наличные"}
    return [BalanceEntry(d, names[k], Decimal(v)) for k, v in amounts.items()]


def _example():
    """Пример из ТЗ: октябрь 12 400 (+650 к сентябрю), карта 3 100 (−200), вклад 9 300 (+850),
    доход − траты +500, не учтено +150, за 3 мес +1 900, за 6 мес +3 400."""
    entries = (
        _snap(date(2026, 4, 1), card=2000, dep=7000)
        + _snap(date(2026, 7, 1), card=2500, dep=8000)
        + _snap(date(2026, 8, 1), card=3000, dep=8200)
        + _snap(date(2026, 9, 1), card=3300, dep=8450)
        + _snap(date(2026, 10, 1), card=3100, dep=9300)
    )
    ops = [
        op(date(2026, 9, 1), I, 1000, "Зарплата"),
        op(date(2026, 9, 15), E, 500, "Продукты домой"),
        op(date(2026, 9, 20), T, 700),              # перевод на вклад не влияет
        op(date(2026, 10, 1), E, 999, "Досуг"),     # октябрь не входит
    ]
    return entries, ops


def test_capital_summary_example():
    entries, ops = _example()
    s = reports.capital_summary(entries, ops, ["Карта", "Вклад", "Наличные"])
    assert s.date == date(2026, 10, 1)
    assert s.total == 12400
    assert s.prev_date == date(2026, 9, 1)
    assert s.total_delta == 650
    assert [(a.account, a.amount, a.delta) for a in s.accounts] == [
        ("Карта", 3100, -200), ("Вклад", 9300, 850),
    ]
    assert s.ops_month == date(2026, 9, 1)
    assert s.ops_balance == 500
    assert s.unaccounted == 150
    assert dict(s.dynamics) == {1: 650, 3: 1900, 6: 3400}   # за 12 мес данных нет


def test_format_capital_example():
    entries, ops = _example()
    text = reports.format_capital(reports.capital_summary(entries, ops, ["Карта", "Вклад"]))
    assert f"Капитал на 1 октября</b>: <b>12{NB}400</b> (+650 к 1 сентября)" in text
    assert f"• Карта: 3{NB}100 (−200)" in text
    assert f"• Вклад: 9{NB}300 (+850)" in text
    assert "Доход − траты за сентябрь: +500" in text
    assert "Не учтено: <b>+150</b>" in text
    assert f"за 3 мес +1{NB}900 · за 6 мес +3{NB}400" in text


def test_capital_currency():
    entries, ops = _example()
    text = reports.format_capital(reports.capital_summary(entries, ops), cur="€")
    assert f"12{NB}400{NB}€" in text


def test_capital_first_snapshot():
    s = reports.capital_summary(_snap(date(2026, 10, 1), card=100, dep=200), [])
    assert s.total == 300 and s.total_delta is None and s.unaccounted is None
    assert all(a.delta is None for a in s.accounts)
    assert "первый снимок" in reports.format_capital(s)
    assert "/snapshot" in reports.format_capital(None)


def test_capital_new_account_and_no_operations():
    entries = _snap(date(2026, 9, 1), card=100) + _snap(date(2026, 10, 1), card=150, cash=50)
    s = reports.capital_summary(entries, [], ["Карта", "Наличные"])
    assert [(a.account, a.delta) for a in s.accounts] == [("Карта", 50), ("Наличные", None)]
    assert s.total_delta == 100
    assert s.ops_balance is None and s.unaccounted is None


def test_capital_same_month_reference():
    entries = _snap(date(2026, 10, 1), card=100) + _snap(date(2026, 10, 15), card=130)
    text = reports.format_capital(reports.capital_summary(entries, []))
    assert "(+30 к 1 октября)" in text


