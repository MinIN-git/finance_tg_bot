import asyncio
from datetime import date
from decimal import Decimal

from bot import reports
from bot.models import BigPurchase, Operation, OpType
from bot.service import FinanceService
from bot.storage import MemoryStorage

E, I = OpType.EXPENSE, OpType.INCOME
NB = " "
TODAY = date(2026, 10, 6)


def op(d, t, amount, cat):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat)


OPS = [
    op(date(2026, 8, 1), I, 100000, "Зарплата"), op(date(2026, 8, 5), E, 20000, "Продукты домой"),
    op(date(2026, 8, 6), E, 5000, "Досуг"),
    op(date(2026, 9, 1), I, 150000, "Зарплата"), op(date(2026, 9, 5), E, 30000, "Продукты домой"),
    op(date(2026, 9, 7), E, 40000, "Маркетплейсы"),
    op(date(2026, 7, 5), E, 10000, "Продукты домой"),
]


def test_format_compare():
    text = reports.format_compare(OPS, date(2026, 8, 1), date(2026, 9, 1))
    assert "Август 2026 → сентябрь 2026" in text
    assert f"💰 Доход: 100{NB}000 → <b>150{NB}000</b>  ⬆️ +50%" in text
    assert f"Сальдо: +75{NB}000 → <b>+80{NB}000</b>" in text
    assert f"Маркетплейсы: 0 → 40{NB}000  🆕" in text
    assert f"Продукты домой: 20{NB}000 → 30{NB}000  ⬆️ +50%" in text
    assert f"Досуг: 5{NB}000 → 0  ⬇️ не было" in text


def test_cat_average_over_months_with_data():
    text = reports.format_category_report("Продукты домой", OPS, TODAY)
    assert f"Среднее за 3 мес.: <b>20{NB}000</b> в месяц" in text   # (10+20+30)/3


def test_format_big():
    bigs = [BigPurchase(date(2026, 9, 7), "Пылесос", Decimal(40000)), BigPurchase(date(2025, 1, 1), "Давно", Decimal(1))]
    text = reports.format_big(bigs, OPS, TODAY)
    assert f"<b>40{NB}000</b> · 38% всех расходов" in text        # 40к из 105к
    assert "Пылесос" in text and "Давно" not in text
    assert "Пока нет" in reports.format_big([], OPS, TODAY)


def run(coro):
    return asyncio.run(coro)


def test_service_compare_args():
    svc = FinanceService(MemoryStorage(operations=OPS), today_fn=lambda: TODAY)
    assert "Август 2026 → сентябрь 2026" in run(svc.compare_text(None))           # по умолчанию два прошлых месяца
    assert "Август 2026 → сентябрь 2026" in run(svc.compare_text("август сентябрь"))
    assert "Август 2026 → сентябрь 2026" in run(svc.compare_text("08.2026 09.2026"))
    assert "Сентябрь 2025 → сентябрь 2026" in run(svc.compare_text("сентябрь 2025 сентябрь"))
    assert "Укажите два месяца" in run(svc.compare_text("абв"))
