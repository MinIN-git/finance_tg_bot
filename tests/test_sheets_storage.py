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

    def __init__(self, title, cols=26):
        self.title = title
        self.id = next(self._ids)
        self.rows = []
        self.col_count = cols

    def add_cols(self, n):
        self.col_count += n

    def get_all_values(self, **kwargs):
        return [list(r) for r in self.rows]

    def append_row(self, values, value_input_option=None, table_range=None):
        self.rows.append([user_entered(v) for v in values])

    def append_rows(self, values, value_input_option=None, table_range=None):
        for row in values:
            self.append_row(row)

    def update(self, values, range_name, value_input_option=None):
        start = a1_row(range_name)
        col = ord(range_name[0]) - ord("A")
        assert col + max(len(r) for r in values) <= self.col_count, "exceeds grid limits"
        for i, row in enumerate(values):
            while len(self.rows) < start + i:
                self.rows.append([])
            target = self.rows[start + i - 1]
            target.extend([""] * (col + len(row) - len(target)))
            target[col:col + len(row)] = [user_entered(v) for v in row]

    def batch_update(self, data, value_input_option=None):
        for item in data:
            self.update(item["values"], item["range"], value_input_option=value_input_option)

    def update_acell(self, label, value):
        col = ord(label[0]) - ord("A")
        row = self.rows[a1_row(label) - 1]
        row.extend([""] * (col + 1 - len(row)))
        row[col] = user_entered(value)

    def row_values(self, row):
        return [str(v) for v in self.rows[row - 1]] if len(self.rows) >= row else []

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
        self.sheets[title] = FakeWorksheet(title, cols)
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
    assert set(sheets) == {"Лист1", "Операции", "Счета", "Категории", "Остатки", "Поездки"}
    assert sheets["Операции"].rows[0][-3:] == ["id", "Вид", "Статья поездки"]
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


def test_manual_rows_without_id_get_id(storage):
    """Строки, вписанные руками без id: бот вписывает id, и правка одной не задевает другую."""
    ws = storage._sh.sheets["Операции"]
    ws.rows.append(["01.09.2026", "расход", 100, "Досуг", "Карта", "", "кино"])
    ws.rows.append(["02.09.2026", "расход", 200, "Досуг", "Карта", "", "театр"])
    first, second = run(storage.list_operations())
    assert first.id and second.id and first.id != second.id
    assert [r[8] for r in ws.rows[1:]] == [first.id, second.id]

    assert run(storage.update_operation(Operation(**{**first.__dict__, "category": "Подарки"})))
    assert [r[3] for r in ws.rows[1:]] == ["Подарки", "Досуг"]
    assert run(storage.delete_operation(second.id)).comment == "театр"
    assert [r[6] for r in ws.rows[1:]] == ["кино"]


def test_empty_id_never_matches(storage):
    ws = storage._sh.sheets["Операции"]
    ws.rows.append(["01.09.2026", "расход", 100, "Досуг", "Карта", "", "кино", "", ""])
    assert run(storage.delete_operation("")) is None
    assert not run(storage.update_operation(Operation(date(2026, 9, 1), OpType.EXPENSE, Decimal(1), id="")))


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


def test_emoji_column_added_to_old_categories_sheet(monkeypatch):
    import gspread

    client = FakeClient()
    old = client.sh.add_worksheet("Категории", 1000, 3)       # лист старой версии: 3 колонки
    old.rows = [["Название", "Тип", "Ключевые слова"],
                ["Еда вне дома", "расход", "кофе"],
                ["Своя категория", "расход", ""],
                ["Зарплата", "доход", "зп"]]
    monkeypatch.setattr(gspread, "service_account", lambda filename: client)
    s = st.SheetsStorage("key.json", "sheet-id", cache_ttl=0)
    s.ensure_sheets()
    assert [r[3] for r in old.rows] == ["Эмодзи", "🍽", "", "💼"]
    cats = {c.name: c for c in run(s.list_categories())}
    assert cats["Еда вне дома"].emoji == "🍽" and cats["Еда вне дома"].label == "🍽 Еда вне дома"

    old.rows[1][3] = "☕"                                      # правка пользователя
    s.ensure_sheets()                                         # повторный запуск колонку не трогает
    assert old.rows[1][3] == "☕"


