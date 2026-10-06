import asyncio
from datetime import date, datetime
from decimal import Decimal

from bot import reports
from bot.charts import quarter_chart
from bot.models import BalanceEntry, Operation, OpType
from bot.notify import send_signals
from bot.service import FinanceService
from bot.storage import MemoryStorage

E, I = OpType.EXPENSE, OpType.INCOME
NB = " "


def op(d, t, amount, cat="Досуг"):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat)


def year_of_data():
    ops, bal, cap = [], [], 100000
    for i in range(12):
        m = reports.add_months(date(2025, 10, 1), i)
        ops += [op(reports.month_end(m), I, 100000, "Зарплата"), op(reports.month_end(m), E, 80000)]
        cap += 20000
        bal.append(BalanceEntry(reports.add_months(m, 1), "Карта", Decimal(cap)))
    return ops, bal


def test_quarter_report():
    ops, bal = year_of_data()
    q = reports.quarter_report(ops, [], bal, date(2026, 10, 1))
    assert q.months == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
    assert (q.summary.income, q.summary.expense) == (300000, 240000)
    assert q.capital_start == 280000 and q.capital_end == 340000      # снимки 01.07 и 01.10
    text = reports.format_quarter(q)
    assert "Квартал: июль–сентябрь 2026" in text
    assert f"(+60{NB}000 за квартал)" in text
    assert "🟢 сен: доход" in text


def test_chart_png():
    ops, bal = year_of_data()
    months, inc, exp, cap = reports.chart_series(ops, bal, date(2026, 10, 1))
    assert len(months) == 12 and cap[-1] == 340000
    png = quarter_chart(months, inc, exp, cap, "Квартал")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10000
    assert quarter_chart(months[:3], inc[:3], exp[:3], [None] * 3, "без капитала")[:4] == b"\x89PNG"


def signal_ops():
    ops = []
    for m in (7, 8, 9):
        ops += [op(date(2026, m, 5), I, 100000, "Зарплата"), op(date(2026, m, 10), E, 10000, "Досуг"),
                op(date(2026, m, 11), E, 50000, "Продукты домой")]
    ops += [op(date(2026, 10, 3), I, 100000, "Зарплата"), op(date(2026, 10, 5), E, 12000, "Досуг"),
            op(date(2026, 10, 6), E, 20000, "Продукты домой"), op(date(2026, 10, 7), E, 5000, "Прочее")]
    return ops


def test_category_signal_only_from_20th():
    assert [k for k, _ in reports.find_signals(signal_ops(), [], date(2026, 10, 19)) if k.startswith("cat:")] == []
    sig = dict(reports.find_signals(signal_ops(), [], date(2026, 10, 20)))
    assert list(k for k in sig if k.startswith("cat:")) == ["cat:2026-10:Досуг"]
    assert f"уже 12{NB}000 к 20-му" in sig["cat:2026-10:Досуг"] and f"≈ 10{NB}000" in sig["cat:2026-10:Досуг"]


def test_other_signal():
    sig = dict(reports.find_signals(signal_ops(), [], date(2026, 10, 12)))
    assert "other:2026-10" in sig and "14% расходов" in sig["other:2026-10"]


def test_reconcile_signal():
    bal = [BalanceEntry(date(2026, 9, 1), "Карта", Decimal(100000)), BalanceEntry(date(2026, 10, 1), "Карта", Decimal(200000))]
    sig = dict(reports.find_signals(signal_ops(), bal, date(2026, 10, 2)))
    assert "reconcile:2026-10-01" in sig            # +100к капитала при сальдо сентября +40к


def test_send_signals_weekly_and_off():
    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, **kw):
            self.sent.append(text)

    svc = FinanceService(MemoryStorage(operations=signal_ops()), today_fn=lambda: date(2026, 10, 20))
    bot = FakeBot()
    t0 = datetime(2026, 10, 20, 20, 0)
    assert asyncio.run(send_signals(bot, [1], svc, now=t0))
    assert "Досуг" in bot.sent[0] and "/signals off" in bot.sent[0]
    # через 3 дня — даже при новом сигнале ничего (не чаще раза в неделю)
    svc.storage.operations.append(op(date(2026, 10, 19), E, 60000, "Продукты домой"))
    assert not asyncio.run(send_signals(bot, [1], svc, now=datetime(2026, 10, 23, 20, 0)))
    # через неделю — только новый сигнал, старый не повторяется
    assert asyncio.run(send_signals(bot, [1], svc, now=datetime(2026, 10, 27, 20, 0)))
    assert "Продукты домой" in bot.sent[1] and "Досуг" not in bot.sent[1]
    svc.state.set("signals_enabled", False)
    svc.storage.operations.append(op(date(2026, 10, 28), E, 90000, "Машина"))
    assert not asyncio.run(send_signals(bot, [1], svc, now=datetime(2026, 11, 20, 20, 0)))
