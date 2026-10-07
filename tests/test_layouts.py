"""Тексты отчетов в итоговой раскладке."""
import asyncio
from datetime import date
from decimal import Decimal


from bot import reports
from bot.categories import DEFAULT_CATEGORIES
from bot.charts import stats_chart
from bot.models import BalanceEntry, BigPurchase, Operation, OpType
from bot.service import FinanceService
from bot.storage import MemoryStorage

E, I = OpType.EXPENSE, OpType.INCOME
NB = " "
ICONS = {(c.type, c.name): c.emoji for c in DEFAULT_CATEGORIES}


def op(d, t, amount, cat, tags=(), comment=""):
    return Operation(date=d, type=t, amount=Decimal(amount), category=cat, tags=list(tags), comment=comment)


def history():
    """Июнь–август — обычные месяцы, сентябрь — с отпуском (#тяжелое) и ростом одежды."""
    ops = []
    for m in (6, 7, 8):
        ops += [op(date(2026, m, 1), I, 300000, "Зарплата"), op(date(2026, m, 5), E, 44000, "Одежда / украшения"),
                op(date(2026, m, 6), E, 18000, "Машина"), op(date(2026, m, 7), E, 30000, "Продукты домой"),
                op(date(2026, m, 8), E, 500, "Подписки")]
    ops += [op(date(2026, 9, 1), I, 491386.34, "Зарплата"), op(date(2026, 9, 5), E, 82291, "Одежда / украшения"),
            op(date(2026, 9, 6), E, 2672, "Машина"), op(date(2026, 9, 7), E, 31000, "Продукты домой"),
            op(date(2026, 9, 12), E, 283122, "Досуг", ["тяжелое"], "отпуск турция"),
            op(date(2026, 9, 13), E, 5000, "Досуг")]
    return ops


BAL = [BalanceEntry(date(2026, 9, 1), "Карта", Decimal(790000)), BalanceEntry(date(2026, 10, 1), "Карта", Decimal(837000))]


def svc(today=date(2026, 10, 1), ops=None, bigs=None, balances=BAL):
    return FinanceService(MemoryStorage(operations=ops if ops is not None else history(), big_purchases=bigs or [],
                                        balances=balances), today_fn=lambda: today)


def run(c):
    return asyncio.run(c)


# ---------- /month ----------

def test_month_now():
    ops = [op(date(2026, 10, d), E, a, c) for d, a, c in
           [(1, 12400, "Продукты домой"), (2, 9800, "Еда вне дома"), (3, 6100, "Транспорт/РЖД"), (3, 5900, "ЖКУ"),
            (4, 4700, "Подписки"), (5, 3200, "Досуг")]]
    ops += [op(date(2026, 10, 2), I, 120000, "Зарплата"), op(date(2026, 10, 6), E, 20000, "Маркетплейсы", ["тяжелое"], "ноутбук")]
    text = run(svc(date(2026, 10, 6), ops).month_text())
    assert text.splitlines()[:4] == [
        "📅 <b>Октябрь</b>, 6-й день",
        f"💸 <b>62{NB}100</b> потрачено",
        f"🧾 из них тяжелые: 20{NB}000",
        f"💰 <b>120{NB}000</b> пришло",
    ]
    assert "<b>Больше всего:</b>" in text
    top = text.split("<b>Больше всего:</b>\n")[1].splitlines()
    assert top[0] == f"📦 Маркетплейсы — 20{NB}000" and len(top) == 5   # столбцом, топ-5
    assert "обычно" not in text and "⬆️" not in text                      # без сравнений с прошлым
    all_text = run(svc(date(2026, 10, 6), ops).month_text("all"))
    assert "🎭 Досуг — 3" in all_text and "💼 Зарплата" in all_text
    last = run(svc(date(2026, 10, 6), ops).month_text("last"))
    assert "последние траты" in last and "ноутбук" in last


def test_month_now_no_heavy():
    text = run(svc(date(2026, 10, 6), [op(date(2026, 10, 1), E, 100, "Досуг")]).month_text())
    assert "🧾 из них тяжелые: —" in text


# ---------- итоги месяца ----------

def test_month_close_main():
    text = run(svc().month_close_text())
    lines = text.splitlines()
    assert lines[:5] == [
        "📊 <b>Сентябрь</b> без тяжелых трат",
        f"💰 Доход <b>491{NB}тыс</b>",
        f"💸 Расход <b>121{NB}тыс</b>",
        f"🟢 Итог <b>+370{NB}тыс</b>",
        f"💼 Капитал на 1 октября <b>837{NB}тыс</b> (на 1 сентября 790{NB}тыс, +47{NB}тыс)",
    ]
    assert f"<b>Выше обычного:</b>\n👕 Одежда / украшения 82{NB}тыс · обычно 44{NB}тыс" in text
    assert f"<b>Ниже обычного:</b>\n🚗 Машина 3{NB}тыс · обычно 18{NB}тыс" in text
    assert "Продукты" not in text                    # +3% — как обычно, не показывается
    assert "Досуг" not in text                       # отпуск — тяжелая трата, в основной вид не входит
    assert f"🧾 Тяжелые траты: 1 на 283{NB}тыс" in text
    assert "отпуск" not in text                      # названий тяжелых в основном виде нет
    assert "Не учтено" not in text and "сверка" not in text.lower() and "%" not in text


def test_month_close_heavy_view():
    text = run(svc().month_close_text(view="heavy"))
    assert f"12.09 🎭 отпуск турция — 283{NB}122" in text
    assert f"💸 Расход 404{NB}тыс (без них 121{NB}тыс)" in text
    assert f"🟢 Итог <b>+87{NB}тыс</b> (без них +370{NB}тыс)" in text
    assert f"🎭 Досуг 288{NB}тыс (без них 5{NB}тыс)" in text
    assert f"За 12 месяцев: 1 трат на 283{NB}тыс" in text


