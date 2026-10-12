import asyncio
from datetime import date
from decimal import Decimal

from bot import reports
from bot.models import Operation, OpType
from bot.service import FinanceService
from bot.storage import MemoryStorage

E, I = OpType.EXPENSE, OpType.INCOME
TODAY = date(2026, 10, 7)


def run(c):
    return asyncio.run(c)


def op(d, t, amount, cat, tags=()):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat, tags=list(tags))


def test_heavy_tag_on_input_is_heavy_kind():
    svc = FinanceService(MemoryStorage(), today_fn=lambda: TODAY)
    p = run(svc.prepare("283122 отпуск турция #крупное"))
    assert p.op.tags == ["тяжелое"]
    p.op.category = "Досуг"
    run(svc.save(p.op))
    assert run(svc.get_operation(p.op.id)).kind.value == "тяжелая"
    assert "🧾 тяжелая трата" in svc.describe(p.op) and "#тяжелое" not in svc.describe(p.op)


def test_summarize_clean_only_regular():
    ops = [
        op(date(2026, 9, 1), I, 100000, "Зарплата"),
        op(date(2026, 9, 3), E, 283122, "Досуг", ["тяжелое"]),
        op(date(2026, 9, 4), E, 5000, "Досуг"),
        op(date(2026, 9, 7), E, 9000, "Еда вне дома", ["поездка-казань-070926"]),
        op(date(2026, 9, 8), E, 10000, "Продукты домой"),
    ]
    assert [o.kind.value for o in ops] == ["обычная", "тяжелая", "обычная", "командировка", "обычная"]
    s = reports.summarize_clean(ops, date(2026, 9, 1), date(2026, 9, 30))
    assert s.expense == 15000 and s.income == 100000
    assert dict(s.expense_by_category) == {"Досуг": 5000, "Продукты домой": 10000}
    assert [b.what for b in reports.heavy_items(ops, date(2026, 9, 1), date(2026, 9, 30))] == ["Досуг"]
    assert reports.trip_spending(ops, date(2026, 9, 1), date(2026, 9, 30)) == 9000
    # «потрачено за месяц» = обычные + тяжелые + командировки
    assert reports.summarize(ops, date(2026, 9, 1), date(2026, 9, 30)).expense == 15000 + 283122 + 9000


def test_usual_excludes_heavy_and_current_month():
    ops = [op(date(2026, m, 5), E, 10000, "Досуг") for m in (6, 7, 8)]
    ops += [op(date(2026, 8, 6), E, 90000, "Досуг", ["тяжелое"]), op(date(2026, 9, 5), E, 99999, "Досуг")]
    u = reports.usual(ops, date(2026, 9, 1))
    assert u.expense == 10000 and u.by_category == {"Досуг": 10000}
    assert u.months == [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]
