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

# Раскладка как на листе Ивана: объединенный заголовок с датами, «Расходы | Сумма»,
# «№ | Статья | Сумма», «Итого | Расход за командировку | Сумма».
TRIPS_FORMATTED = [
    ["Командировки", "", ""],
    ["", "", ""],
    ["13.06.2026 – 16.06.2026", "", ""],
    ["Расходы", "", "Сумма"],
    ["1", "Кафешки", "14 588,13"],
    ["2", "Транспорт", "2 757,00"],
    ["3", "Жилье", "12 700,00"],
    ["4", "ЖД билеты", "3 598,00"],
    ["5", "Прочее", ""],
    ["Итого", "Расход за командировку", "33 643,13"],
    ["", "", ""],
    ["06.09.2026 – 11.09.2026", "", ""],
    ["Расходы", "", "Сумма"],
    ["1", "Кафешки", "14 897,33"],
    ["2", "Транспорт", "926,00"],
    ["3", "Жилье", "20 000,00"],
    ["4", "ЖД билеты", "6 172,00"],
    ["5", "Прочее", ""],
    ["Итого", "Расход за командировку", "41 995,33"],
]
TRIPS_RAW = [
    [r[0] if not (r[0].isdigit() and len(r[0]) == 1) else int(r[0]), r[1],
     float(r[2].replace(" ", "").replace(",", ".")) if r[2] and r[2][0].isdigit() else r[2]]
    for r in TRIPS_FORMATTED
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
    assert trips[0].items == [("Кафешки", Decimal("14588.13")), ("Транспорт", Decimal(2757)),
                              ("Жилье", Decimal(12700)), ("ЖД билеты", Decimal(3598))]
    # только отформатированные значения (как без «сырых» чисел) — то же самое
    assert [t.items for t in ms.parse_trips_sheet(TRIPS_FORMATTED, TRIPS_FORMATTED)] == [t.items for t in trips]
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


# ---------- обрыв посередине --apply и повторный запуск ----------

class Crash(Exception):
    pass


class FlakyStorage(MemoryStorage):
    """Падает на k-м пишущем вызове (как 429 посреди apply)."""

    def __init__(self, crash_at, **kw):
        super().__init__(**kw)
        self.crash_at, self.calls = crash_at, 0

    def _tick(self):
        self.calls += 1
        if self.calls == self.crash_at:
            raise Crash

    async def add_trip(self, trip):
        self._tick()
        await super().add_trip(trip)

    async def add_operations(self, ops):
        self._tick()
        await super().add_operations(ops)

    async def link_big_purchases(self, links):
        self._tick()
        return await super().link_big_purchases(links)

    async def update_operations(self, ops):
        self._tick()
        return await super().update_operations(ops)


def snapshot(st):
    ops = run(st.list_operations())
    return (
        sorted((o.date, o.kind.value, o.category, o.amount, o.comment.split(" [")[0]) for o in ops),
        sorted((b.what, b.category, bool(b.op_id)) for b in run(st.list_big_purchases())),
        sorted(t.id for t in run(st.list_trips())),
    )


def flaky_storage(crash_at):
    import copy
    return FlakyStorage(crash_at, operations=history(), big_purchases=copy.deepcopy(BIGS),
                        sheets={"Командировки": (TRIPS_FORMATTED, TRIPS_RAW)})


def test_rerun_after_crash_at_any_step():
    clean = storage()
    run(ms.apply_plan(clean, run(ms.make_plan(clean, args()))))
    expected = snapshot(clean)
    total_calls = 2 + 3          # 2 поездки, операции, связи с листом, правки операций
    for k in range(1, total_calls + 1):
        st = flaky_storage(k)
        try:
            run(ms.apply_plan(st, run(ms.make_plan(st, args()))))
        except Crash:
            pass
        st.crash_at = -1
        rest = run(ms.make_plan(st, args()))
        assert not any("тяжелая" in w for w in rest.warnings), k
        run(ms.apply_plan(st, rest))
        assert snapshot(st) == expected, f"обрыв на шаге {k}"
        assert not ms.format_plan(run(ms.make_plan(st, args()))).count("+ "), k   # третьему запуску нечего делать


def test_dry_run_after_partial_apply_shows_only_rest():
    st = flaky_storage(4)        # поездки и операции записаны, связи с листом — нет
    try:
        run(ms.apply_plan(st, run(ms.make_plan(st, args()))))
    except Crash:
        pass
    text = ms.format_plan(run(ms.make_plan(st, args())))
    assert "Новые поездки" not in text and "Новые операции" not in text
    assert "Вписать категорию и id" in text and "Уменьшить #импорт-операции" in text
    assert "осталось связать строку листа" in text


def test_full_migration_on_sheets_with_few_reads(monkeypatch):
    import gspread
    from tests.test_sheets_storage import FakeClient
    from bot.storage import SheetsStorage, op_to_row

    client = FakeClient()
    monkeypatch.setattr(gspread, "service_account", lambda filename: client)
    s = SheetsStorage("key.json", "sheet-id", cache_ttl=3600)
    s.ensure_sheets()
    sh = client.sh.sheets
    for op in history():
        sh["Операции"].rows.append([c[1:] if isinstance(c, str) and c.startswith("'") else c for c in op_to_row(op)])
    sh["Большие покупки"].rows += [[(b.date - date(1899, 12, 30)).days, b.what, int(b.amount)] for b in BIGS]
    client.sh.add_worksheet("Командировки", 30, 5).rows = [list(r) for r in TRIPS_RAW]

    reads = {"n": 0}
    for ws in client.sh.sheets.values():
        for name in ("get_all_values", "col_values", "row_values"):
            orig = getattr(ws, name)

            def counting(*a, _orig=orig, **kw):
                reads["n"] += 1
                return _orig(*a, **kw)
            setattr(ws, name, counting)

    plan = run(ms.make_plan(s, args()))
    assert len(plan.new_ops) == 12 and len(plan.reductions) == 8 and not plan.warnings
    run(ms.apply_plan(s, plan))
    assert reads["n"] <= 15, reads["n"]                 # раньше — чтение «Операций» на каждую правку
    assert [r[3] for r in sh["Большие покупки"].rows[1:]] == ["Семейные расходы", "Одежда / украшения", "Подарки", "Досуг"]
    fresh = SheetsStorage("key.json", "sheet-id", cache_ttl=3600)
    again = run(ms.make_plan(fresh, args()))
    assert not (again.new_ops or again.reductions or again.new_trips or again.links)
