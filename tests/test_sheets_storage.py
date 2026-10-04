"""SheetsStorage против поддельного gspread, который ведет себя как USER_ENTERED в Google Sheets."""
import asyncio
import re
from datetime import date
from decimal import Decimal

import pytest

from bot import storage as st
from bot.models import BalanceEntry, Operation, OpType

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def user_entered(value):
    if isinstance(value, str):
        if value.startswith("'"):
            return value[1:]
        if ISO_RE.match(value):
            return (date.fromisoformat(value) - st.SHEETS_EPOCH).days
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    return value


def a1_row(label):
    return int(re.search(r"\d+", label).group())


class FakeWorksheet:
    _ids = iter(range(1, 1000))

    def __init__(self, title):
        self.title = title
        self.id = next(self._ids)
        self.rows = []

    def get_all_values(self, **kwargs):
        return [list(r) for r in self.rows]

    def append_row(self, values, value_input_option=None, table_range=None):
        self.rows.append([user_entered(v) for v in values])

    def append_rows(self, values, value_input_option=None, table_range=None):
        for row in values:
            self.append_row(row)

    def update(self, values, range_name, value_input_option=None):
        start = a1_row(range_name)
        for i, row in enumerate(values):
            while len(self.rows) < start + i:
                self.rows.append([])
            self.rows[start + i - 1] = [user_entered(v) for v in row]

    def update_acell(self, label, value):
        col = ord(label[0]) - ord("A")
        row = self.rows[a1_row(label) - 1]
        row.extend([""] * (col + 1 - len(row)))
        row[col] = user_entered(value)

    def col_values(self, col):
        return [str(r[col - 1]) if len(r) >= col else "" for r in self.rows]

    def delete_rows(self, idx):
        del self.rows[idx - 1]

    def freeze(self, rows=None):
        pass


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = {"Лист1": FakeWorksheet("Лист1")}

    def worksheets(self):
        return list(self.sheets.values())

    def worksheet(self, title):
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]

    def batch_update(self, body):
        for req in body["requests"]:
            rng = req["deleteDimension"]["range"]
            ws = next(w for w in self.sheets.values() if w.id == rng["sheetId"])
            del ws.rows[rng["startIndex"]:rng["endIndex"]]


class FakeClient:
    def __init__(self):
        self.sh = FakeSpreadsheet()

    def open_by_key(self, key):
        return self.sh


@pytest.fixture
def storage(monkeypatch):
    import gspread

    client = FakeClient()
    monkeypatch.setattr(gspread, "service_account", lambda filename: client)
    s = st.SheetsStorage("key.json", "sheet-id", cache_ttl=0)
    s.ensure_sheets()
    return s


def run(coro):
    return asyncio.run(coro)


def test_ensure_sheets_creates_and_seeds(storage):
    sheets = storage._sh.sheets
    assert set(sheets) == {"Лист1", "Операции", "Счета", "Категории", "Остатки"}
    assert sheets["Операции"].rows[0][-1] == "id"
    accounts = run(storage.list_accounts())
    assert [(a.name, a.prompt) for a in accounts][0] == ("Карта", "Сколько на карте?")
    cats = run(storage.list_categories())
    assert any(c.name == "Еда вне дома" and "кофе" in c.keywords for c in cats)
    sheets["Счета"].rows.append(["Тинькофф", ""])
    storage.ensure_sheets()  # повторный запуск не трогает существующие листы
    assert sheets["Счета"].rows[-1][0] == "Тинькофф"


def test_operations_roundtrip(storage):
    ops = [
        Operation(date(2026, 10, 4), OpType.EXPENSE, Decimal("99.9"), "Продукты домой", "Карта", "", "5.10", ["командировка"]),
        Operation(date(2026, 10, 3), OpType.INCOME, Decimal(150000), "Зарплата", "Карта", "", "=зп", [], id="1234567890"),
        Operation(date(2026, 10, 2), OpType.TRANSFER, Decimal(5000), "", "Карта", "Вклад"),
    ]
    for op in ops:
        run(storage.add_operation(op))
    storage._ops = None  # перечитать из «таблицы»
    assert run(storage.list_operations()) == ops

    changed = Operation(**{**ops[0].__dict__, "category": "Досуг"})
    assert run(storage.update_operation(changed))
    storage._ops = None
    assert run(storage.list_operations())[0].category == "Досуг"

    assert run(storage.delete_operation(ops[1].id)).amount == 150000
    storage._ops = None
    assert [o.id for o in run(storage.list_operations())] == [ops[0].id, ops[2].id]
    assert run(storage.delete_operation("nope")) is None


def test_add_keyword_moves_between_categories(storage):
    run(storage.add_keyword("Досуг", OpType.EXPENSE, "хинкали"))
    run(storage.add_keyword("Еда вне дома", OpType.EXPENSE, "хинкали"))
    cats = {(c.name, c.type): c for c in run(storage.list_categories())}
    assert "хинкали" in cats[("Еда вне дома", OpType.EXPENSE)].keywords
    assert "хинкали" not in cats[("Досуг", OpType.EXPENSE)].keywords


def test_balances_replace_same_day(storage):
    run(storage.add_balance(BalanceEntry(date(2026, 10, 1), "Карта", Decimal(100))))
    run(storage.add_balance(BalanceEntry(date(2026, 10, 1), "Вклад", Decimal(200))))
    run(storage.add_balance(BalanceEntry(date(2026, 10, 1), "Карта", Decimal(150))))
    got = sorted((e.account, e.amount) for e in run(storage.list_balances()))
    assert got == [("Вклад", 200), ("Карта", 150)]


def test_manual_rows_with_strings(storage):
    ws = storage._sh.sheets["Операции"]
    ws.rows.append(["01.09.2026", "Расход", "1 200,50 ₽", "Досуг", "Карта", "", "кино", "", "x1"])
    ws.rows.append(["мусор"])
    ops = run(storage.list_operations())
    assert [(o.date, o.amount, o.type) for o in ops] == [(date(2026, 9, 1), Decimal("1200.50"), OpType.EXPENSE)]


def test_batch_add_and_delete(storage):
    ops = [Operation(date(2026, 9, 30), OpType.EXPENSE, Decimal(i + 1), "Досуг", "Карта", tags=["импорт"]) for i in range(5)]
    run(storage.add_operations(ops))
    assert run(storage.delete_operations([ops[1].id, ops[3].id])) == 2
    storage._ops = None
    assert [o.amount for o in run(storage.list_operations())] == [1, 3, 5]


def test_read_grid(storage):
    storage._sh.sheets["Лист1"].rows = [["Сентябрь", ""], ["Доходы", "Сумма"]]
    formatted, raw = storage.read_grid("Лист1")
    assert formatted[1] == ["Доходы", "Сумма"]
