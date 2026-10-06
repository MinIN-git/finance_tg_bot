import asyncio
from datetime import date
from decimal import Decimal

from bot import reports
from bot.models import BigPurchase, Operation, OpType
from bot.service import FinanceService
from bot.storage import MemoryStorage

E, I = OpType.EXPENSE, OpType.INCOME
TODAY = date(2026, 10, 7)


def run(c):
    return asyncio.run(c)


def op(d, t, amount, cat, tags=()):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat, tags=list(tags))


def test_heavy_tag_on_input_writes_big_purchase():
    svc = FinanceService(MemoryStorage(), today_fn=lambda: TODAY)
    p = run(svc.prepare("283122 отпуск турция #крупное"))
    assert p.op.tags == ["тяжелое"]
    p.op.category = "Досуг"
    run(svc.save(p.op))
    [b] = run(svc.storage.list_big_purchases())
    assert (b.what, b.amount, b.category, b.op_id) == ("отпуск турция", 283122, "Досуг", p.op.id)
    assert "🧾 тяжелая трата" in svc.describe(p.op) and "#тяжелое" not in svc.describe(p.op)
    run(svc.set_category(p.op.id, "Подарки"))
    assert run(svc.storage.list_big_purchases())[0].category == "Подарки"
    run(svc.undo())
    assert run(svc.storage.list_big_purchases()) == []


def test_summarize_clean():
    ops = [
        op(date(2026, 9, 1), I, 100000, "Зарплата"),
        op(date(2026, 9, 3), E, 283122, "Досуг", ["тяжелое"]),          # тег — просто не учитывается
        op(date(2026, 9, 4), E, 5000, "Досуг"),
        op(date(2026, 9, 5), E, 60000, "Маркетплейсы"),                  # старая строка листа с категорией
        op(date(2026, 9, 6), E, 30000, "Одежда / украшения"),            # старая строка без категории
        op(date(2026, 9, 7), E, 10000, "Продукты домой"),
    ]
    bigs = [
        BigPurchase(date(2026, 9, 3), "отпуск", Decimal(283122), "Досуг", ops[1].id),
        BigPurchase(date(2026, 9, 5), "телевизор", Decimal(50000), "Маркетплейсы"),
        BigPurchase(date(2026, 9, 6), "пуховик", Decimal(20000)),
    ]
    s = reports.summarize_clean(ops, bigs, date(2026, 9, 1), date(2026, 9, 30))
    assert s.expense == 5000 + 60000 + 30000 + 10000 - 50000 - 20000
    assert dict(s.expense_by_category) == {"Досуг": 5000, "Маркетплейсы": 10000, "Одежда / украшения": 30000, "Продукты домой": 10000}
    items = reports.heavy_items(ops, bigs, date(2026, 9, 1), date(2026, 9, 30))
    assert [b.what for b in items] == ["отпуск", "телевизор", "пуховик"]
    # операция с тегом, которой нет в листе, тоже попадает в список
    lone = op(date(2026, 9, 9), E, 1000, "Досуг", ["тяжелое"])
    assert [b.what for b in reports.heavy_items([lone], [], date(2026, 9, 1), date(2026, 9, 30))] == ["Досуг"]


def test_usual_excludes_heavy_and_current_month():
    ops = [op(date(2026, m, 5), E, 10000, "Досуг") for m in (6, 7, 8)]
    ops += [op(date(2026, 8, 6), E, 90000, "Досуг", ["тяжелое"]), op(date(2026, 9, 5), E, 99999, "Досуг")]
    u = reports.usual(ops, [], date(2026, 9, 1))
    assert u.expense == 10000 and u.by_category == {"Досуг": 10000}
    assert u.months == [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]
