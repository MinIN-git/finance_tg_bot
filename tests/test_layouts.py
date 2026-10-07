"""Отчеты в итоговой раскладке: сухой /report, /stats с плитками, «обычно» = медиана за 6 месяцев."""
import asyncio
from datetime import date
from decimal import Decimal

from bot import reports
from bot.categories import DEFAULT_CATEGORIES
from bot.charts import stats_chart
from bot.models import BalanceEntry, Operation, OpType, Trip
from bot.service import FinanceService
from bot.storage import MemoryStorage

E, I = OpType.EXPENSE, OpType.INCOME
NB = " "
TRIP = Trip("Командировка", date(2026, 9, 6), date(2026, 9, 11))


def op(d, t, amount, cat, tags=(), comment=""):
    return Operation(date=d, type=t, amount=Decimal(str(amount)), category=cat, tags=list(tags), comment=comment)


SEPT_REGULAR = {"Одежда / украшения": 82291, "Ипотека": 32500, "Семейные расходы": 31200, "Продукты домой": 31000,
                "Транспорт/РЖД": 16575, "Еда вне дома": 12593, "Прочее": 11990, "ЖКУ": 10597, "Машина": 3904}


def september():
    ops = [op(date(2026, 9, 1), I, 491386, "Зарплата")]
    ops += [op(date(2026, 9, 10), E, a, c) for c, a in SEPT_REGULAR.items()]
    ops.append(op(date(2026, 9, 6), E, 283122, "Досуг", ["тяжелое"], "Отпуск в Турцию"))
    ops += [op(date(2026, 9, 6), E, 14897, "Еда вне дома", [TRIP.tag], "Кафешки [командировка-060926 Кафешки]"),
            op(date(2026, 9, 6), E, 926, "Транспорт/РЖД", [TRIP.tag], "Транспорт [командировка-060926 Транспорт]"),
            op(date(2026, 9, 6), E, 6172, "Транспорт/РЖД", [TRIP.tag], "ЖД билеты [командировка-060926 ЖД билеты]"),
            op(date(2026, 9, 6), E, 20000, "Прочее", [TRIP.tag], "Жилье [командировка-060926 Жилье]")]
    return ops


BAL = [BalanceEntry(date(2026, 9, 1), "Карта", Decimal(790000)), BalanceEntry(date(2026, 10, 1), "Карта", Decimal(837000))]


def svc(ops, today=date(2026, 10, 1), balances=BAL, trips=(TRIP,)):
    return FinanceService(MemoryStorage(operations=ops, balances=balances, trips=list(trips)), today_fn=lambda: today)


def run(c):
    return asyncio.run(c)


# ---------- /report: сухая статистика ----------

def test_report_main():
    text, heavy, trips = run(svc(september()).month_close_view())
    assert heavy and trips
    assert text.splitlines()[:8] == [
        "📊 <b>Сентябрь 2026</b>",
        f"💰 Доход <b>491{NB}386</b>",
        f"💸 Потрачено <b>557{NB}767</b>",
        f"   🛒 обычные 232{NB}650",
        f"   🧾 тяжелые 283{NB}122",
        f"   ✈️ командировки 41{NB}995",
        f"🔴 Итог <b>−66{NB}381</b> · без тяжелых +216{NB}741",
        f"💼 Капитал на 1 октября <b>837{NB}тыс</b> (на 1 сентября 790{NB}тыс, +47{NB}тыс)",
    ]
    assert f"<b>Обычные траты</b>\n👕 Одежда / украшения — 82{NB}291 · 35%" in text
    assert f"<b>Доходы</b>\n💼 Зарплата — 491{NB}386 · 100%" in text
    for word in ("обычно", "▲", "▼", "⬆", "выше", "💬"):
        assert word not in text                       # без аналитики


def test_report_no_capital_line_without_snapshots():
    text, _, _ = run(svc(september(), balances=[]).month_close_view())
    assert "Капитал" not in text


def test_report_heavy_and_trips_views():
    heavy, _, _ = run(svc(september()).month_close_view(view="heavy"))
    assert f"06.09 🎭 Отпуск в Турцию — 283{NB}122" in heavy and f"Всего: <b>283{NB}122</b>" in heavy
    trips, _, _ = run(svc(september()).month_close_view(view="trips"))
    assert f"<b>Командировка 6–11.09</b> — 41{NB}995" in trips
    lines = trips.split("<b>Командировка 6–11.09</b>")[1].splitlines()[1:]
    assert lines == [f"   🏨 Жилье 20{NB}000", f"   🍽 Кафешки 14{NB}897", f"   🚆 Транспорт/РЖД 7{NB}098"]
    assert "Прочее" not in trips                       # статьи поездки, а не категории


