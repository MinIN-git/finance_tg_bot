import asyncio
from datetime import date
from decimal import Decimal

from bot import reports
from bot.models import BalanceEntry, BigPurchase, Operation, OpType
from bot.service import FinanceService
from bot.storage import MemoryStorage

E, I = OpType.EXPENSE, OpType.INCOME
NB = " "


def op(d, t, amount, cat="Досуг"):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat)


def ops():
    out = [op(date(2026, m, 15), E, 30000) for m in (7, 8, 9)]
    out.append(op(date(2026, 9, 20), E, 60000, "Маркетплейсы"))   # разовая в сентябре
    out += [op(date(2026, 10, 2), E, 4000), op(date(2026, 10, 5), E, 6000), op(date(2026, 10, 6), E, 100000)]
    return out


BIG = [BigPurchase(date(2026, 9, 20), "Диван", Decimal(60000)), BigPurchase(date(2026, 10, 6), "Ноутбук", Decimal(100000))]
TODAY = date(2026, 10, 6)


def test_avg_without_big_and_excludes_current_month():
    assert reports.avg_monthly_expense_wo_big(ops(), BIG, TODAY) == 30000


def test_month_pace():
    spent, expected = reports.month_pace(ops(), BIG, TODAY)
    assert spent == 10000                                  # ноутбук исключен
    assert expected == Decimal(30000) * 6 / 31
    line = reports.format_pace(spent, expected, TODAY)
    assert "потрачено 10" in line and "к 6-му числу" in line and "обычно к этому дню ≈ 5" in line and "⬆️ +72%" in line


def test_pace_without_history():
    spent, expected = reports.month_pace([op(date(2026, 10, 2), E, 100)], [], TODAY)
    assert expected is None
    assert "обычно" not in reports.format_pace(spent, expected, TODAY)


def test_safety_months():
    assert reports.safety_months(Decimal(126000), ops(), BIG, TODAY) == Decimal("4.2")


def test_capital_history_and_safety_text():
    entries = [BalanceEntry(date(2026, m, 1), "Карта", Decimal(100000 + m * 1000)) for m in range(1, 11)]
    s = reports.capital_summary(entries, [])
    text = reports.format_capital(s, history=reports.snapshot_totals(entries), safety=Decimal("4.2"))
    assert "🛟 Запас прочности: <b>4,2</b> мес." in text
    assert f"📈 Рост: за 1 мес +1{NB}000 · за 3 мес +3{NB}000 · за 6 мес +6{NB}000" in text
    assert f"01.10.2026 — 110{NB}000 (+1{NB}000)" in text and "01.01.2026" in text


def test_service_month_and_capital():
    balances = [BalanceEntry(date(2026, 10, 1), "Карта", Decimal(126000))]
    svc = FinanceService(MemoryStorage(operations=ops(), big_purchases=BIG, balances=balances), today_fn=lambda: TODAY)
    month = asyncio.run(svc.month_text())
    assert "⏱ <b>Темп</b>" in month.split("\n")[5]          # сразу после доход/расход/сальдо
    assert "🛟 Запас прочности: <b>4,2</b>" in asyncio.run(svc.capital_text())
