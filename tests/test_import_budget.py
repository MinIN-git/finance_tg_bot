from datetime import date
from decimal import Decimal

import pytest

from bot.categories import DEFAULT_CATEGORIES
from bot.import_budget import (
    build_plan, format_plan, map_category, month_status, parse_budget, parse_month_label,
)
from bot.models import Operation, OpType
from bot.storage import MemoryStorage

TODAY = date(2026, 10, 4)
E, I = OpType.EXPENSE, OpType.INCOME


def grid(cells: dict[str, object], rows=40, cols=20):
    """Сетка из словаря {"A1": значение}; возвращает (formatted, raw) как отдает Sheets."""
    raw = [["" for _ in range(cols)] for _ in range(rows)]
    for addr, value in cells.items():
        col = ord(addr[0]) - 65
        row = int(addr[1:]) - 1
        raw[row][col] = value
    formatted = [[f"{v:,.2f}".replace(",", " ").replace(".", ",") if isinstance(v, float) else str(v) for v in r] for r in raw]
    return formatted, raw


def month_block(col: str, row: int, title: str, income: dict, expense: dict, inc_total, exp_total):
    """Блок месяца: заголовок, «Доходы | Сумма», ниже «Расходы | Сумма», справа «Отчет»."""
    c = ord(col)
    nxt = lambda k: chr(c + k)  # noqa: E731
    cells = {f"{col}{row}": title, f"{col}{row + 1}": "Доходы", f"{nxt(1)}{row + 1}": "Сумма"}
    r = row + 2
    for name, amount in income.items():
        cells[f"{col}{r}"], cells[f"{nxt(1)}{r}"] = name, amount
        r += 1
    cells[f"{col}{r}"], cells[f"{nxt(1)}{r}"] = "Итого", float(sum(v for v in income.values() if v))
    r += 2
    cells[f"{col}{r}"], cells[f"{nxt(1)}{r}"] = "Расходы", "Сумма"
    r += 1
    for name, amount in expense.items():
        cells[f"{col}{r}"], cells[f"{nxt(1)}{r}"] = name, amount
        r += 1
    cells[f"{nxt(3)}{row + 1}"] = "Отчет"
    cells[f"{nxt(3)}{row + 2}"], cells[f"{nxt(4)}{row + 2}"] = "Доходы за месяц:", inc_total
    cells[f"{nxt(3)}{row + 3}"], cells[f"{nxt(4)}{row + 3}"] = "Расходы за месяц:", exp_total
    return cells


SEPT_INCOME = {"Зарплата": 450000.0, "Папа": 41386.34, "Подарки": ""}
SEPT_EXPENSE = {
    "ЖКУ + интернет": 12000.0, "Ипотека": 60000.0, "Транспорт/РЖД/Такси": 8000.0,
    "Еда вне дома/доставки": 15000.0, "Продукты домой": 462767.01, "Подписки": 0,
}


def budget_sheet():
    cells = {}
    cells.update(month_block("A", 1, "Июль", {"Зарплата": 300000.0}, {"Ипотека": 60000.0, "Досуг": 1000.5}, 300000.0, 61000.5))
    cells.update(month_block("H", 1, "Август 2026", {"Зарплата": 310000.0}, {"Ипотека": 60000.0}, 310000.0, 60000.0))
    cells.update(month_block("A", 20, "Сентябрь", SEPT_INCOME, SEPT_EXPENSE, 491386.34, 557767.01))
    cells.update(month_block("H", 20, "Октябрь", {"Зарплата": 1.0}, {"Досуг": 1.0}, 1.0, 1.0))  # текущий
    return grid(cells)


@pytest.mark.parametrize("text,expected", [
    ("Сентябрь", date(2026, 9, 1)),
    ("сентябрь 2025", date(2025, 9, 1)),
    ("Сентябрь 2026 г.", date(2026, 9, 1)),
    ("Ноябрь", date(2025, 11, 1)),          # будущий месяц без года -> прошлый год
    ("01.09.2026", date(2026, 9, 1)),
    ("09.2026", date(2026, 9, 1)),
    ("Май", date(2026, 5, 1)),
    ("Sep 2026", date(2026, 9, 1)),
])
def test_month_label(text, expected):
    assert parse_month_label(text, TODAY) == expected


@pytest.mark.parametrize("text", ["Доходы за сентябрь", "Маркетплейсы", "Машина", "Март 8", "Сумма", "", "Отчет"])
def test_not_month_label(text):
    assert parse_month_label(text, TODAY) is None


def test_parse_budget_blocks():
    formatted, raw = budget_sheet()
    blocks, warnings = parse_budget(formatted, raw, TODAY)
    assert warnings == []
    assert [b.month for b in blocks] == [date(2026, m, 1) for m in (7, 8, 9, 10)]
    sept = blocks[2]
    assert [(i.name, i.amount) for i in sept.income] == [("Зарплата", 450000), ("Папа", Decimal("41386.34"))]
    assert len(sept.expense) == 5                     # «Подписки: 0» пропущены
    assert sept.income_sum == Decimal("491386.34")
    assert sept.expense_sum == Decimal("557767.01")
    assert sept.income_total == Decimal("491386.34")
    assert sept.expense_total == Decimal("557767.01")
    assert sept.reconcile() == []
    assert blocks[0].expense_sum == Decimal("61000.5")