def test_trip_view_bot_entered_ops_by_items():
    ops = [op(date(2026, 9, 7), E, 900, "Еда вне дома", [TRIP.tag], "обед"),
           op(date(2026, 9, 7), E, 5000, "Досуг", [TRIP.tag], "отель"),
           op(date(2026, 9, 8), E, 3000, "Транспорт/РЖД", [TRIP.tag], "билеты ржд"),
           op(date(2026, 9, 8), E, 400, "Транспорт/РЖД", [TRIP.tag], "такси"),
           op(date(2026, 9, 9), E, 300, "Подарки", [TRIP.tag], "сувенир")]
    text, _, _ = run(svc(ops).month_close_view(view="trips"))
    lines = text.split("</b> — ")[1].splitlines()[1:]
    assert lines == [f"   🏨 Жилье 5{NB}000", f"   🚆 Транспорт/РЖД 3{NB}400", "   🍽 Кафешки 900",
                     "   📌 Прочее 300"]


def test_report_buttons_only_when_needed():
    ops = [op(date(2026, 9, 1), I, 100, "Зарплата"), op(date(2026, 9, 2), E, 50, "Досуг")]
    _, heavy, trips = run(svc(ops).month_close_view())
    assert not heavy and not trips


# ---------- /month ----------

def test_month_now_streams():
    ops = [op(date(2026, 10, 1), E, 12400, "Продукты домой"), op(date(2026, 10, 2), I, 120000, "Зарплата"),
           op(date(2026, 10, 3), E, 20000, "Маркетплейсы", ["тяжелое"], "ноутбук"),
           op(date(2026, 10, 4), E, 3000, "Еда вне дома", ["поездка-казань-041026"])]
    text = run(svc(ops, date(2026, 10, 6)).month_text())
    assert text.splitlines()[:6] == [
        "📅 <b>Октябрь</b>, 6-й день",
        f"💸 <b>35{NB}400</b> потрачено",
        f"   🛒 обычные 12{NB}400",
        f"   🧾 тяжелые 20{NB}000",
        f"   ✈️ командировки 3{NB}000",
        f"💰 <b>120{NB}000</b> пришло",
    ]
    assert f"<b>Больше всего</b> (обычные):\n🛒 Продукты домой — 12{NB}400" in text
    assert "Маркетплейсы" not in text.split("Больше всего")[1]


def test_month_now_dashes():
    text = run(svc([op(date(2026, 10, 1), E, 100, "Досуг")], date(2026, 10, 6)).month_text())
    assert "   🧾 тяжелые —" in text and "   ✈️ командировки —" in text


# ---------- «обычно»: медиана, ▲/▼, тенденции ----------

def history(values: dict[int, int], cat="Досуг", year=2026):
    """Обычные траты категории по месяцам + зарплата, чтобы месяц считался «с данными»."""
    ops = []
    for m, v in values.items():
        ops.append(op(date(year, m, 1), I, 100000, "Зарплата"))
        if v:
            ops.append(op(date(year, m, 5), E, v, cat))
    return ops


def test_usual_median_and_band():
    ops = history({3: 10000, 4: 12000, 5: 50000, 6: 11000, 7: 9000, 8: 13000})
    ops.append(op(date(2026, 8, 6), E, 90000, "Досуг", ["тяжелое"]))      # тяжелые не участвуют
    u = reports.usual(ops, date(2026, 9, 1))
    assert u.months[0] == date(2026, 3, 1) and len(u.months) == 6
    assert u.by_category["Досуг"] == 11500                                  # медиана, выброс 50 000 не мешает
    assert u.band("Досуг") == (9000, 50000)


def test_marker_rule():
    u = reports.usual(history({3: 10000, 4: 12000, 5: 11000, 6: 11000, 7: 9000, 8: 12500}), date(2026, 9, 1))
    assert u.by_category["Досуг"] == 11000
    assert u.marker("Досуг", Decimal(12800)) == ""       # выше максимума, но меньше 2 000 от медианы
    assert u.marker("Досуг", Decimal(16000)) == "▲"
    assert u.marker("Досуг", Decimal(12400)) == ""       # в пределах min–max
    assert u.marker("Досуг", Decimal(5000)) == "▼"
    assert u.marker("Новая", Decimal(5000)) == "▲"       # раньше было 0 во всех месяцах
    assert reports.usual([], date(2026, 9, 1)).marker("Досуг", Decimal(99999)) == ""


