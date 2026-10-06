import asyncio
from datetime import date
from decimal import Decimal

from bot import reports
from bot.models import BalanceEntry, BigPurchase, Operation, OpType
from bot.service import FinanceService
from bot.state import State
from bot.storage import MemoryStorage, parse_big_purchases

E, I = OpType.EXPENSE, OpType.INCOME
NB = " "


def op(d, t, amount, cat):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat)


def history():
    ops = []
    for m, (inc, food, fun) in {6: (100000, 20000, 10000), 7: (100000, 20000, 10000), 8: (100000, 20000, 10000)}.items():
        ops += [op(date(2026, m, 1), I, inc, "Зарплата"), op(date(2026, m, 10), E, food, "Продукты домой"),
                op(date(2026, m, 12), E, fun, "Досуг")]
    # сентябрь: телевизор 50к (разовый, записан и в Маркетплейсы), Досуг ×3
    ops += [op(date(2026, 9, 1), I, 120000, "Зарплата"), op(date(2026, 9, 10), E, 20000, "Продукты домой"),
            op(date(2026, 9, 12), E, 30000, "Досуг"), op(date(2026, 9, 15), E, 50000, "Маркетплейсы")]
    return ops


BIG = [BigPurchase(date(2026, 9, 15), "Телевизор", Decimal(50000)), BigPurchase(date(2026, 8, 1), "Старое", Decimal(1))]


def balances(oct_total=520000):
    return [BalanceEntry(date(2026, 9, 1), "Карта", Decimal(100000)), BalanceEntry(date(2026, 9, 1), "Вклад", Decimal(400000)),
            BalanceEntry(date(2026, 10, 1), "Карта", Decimal(oct_total - 400000)), BalanceEntry(date(2026, 10, 1), "Вклад", Decimal(400000))]


def test_month_close_numbers():
    mc = reports.month_close(history(), BIG, balances(), date(2026, 9, 1), ["Карта", "Вклад"])
    s = mc.report.summary
    assert (s.income, s.expense, s.balance) == (120000, 100000, 20000)
    assert [b.what for b in mc.big] == ["Телевизор"]
    assert mc.expense_wo_big == 50000 and mc.balance_wo_big == 70000
    assert mc.capital.total == 520000 and mc.capital.total_delta == 20000
    assert mc.reconcile_diff == 0 and not mc.reconcile_warning
    assert (mc.rolling.start, mc.rolling.end) == (date(2026, 7, 1), date(2026, 9, 30))
    assert mc.rolling.balance == 70000 + 70000 + 20000 and mc.rolling.income == 320000


def test_reconcile_warning_over_10_percent_of_income():
    mc = reports.month_close(history(), BIG, balances(oct_total=545000), date(2026, 9, 1))
    assert mc.reconcile_diff == 25000          # > 12 000 (10% дохода)
    assert mc.reconcile_warning
    assert "⚠️ Расхождение больше 10% дохода" in reports.format_month_close(mc)


def test_stale_snapshot_not_used():
    old = [BalanceEntry(date(2026, 7, 1), "Карта", Decimal(1))]
    mc = reports.month_close(history(), [], old, date(2026, 9, 1))
    assert mc.capital is None
    assert "Снимок остатков на конец месяца не заполнен" in reports.format_month_close(mc)


def test_format_month_close():
    mc = reports.month_close(history(), BIG, balances(), date(2026, 9, 1), ["Карта", "Вклад"])
    text = reports.format_month_close(mc)
    assert "Сентябрь 2026</b> — итоги месяца" in text
    assert f"🧾 Разовые траты: <b>50{NB}000</b> · 42% дохода" in text
    assert f"• 15.09 Телевизор — 50{NB}000" in text
    assert f"Без разовых: расход 50{NB}000 · сальдо +70{NB}000 · сбережения 58%" in text
    assert f"💼 <b>Капитал на 01.10</b>: <b>520{NB}000</b> (+20{NB}000 к 01.09)" in text
    assert "Сверка: прирост +20" in text and "= <b>0</b> не учтено" in text
    assert "📆 <b>3 месяца (июл–сен)</b>" in text and "сбережения 50%" in text
    # 2–3 сильнее всего отклонившиеся категории — в выводе
    assert "сильнее всего отклонились: Маркетплейсы 🆕, Досуг ⬆️ +20" in text and "(×3,0)" in text


def test_parse_big_purchases_sheet():
    formatted = [["Крупные покупки 2026", "", ""], ["Дата", "Что купил", "Стоимость"],
                 ["15.09.2026", "Телевизор", "50 000,00"], ["09.2026", "Диван", "30 000"],
                 ["", "", ""], ["мусор", "x", "y"]]
    raw = [["Крупные покупки 2026", "", ""], ["Дата", "Что купил", "Стоимость"],
           [46280, "Телевизор", 50000], ["09.2026", "Диван", 30000], ["", "", ""], ["мусор", "x", "y"]]
    got = parse_big_purchases(formatted, raw)
    assert [(b.date, b.what, b.amount) for b in got] == [
        (date(2026, 9, 15), "Телевизор", 50000), (date(2026, 9, 1), "Диван", 30000)]
    assert parse_big_purchases([["что-то"]], [["что-то"]]) == []


def test_state_persists(tmp_path):
    path = tmp_path / "state.json"
    s = State(str(path))
    assert not s.month_closed("2026-10")
    s.mark_month_closed("2026-10")
    assert State(str(path)).month_closed("2026-10")


def test_service_month_close_text_and_snapshot_purpose():
    storage = MemoryStorage(operations=history(), big_purchases=BIG, balances=balances()[:2])
    svc = FinanceService(storage, today_fn=lambda: date(2026, 10, 1))
    q = asyncio.run(svc.start_snapshot(1, purpose="monthly"))
    for _ in range(q.total):
        asyncio.run(svc.answer_snapshot(1, Decimal(100)))
    assert svc.pop_finished_purpose(1) == "monthly"
    assert svc.pop_finished_purpose(1) == "manual"
    text = asyncio.run(svc.month_close_text())
    assert "Капитал на 01.10" in text and "Телевизор" in text