def test_sheet_rows_do_not_change_sums():
    """Строки «Больших покупок» — только зеркало: в расчетах участвует «Вид» операций."""
    ops = history()[:-2] + [op(date(2026, 9, 12), E, 283122, "Досуг"), op(date(2026, 9, 13), E, 5000, "Досуг")]
    bigs = [BigPurchase(date(2026, 9, 12), "Отпуск в Турцию", Decimal(283122))]
    text = run(svc(ops=ops, bigs=bigs).month_close_text())
    assert f"💸 Расход <b>404{NB}тыс</b>" in text and "Тяжелые траты" not in text


def test_month_close_all_categories_and_no_snapshot():
    s = svc(balances=[])
    assert "снимок остатков не заполнен" in run(s.month_close_text())
    text = run(s.month_close_text(view="all"))
    assert "все категории без тяжелых трат" in text and "🎭 Досуг — 5" in text and "💼 Зарплата" in text


def test_month_close_without_history():
    text = run(svc(ops=[op(date(2026, 9, 3), E, 100, "Досуг")], balances=[]).month_close_text())
    assert "Сравнивать пока не с чем" in text


# ---------- /stats ----------

def stats_ops():
    ops = []
    for m, (taxi, car) in {7: (6000, 18000), 8: (7000, 17000), 9: (24000, 3000)}.items():
        ops += [op(date(2026, m, 1), I, 300000, "Зарплата"), op(date(2026, m, 3), E, taxi, "Транспорт/РЖД"),
                op(date(2026, m, 4), E, car, "Машина"), op(date(2026, m, 5), E, 30000, "Продукты домой")]
    ops.append(op(date(2026, 9, 12), E, 283122, "Досуг", ["тяжелое"], "отпуск"))
    return ops


def test_stats_text():
    bal = [BalanceEntry(date(2026, 7, 1), "Карта", Decimal(700000)), BalanceEntry(date(2026, 10, 1), "Карта", Decimal(837000))]
    text, png = run(svc(ops=stats_ops(), balances=bal).stats_report(3))
    assert text.splitlines()[0] == "📈 <b>Июль – сентябрь</b>, без тяжелых"
    assert f"• Расходы растут: 🚆 Транспорт/РЖД (6{NB}тыс → 24{NB}тыс)" in text
    assert f"• Расходы снижаются: 🚗 Машина (18{NB}тыс → 3{NB}тыс)" in text
    assert f"• Капитал вырос на 137{NB}тыс" in text
    assert f"🧾 Тяжелые: 283{NB}тыс" in text
    assert "Продукты" not in text and "Досуг" not in text
    assert png[:4] == b"\x89PNG"
    heavy = run(svc(ops=stats_ops(), balances=bal).stats_heavy_text(3))
    assert "отпуск" in heavy and "Июль – сентябрь" in heavy


def test_stats_periods_and_empty():
    st = run(svc(ops=stats_ops()).stats(6))
    assert st.months[0] == date(2026, 4, 1) and len(st.months) == 6
    text, png = run(svc(ops=[], balances=[]).stats_report(3))
    assert "записей нет" in text and png is None


def test_stats_chart_png():
    st = run(svc(ops=stats_ops(), balances=BAL).stats(3))
    png = stats_chart(st.months, [s.income for s in st.monthly], [s.expense for s in st.monthly],
                      st.capital, st.category_series(), "тест")
    assert png[:4] == b"\x89PNG" and len(png) > 10000
    assert set(st.category_series()) == {"Транспорт/РЖД", "Машина", "Продукты домой"}   # без тяжелого Досуга


# ---------- прочие команды в тех же правилах ----------

def test_deviation():
    assert reports.deviation(Decimal(140), Decimal(100)) == "⬆️ +40%"
    assert reports.deviation(Decimal(129), Decimal(100)) == ""
    assert reports.deviation(Decimal(60), Decimal(100)) == "⬇️ −40%"
    assert reports.deviation(Decimal(11500), Decimal(100)) == "⬆️ ×115"
    assert reports.deviation(Decimal(320), Decimal(100)) == "⬆️ ×3,2"
    assert reports.deviation(Decimal(5), None) == ""


def test_compare():
    text = run(svc(ops=stats_ops()).compare_text("август сентябрь"))
    assert "Август 2026 → сентябрь 2026</b>, без тяжелых" in text
    assert f"🚆 Транспорт/РЖД: 7{NB}000 → 24{NB}000  ⬆️ ×3,4" in text
    assert f"🛒 Продукты домой: 30{NB}000 → 30{NB}000" in text and "Досуг" not in text
    assert "%" not in text.split("⬆️ ×3,4")[0].split("Итог")[0].replace("⬆️ ×", "")


def test_big_and_cat():
    s = svc(date(2026, 10, 6), ops=stats_ops())
    big = run(s.big_text())
    assert f"1 на <b>283{NB}тыс</b>" in big and "12.09 🎭 отпуск" in big
    cat = run(s.category_text("транспорт"))
    assert "Прошлые месяцы</b> (без тяжелых)" in cat and f"Обычно: <b>12{NB}333</b> в месяц" in cat


def test_signals_clean():
    ops = stats_ops() + [op(date(2026, 10, 3), E, 30000, "Транспорт/РЖД"),
                         op(date(2026, 10, 4), E, 900000, "Досуг", ["тяжелое"])]
    keys = [k for k, _ in run(svc(date(2026, 10, 20), ops).signals())]
    assert "cat:2026-10:Транспорт/РЖД" in keys and "cat:2026-10:Досуг" not in keys