def test_trends_rule():
    ops = history({4: 6000, 5: 7000, 6: 6500, 7: 9000, 8: 9500, 9: 9200}, "Транспорт/РЖД")
    ops += [op(date(2026, m, 6), E, v, "Машина") for m, v in {4: 18000, 5: 17000, 6: 18000, 7: 3000, 8: 2500, 9: 2700}.items()]
    ops += [op(date(2026, m, 7), E, v, "Подписки") for m, v in {4: 1000, 5: 1000, 6: 1000, 7: 2000, 8: 2000, 9: 2000}.items()]
    rising, falling = reports.trends(ops, date(2026, 9, 1))
    assert [t.name for t in rising] == ["Транспорт/РЖД"]          # +41% и +2 733
    assert [t.name for t in falling] == ["Машина"]
    # «Подписки» +100%, но разница 1 000 — не тенденция
    assert reports.trends(history({8: 1, 9: 1}), date(2026, 9, 1)) is None


# ---------- /stats ----------

def stats_ops():
    ops = history({4: 6000, 5: 7000, 6: 6500, 7: 9000, 8: 9500, 9: 24000}, "Транспорт/РЖД")
    ops += [op(date(2026, m, 6), E, 30000, "Продукты домой") for m in range(4, 10)]
    ops.append(op(date(2026, 9, 6), E, 283122, "Досуг", ["тяжелое"], "Отпуск в Турцию"))
    ops.append(op(date(2026, 9, 7), E, 41995, "Прочее", [TRIP.tag], "Жилье"))
    return ops


def test_stats_text_and_tiles():
    text, png, heavy, trips = run(svc(stats_ops()).stats_view(3, chart=True))
    assert text.splitlines()[0] == "📈 <b>Июль – сентябрь</b>"
    assert f"• Расходы растут: 🚆 Транспорт/РЖД (7{NB}тыс → 14{NB}тыс)" in text
    assert f"🧾 Тяжелые за период: 1 на 283{NB}тыс · ✈️ Командировки: 1 на 42{NB}тыс" in text
    assert heavy and trips and png[:4] == b"\x89PNG"
    st = run(svc(stats_ops()).stats(3))
    tiles = {t.name: t for t in st.tiles}
    assert set(tiles) == {"Транспорт/РЖД", "Продукты домой"}               # только обычные траты
    assert tiles["Транспорт/РЖД"].marker == "▲" and tiles["Продукты домой"].marker == ""
    assert tiles["Транспорт/РЖД"].band == (6000, 9500)
    assert st.heavy == [0, 0, 283122] and st.trips == [0, 0, 41995]


def test_stats_little_history():
    short = history({8: 9500, 9: 24000}, "Транспорт/РЖД")
    text, _, _, _ = run(svc(short).stats_view(3))
    assert "Мало данных для тенденций" in text and "Расходы растут" not in text
    text, png, heavy, trips = run(svc([], balances=[]).stats_view(3, chart=True))
    assert "записей нет" in text and png is None and not heavy and not trips


def test_stats_heavy_and_trips_views():
    heavy, _, _, _ = run(svc(stats_ops()).stats_view(3, "heavy"))
    assert "Отпуск в Турцию" in heavy and "Июль – сентябрь" in heavy
    trips, _, _, _ = run(svc(stats_ops()).stats_view(3, "trips"))
    assert f"Командировка 6–11.09</b> — 41{NB}995" in trips


def test_stats_chart_many_tiles():
    ops = stats_ops() + [op(date(2026, 9, 9), E, 1000 * (i + 1), f"Кат{i}") for i in range(14)]
    st = run(svc(ops).stats(6))
    assert len(st.tiles) == reports.MAX_TILES
    assert stats_chart(st, "тест")[:4] == b"\x89PNG"


# ---------- /cat, /compare, сигналы — та же «обычно» ----------

def test_cat_and_compare_use_median():
    s = svc(stats_ops(), date(2026, 10, 6))
    cat = run(s.category_text("транспорт"))
    assert "Обычно (медиана): <b>8" in cat and "Прошлые месяцы</b> (обычные траты)" in cat
    cmp = run(s.compare_text("август сентябрь"))
    assert f"🚆 Транспорт/РЖД: 9{NB}500 → 24{NB}000 ▲" in cmp
    assert f"🛒 Продукты домой: 30{NB}000 → 30{NB}000" in cmp and "Досуг" not in cmp


def test_signals_use_median_and_regular_only():
    ops = stats_ops() + [op(date(2026, 10, 3), E, 30000, "Транспорт/РЖД"),
                         op(date(2026, 10, 4), E, 900000, "Досуг", ["тяжелое"])]
    keys = [k for k, _ in run(svc(ops, date(2026, 10, 20)).signals())]
    assert "cat:2026-10:Транспорт/РЖД" in keys and "cat:2026-10:Досуг" not in keys
