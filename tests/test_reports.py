from datetime import date

import pytest
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
    assert s.savings_rate == 62
    assert s.expense_by_category == [("Ипотека", Decimal(30000)), ("Продукты домой", Decimal(8000))]
    assert s.count == 4


def test_savings_rate_without_income():
    s = reports.summarize([op(date(2026, 9, 2), E, 100)], date(2026, 9, 1), date(2026, 9, 30))
    assert s.savings_rate is None
    assert s.balance == -100


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


def test_monthly_report_average():
    r = reports.monthly_report(_history(), date(2026, 9, 1))
    assert r.summary.expense == 90000
    assert r.summary.income == 120000
    assert r.avg_expense == 50000
    assert r.avg_income == 100000
    assert r.avg_months == [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]
    assert r.avg_by_category["Продукты домой"] == 50000


def test_monthly_report_skips_empty_months():
    ops = [op(date(2026, 8, 5), E, 1000, "Досуг"), op(date(2026, 9, 5), E, 3000, "Досуг")]
    r = reports.monthly_report(ops, date(2026, 9, 1))
    assert r.avg_months == [date(2026, 8, 1)]
    assert r.avg_expense == 1000


def test_monthly_report_no_history():
    r = reports.monthly_report([op(date(2026, 9, 5), E, 3000, "Досуг")], date(2026, 9, 1))
    assert r.avg_expense is None
    assert "мало данных" in reports.format_monthly_report(r)


def test_format_monthly_report():
    from bot.categories import DEFAULT_CATEGORIES
    icons = {(c.type, c.name): c.emoji for c in DEFAULT_CATEGORIES}
    text = reports.format_monthly_report(reports.monthly_report(_history(), date(2026, 9, 1)), icons=icons)
    assert "Сентябрь 2026</b> — итоги месяца" in text
    assert "🟢 Сальдо: <b>+30\u00a0000</b> · сбережения 25%" in text
    assert "💸 Расход: <b>90\u00a0000</b>  ⬆️ +80%" in text        # 90к против среднего 50к
    assert "💰 Доход: <b>120\u00a0000</b>  ⬆️ +20%" in text
    assert "🛒 Продукты домой — 60\u00a0000 · 67%  ⬆️ +20%" in text  # все категории, с эмодзи
    assert "🎭 Досуг — 30\u00a0000 · 33%  🆕" in text
    assert "💼 Зарплата — 120\u00a0000 · 100%  ⬆️ +20%" in text      # доходы по категориям
    assert "#командировка" in text
    assert text.splitlines()[-1].startswith("💬 Месяц в плюсе на 30\u00a0000")
    assert "сильнее всего отклонились: 🎭 Досуг 🆕, 🛒 Продукты домой ⬆️ +20%." in text


def test_all_nonzero_categories_shown():
    ops = [op(date(2026, 9, i + 1), E, 100 + i, f"Кат{i}") for i in range(8)]
    text = reports.format_monthly_report(reports.monthly_report(ops, date(2026, 9, 1)))
    assert all(f"Кат{i}" in text for i in range(8))
    lines = [l for l in text.splitlines() if l.startswith("Кат")]
    assert lines[0].startswith("Кат7")                          # по убыванию


@pytest.mark.parametrize("current,avg,expected", [
    (Decimal(112), Decimal(100), "⬆️ +12%"),
    (Decimal(80), Decimal(100), "⬇️ −20%"),
    (Decimal(101), Decimal(100), "≈ среднее"),
    (Decimal(11500), Decimal(100), "⬆️ +11\u00a0400 (×115)"),
    (Decimal(320), Decimal(100), "⬆️ +220 (×3,2)"),
    (Decimal(50), None, "🆕"),
    (Decimal(50), Decimal(0), "🆕"),
])
def test_compare_to_average(current, avg, expected):
    assert reports.compare_to_average(current, avg) == expected


def test_red_balance_and_conclusion():
    ops = [op(date(2026, 8, 1), I, 100, "Зарплата"), op(date(2026, 8, 2), E, 100, "Досуг"),
           op(date(2026, 9, 1), I, 100, "Зарплата"), op(date(2026, 9, 2), E, 250, "Досуг")]
    text = reports.format_monthly_report(reports.monthly_report(ops, date(2026, 9, 1)))
    assert "🔴 Сальдо: <b>−150</b>" in text
    assert "💬 Расходы превысили доходы на 150; траты выше среднего на 150%" in text


def test_import_tag_hidden():
    ops = [op(date(2026, 9, 30), E, 100, "Досуг", ["импорт"]), op(date(2026, 9, 3), E, 5, "Досуг", ["командировка"])]
    text = reports.format_monthly_report(reports.monthly_report(ops, date(2026, 9, 1)))
    assert "#импорт" not in text and "#командировка" in text
    s = reports.summarize_month(ops, date(2026, 9, 1))
    assert "#импорт" not in reports.format_month(s, date(2026, 9, 30))


def test_format_category_report():
    ops = _history() + [op(date(2026, 10, 2), E, 700, "Продукты домой")]
    text = reports.format_category_report("Продукты домой", ops, date(2026, 10, 4))
    assert "700" in text
    assert f"сен 2026: 60{NB}000" in text


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
    assert f"всего <b>12{NB}400</b> (+650 к сентябрю)" in text
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
    assert "(+30 к 01.10)" in text


def test_conclusion_uses_multiplier_for_huge_growth():
    ops = [op(date(2026, 8, 1), E, 100, "Досуг"), op(date(2026, 9, 1), E, 520, "Досуг")]
    text = reports.format_monthly_report(reports.monthly_report(ops, date(2026, 9, 1)))
    assert "траты в 5,2 раза выше среднего" in text and "420%" not in text
