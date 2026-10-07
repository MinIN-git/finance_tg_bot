import asyncio
from datetime import date
from decimal import Decimal

from bot import migrate_streams as ms
from bot import reports
from bot.models import BigPurchase, Kind, Operation, OpType
from bot.storage import MemoryStorage

E = OpType.EXPENSE


def run(c):
    return asyncio.run(c)


def imp(d, amount, cat):
    return Operation(date=d, type=E, amount=Decimal(str(amount)), category=cat, account="Карта",
                     comment="импорт из Бюджета", tags=["импорт"])


def history():
    return [
        imp(date(2026, 7, 31), 107000, "Семейные расходы"),
        imp(date(2026, 8, 31), 87440, "Одежда / украшения"),
        imp(date(2026, 8, 31), 68838, "Подарки"),
        imp(date(2026, 9, 30), 287552, "Досуг"),
        imp(date(2026, 9, 30), "27489.99", "Еда вне дома"),
        imp(date(2026, 9, 30), 23673, "Транспорт/РЖД"),
        imp(date(2026, 9, 30), 31990, "Прочее"),
        Operation(date(2026, 10, 2), E, Decimal(5000), "Досуг", tags=["тяжелое"]),   # введено ботом раньше
    ]


BIGS = [
    BigPurchase(date(2026, 7, 20), "Танюшке с мамкой на отпуск", Decimal(100000)),
    BigPurchase(date(2026, 8, 12), "Кольцо для Танюшки", Decimal(77960)),
    BigPurchase(date(2026, 8, 17), "Круиз для Родителей", Decimal(62838)),
    BigPurchase(date(2026, 9, 6), "Отпуск в Турцию", Decimal(283122)),
]

TRIPS_FORMATTED = [
    ["13.06.2026 – 16.06.2026", "", "", "06.09.2026 – 11.09.2026", ""],
    ["Кафешки", "9 500,13", "", "Кафешки", "14 897,33"],
    ["Транспорт", "3 143", "", "Транспорт", "926"],
    ["Жилье", "18 000", "", "Жилье", "20 000"],
    ["ЖД билеты", "3 000", "", "ЖД билеты", "6 172"],
    ["Прочее", "0", "", "Прочее", ""],
    ["Расход за командировку", "33 643,13", "", "Расход за командировку", "41 995,33"],
]
TRIPS_RAW = [
    ["13.06.2026 – 16.06.2026", "", "", "06.09.2026 – 11.09.2026", ""],
    ["Кафешки", 9500.13, "", "Кафешки", 14897.33],
    ["Транспорт", 3143, "", "Транспорт", 926],
    ["Жилье", 18000, "", "Жилье", 20000],
    ["ЖД билеты", 3000, "", "ЖД билеты", 6172],
    ["Прочее", 0, "", "Прочее", ""],
    ["Расход за командировку", 33643.13, "", "Расход за командировку", 41995.33],
]


def storage():
    import copy
    return MemoryStorage(operations=history(), big_purchases=copy.deepcopy(BIGS),
                         sheets={"Командировки": (TRIPS_FORMATTED, TRIPS_RAW)})


def args(*argv):
    return ms.build_parser().parse_args(list(argv))


def test_parse_trips_sheet():
    trips = ms.parse_trips_sheet(TRIPS_FORMATTED, TRIPS_RAW)
    assert [(t.start, t.end, t.total) for t in trips] == [
        (date(2026, 6, 13), date(2026, 6, 16), Decimal("33643.13")),
        (date(2026, 9, 6), date(2026, 9, 11), Decimal("41995.33")),
    ]
    assert trips[1].items == [("Кафешки", Decimal("14897.33")), ("Транспорт", Decimal(926)),
                              ("Жилье", Decimal(20000)), ("ЖД билеты", Decimal(6172))]
    assert trips[0].items_sum == trips[0].total


def test_plan_and_apply_idempotent():
    st = storage()
    plan = run(ms.make_plan(st, args()))
    assert len(plan.new_trips) == 2 and not plan.warnings
    heavy = [o for o in plan.new_ops if o.kind == Kind.HEAVY]
    trip_ops = [o for o in plan.new_ops if o.kind == Kind.TRIP]
    assert [(o.date, o.category) for o in heavy] == [
        (date(2026, 7, 20), "Семейные расходы"), (date(2026, 8, 12), "Одежда / украшения"),
        (date(2026, 8, 17), "Подарки"), (date(2026, 9, 6), "Досуг")]
    assert len(trip_ops) == 8                                   # 4 + 4 статьи (нулевое «Прочее» пропущено)
    cut = {(r.import_op.category, r.import_op.date.month): r.amount for r in plan.reductions
           if r.import_op.category != "Транспорт/РЖД"}
    assert cut == {("Семейные расходы", 7): 100000, ("Одежда / украшения", 8): 77960, ("Подарки", 8): 62838,
                   ("Досуг", 9): 283122, ("Еда вне дома", 9): Decimal("14897.33"), ("Прочее", 9): 20000}
    assert sum(r.amount for r in plan.reductions if r.import_op.category == "Транспорт/РЖД") == 926 + 6172
    assert [o.kind for o in plan.kind_fixes] == [Kind.HEAVY]
    text = ms.format_plan(plan)
    assert "Командировка 13.06.2026–16.06.2026" in text and "Уменьшить #импорт-операции" in text

    before = reports.summarize(run(st.list_operations()), date(2026, 9, 1), date(2026, 9, 30)).expense
    run(ms.apply_plan(st, plan))
    ops = run(st.list_operations())
    sep = reports.summarize(ops, date(2026, 9, 1), date(2026, 9, 30))
    assert sep.expense == before                                 # всего за месяц не изменилось
    clean = dict(reports.summarize_clean(ops, date(2026, 9, 1), date(2026, 9, 30)).expense_by_category)
    assert clean["Досуг"] == 287552 - 283122 and clean["Еда вне дома"] == Decimal("27489.99") - Decimal("14897.33")
    assert reports.trip_spending(ops, date(2026, 9, 1), date(2026, 9, 30)) == Decimal("41995.33")
    assert [b.category for b in run(st.list_big_purchases())] == [
        "Семейные расходы", "Одежда / украшения", "Подарки", "Досуг"]
    assert {t.id for t in run(st.list_trips())} == {"командировка-130626", "командировка-060926"}

    again = run(ms.make_plan(st, args()))
    assert not (again.new_ops or again.reductions or again.new_trips or again.links)
    assert "Переносить нечего" in ms.format_plan(again)


def test_heavy_override_and_unknown_category():
    st = storage()
    st.big_purchases.append(BigPurchase(date(2026, 9, 20), "Диван", Decimal(50000)))
    plan = run(ms.make_plan(st, args("--heavy", "17.08.2026:62838=Досуг")))
    assert any("«Диван»" in w and "категория неизвестна" in w for w in plan.warnings)
    assert [o.category for o in plan.new_ops if o.date == date(2026, 8, 17)] == ["Досуг"]
    assert any("нет #импорт-операции «Досуг» за 08.2026" in w for w in plan.warnings)


def test_no_deduct_for_june_trip():
    plan = run(ms.make_plan(storage(), args()))
    assert all(r.import_op.date.month != 6 for r in plan.reductions)
    june = [o for o in plan.new_ops if o.date == date(2026, 6, 13)]
    assert sum(o.amount for o in june) == Decimal("33643.13")