def test_big_purchases_columns_and_rows(storage):
    ws = storage._sh.add_worksheet("Большие покупки", 100, 5)
    ws.rows = [["Дата", "Что купил", "Стоимость", "Категория", "id"]]
    storage._worksheets = {}
    assert ws.rows[0] == ["Дата", "Что купил", "Стоимость", "Категория", "id"]
    run(storage.add_big_purchase(st.BigPurchase(date(2026, 9, 3), "отпуск турция", Decimal(283122), "Досуг", "op1")))
    run(storage.add_big_purchase(st.BigPurchase(date(2026, 9, 4), "диван", Decimal(50000), "", "op2")))
    got = run(storage.list_big_purchases())
    assert [(b.what, b.category, b.op_id) for b in got] == [("отпуск турция", "Досуг", "op1"), ("диван", "", "op2")]
    run(storage.set_big_purchase_category("op2", "Прочее"))
    run(storage.delete_big_purchase("op1"))
    assert [(b.what, b.category) for b in run(storage.list_big_purchases())] == [("диван", "Прочее")]


def test_old_big_sheet_gets_category_column(monkeypatch):
    import gspread

    client = FakeClient()
    old = client.sh.add_worksheet("Большие покупки", 100, 3)
    old.rows = [["Мои крупные траты", "", ""], ["Дата", "Что купил", "Стоимость"], [46280, "Отпуск в Турцию", 283122]]
    monkeypatch.setattr(gspread, "service_account", lambda filename: client)
    s = st.SheetsStorage("key.json", "sheet-id", cache_ttl=0)
    s.ensure_sheets()
    assert old.rows[1] == ["Дата", "Что купил", "Стоимость"]     # бот лист больше не трогает
    s.ensure_big_columns()                                       # только скрипт миграции
    assert old.rows[1] == ["Дата", "Что купил", "Стоимость", "Категория", "id"]
    old.rows[2] += ["Досуг"]                          # Иван вписал категорию
    [b] = run(s.list_big_purchases())
    assert (b.what, b.category, b.amount) == ("Отпуск в Турцию", "Досуг", 283122)


def test_old_operations_sheet_gets_kind_column(monkeypatch):
    import gspread

    client = FakeClient()
    old = client.sh.add_worksheet("Операции", 1000, 9)
    old.rows = [["Дата", "Тип", "Сумма", "Категория", "Счет", "Счет-получатель", "Комментарий", "Теги", "id"],
                ["2026-09-06", "расход", 283122, "Досуг", "Карта", "", "отпуск", "#тяжелое", "a1"],
                ["2026-09-07", "расход", 500, "Досуг", "Карта", "", "кино", "", "a2"]]
    monkeypatch.setattr(gspread, "service_account", lambda filename: client)
    s = st.SheetsStorage("key.json", "sheet-id", cache_ttl=0)
    s.ensure_sheets()
    assert old.rows[0][9] == "Вид"
    ops = run(s.list_operations())
    assert [o.kind.value for o in ops] == ["тяжелая", "обычная"]      # без «Вида» — по тегам
    assert old.rows[0][10] == "Статья поездки"
    ops[1] = Operation(**{**ops[1].__dict__, "kind": st.Kind.TRIP, "trip_item": ""})
    run(s.update_operation(ops[1]))
    assert old.rows[2][9:11] == ["командировка", "Прочее"]