def test_parse_budget_without_raw_numbers():
    formatted, _ = budget_sheet()
    blocks, _ = parse_budget(formatted, formatted, TODAY)  # только отформатированный текст «491 386,34»
    assert blocks[2].income_total == Decimal("491386.34")
    assert blocks[2].reconcile() == []


def test_reconcile_mismatch():
    cells = month_block("A", 1, "Сентябрь", {"Зарплата": 100.0}, {"Досуг": 50.0}, 100.0, 70.0)
    blocks, _ = parse_budget(*grid(cells), TODAY)
    assert blocks[0].reconcile() == ["расходы: сумма категорий 50.0 ≠ итог 70.0 (разница -20.0)"]


def test_no_month_labels():
    formatted, raw = grid({"A1": "Доходы", "B1": "Сумма", "A2": "Зарплата", "B2": 1.0})
    blocks, warnings = parse_budget(formatted, raw, TODAY)
    assert blocks == [] and "месяц" in warnings[0]


@pytest.mark.parametrize("old,op_type,expected", [
    ("ЖКУ + интернет", E, "ЖКУ"),
    ("ЖКУ+интернет", E, "ЖКУ"),
    ("Транспорт/РЖД/Такси", E, "Транспорт/РЖД"),
    ("Транспорт / РЖД", E, "Транспорт/РЖД"),
    ("Еда вне дома/доставки", E, "Еда вне дома"),
    ("Одежда/украшения", E, "Одежда / украшения"),
    ("продукты домой", E, "Продукты домой"),
    ("Подарки", I, "Подарки"),
    ("Подарки", E, "Подарки"),
    ("Зарплата", I, "Зарплата"),
    ("Зарплата", E, None),
    ("Бизнес", E, None),
])
def test_map_category(old, op_type, expected):
    assert map_category(old, op_type, DEFAULT_CATEGORIES) == expected


def test_map_category_override():
    assert map_category("Бизнес", E, DEFAULT_CATEGORIES, {"бизнес": "Прочее"}) == "Прочее"
    assert map_category("Бизнес", E, DEFAULT_CATEGORIES, {"Бизнес": "Нет такой"}) is None


def test_build_plan():
    formatted, raw = budget_sheet()
    blocks, _ = parse_budget(formatted, raw, TODAY)
    plans = build_plan(blocks, DEFAULT_CATEGORIES, [], TODAY)
    assert [p.block.month.month for p in plans] == [7, 8, 9]   # октябрь (текущий) не импортируется
    sept = plans[2]
    assert sept.issues == [] and sept.unmapped == []
    assert all(op.date == date(2026, 9, 30) and op.tags == ["импорт"] and op.account == "Карта" for op in sept.operations)
    by_cat = {op.category: op for op in sept.operations}
    assert by_cat["ЖКУ"].comment == "импорт из Бюджета: ЖКУ + интернет"
    assert by_cat["Ипотека"].comment == "импорт из Бюджета"
    assert sum(op.amount for op in sept.operations if op.type == I) == Decimal("491386.34")
    assert sum(op.amount for op in sept.operations if op.type == E) == Decimal("557767.01")
    assert month_status(sept, replace=False, force=False) == "ok"
    assert "Сентябрь 2026" in format_plan(plans, []) or "сентябрь 2026" in format_plan(plans, [])


def test_plan_unmapped_and_idempotent():
    cells = month_block("A", 1, "Сентябрь", {"Зарплата": 100.0}, {"Бизнес": 50.0}, 100.0, 50.0)
    blocks, _ = parse_budget(*grid(cells), TODAY)
    existing = [Operation(date(2026, 9, 30), I, Decimal(100), "Зарплата", tags=["импорт"], id="old1")]
    plan = build_plan(blocks, DEFAULT_CATEGORIES, existing, TODAY)[0]
    assert [i.name for i in plan.unmapped] == ["Бизнес"]
    assert "НЕ СОПОСТАВЛЕНО" in format_plan([plan], [])
    assert "уже импортирован" in month_status(plan, replace=False, force=False)
    assert "несопоставленные" in month_status(plan, replace=True, force=True)
    plan = build_plan(blocks, DEFAULT_CATEGORIES, existing, TODAY, overrides={"Бизнес": "Прочее"})[0]
    assert plan.existing_import_ids == ["old1"]
    assert month_status(plan, replace=True, force=False) == "ok"


def test_memory_storage_batch():
    import asyncio
    s = MemoryStorage()
    ops = [Operation(date(2026, 9, 30), E, Decimal(1), "Досуг", tags=["импорт"]) for _ in range(3)]
    asyncio.run(s.add_operations(ops))
    assert asyncio.run(s.delete_operations([ops[0].id, ops[2].id, "nope"])) == 2
    assert [o.id for o in asyncio.run(s.list_operations())] == [ops[1].id]