def test_link_big_purchase_and_read_sheet(monkeypatch):
    import gspread

    client = FakeClient()
    big = client.sh.add_worksheet("Большие покупки", 100, 3)
    big.rows = [["Дата", "Что купил", "Стоимость"], [46271, "Отпуск в Турцию", 283122], [46250, "Кольцо", 77960]]
    client.sh.add_worksheet("Командировки", 10, 3).rows = [["13.06.2026 – 16.06.2026", ""], ["Кафешки", 100]]
    monkeypatch.setattr(gspread, "service_account", lambda filename: client)
    s = st.SheetsStorage("key.json", "sheet-id", cache_ttl=0)
    s.ensure_sheets()
    item = run(s.list_big_purchases())[0]
    assert run(s.link_big_purchase(item, "op9", "Досуг"))
    assert big.rows[1][3:5] == ["Досуг", "op9"] and big.rows[2][3:5] in ([], ["", ""])
    assert not run(s.link_big_purchase(item, "op9", "Досуг"))       # уже связана — второй раз не трогает
    formatted, raw = run(s.read_sheet("Командировки"))
    assert formatted[1] == ["Кафешки", "100"] or formatted[1][0] == "Кафешки"
    assert run(s.read_sheet("Нет такого")) is None


class _Resp:
    def __init__(self, code):
        self.code = code
        self.text = "quota"

    def json(self):
        return {"error": {"code": self.code, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}


def test_retry_on_429(storage):
    from gspread.exceptions import APIError

    slept, calls = [], {"n": 0}
    storage._sleep = slept.append

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise APIError(_Resp(429))
        return "ok"

    assert storage.call(flaky) == "ok" and slept == [5, 10]

    def always():
        raise APIError(_Resp(429))

    slept.clear()
    with pytest.raises(APIError):
        storage.call(always)
    assert slept == [5, 10, 20, 40, 60]
    with pytest.raises(APIError):     # другие ошибки — без повторов
        storage.call(lambda: (_ for _ in ()).throw(APIError(_Resp(403))))


def test_retry_only_when_request_not_sent(storage):
    import requests
    from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

    slept, calls = [], {"n": 0}
    storage._sleep = slept.append
    dns = requests.exceptions.ConnectionError(MaxRetryError(None, "/x", reason=NewConnectionError(None, "dns")))

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise dns
        return "ok"

    assert storage.call(flaky) == "ok" and slept == [2, 5]
    slept.clear()
    with pytest.raises(requests.exceptions.ConnectionError):
        storage.call(lambda: (_ for _ in ()).throw(dns))
    assert slept == [2, 5, 10]
    # оборвалось после отправки — повтор мог бы записать строку дважды, поэтому без повтора
    slept.clear()
    reset = requests.exceptions.ConnectionError(ProtocolError("Connection aborted"))
    with pytest.raises(requests.exceptions.ConnectionError):
        storage.call(lambda: (_ for _ in ()).throw(reset))
    assert slept == []


def test_update_operations_batch_without_rereading(storage):
    ops = [Operation(date(2026, 9, i + 1), OpType.EXPENSE, Decimal(i + 1), "Досуг", "Карта") for i in range(4)]
    run(storage.add_operations(ops))
    ws = storage._sh.sheets["Операции"]
    reads = {"n": 0}
    orig = ws.get_all_values

    def counting(**kw):
        reads["n"] += 1
        return orig(**kw)

    ws.get_all_values = counting
    changed = [Operation(**{**o.__dict__, "amount": o.amount * 10}) for o in ops]
    assert run(storage.update_operations(changed)) == 4
    assert reads["n"] == 0                       # лист не перечитывался
    storage._ops = None
    assert [o.amount for o in run(storage.list_operations())] == [10, 20, 30, 40]


def test_worksheet_objects_cached(storage):
    calls = {"n": 0}
    orig = storage._sh.worksheets

    def counting():
        calls["n"] += 1
        return orig()

    storage._sh.worksheets = counting
    storage._worksheets = {}
    for _ in range(5):
        storage._ws("Операции")
        storage._ws("Категории")
    assert calls["n"] == 1
    with pytest.raises(Exception):
        storage._ws("Нет такого листа")
