"""Хранилище: интерфейс, реализация в памяти (тесты, локальная проба) и Google Sheets."""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .categories import DEFAULT_CATEGORIES
from .models import Account, BalanceEntry, BigPurchase, Category, Operation, OpType

log = logging.getLogger(__name__)

DEFAULT_ACCOUNTS = [
    Account("Карта", "Сколько на карте?"),
    Account("Вклад", "Сколько на вкладе?"),
    Account("Наличные", "Сколько наличными?"),
]

SHEET_OPERATIONS = "Операции"
SHEET_ACCOUNTS = "Счета"
SHEET_CATEGORIES = "Категории"
SHEET_BALANCES = "Остатки"
SHEET_BIG = "Большие покупки"

HEADERS = {
    SHEET_OPERATIONS: ["Дата", "Тип", "Сумма", "Категория", "Счет", "Счет-получатель", "Комментарий", "Теги", "id"],
    SHEET_ACCOUNTS: ["Название", "Вопрос для ежемесячного снимка"],
    SHEET_CATEGORIES: ["Название", "Тип", "Ключевые слова", "Эмодзи"],
    SHEET_BALANCES: ["Дата", "Счет", "Сумма"],
    SHEET_BIG: ["Дата", "Что купил", "Стоимость"],
}


class Storage(ABC):
    @abstractmethod
    async def list_operations(self) -> list[Operation]:
        """Операции в порядке добавления (последняя добавленная — в конце)."""

    @abstractmethod
    async def add_operation(self, op: Operation) -> None: ...

    @abstractmethod
    async def update_operation(self, op: Operation) -> bool: ...

    @abstractmethod
    async def delete_operation(self, op_id: str) -> Operation | None: ...

    async def add_operations(self, ops: list[Operation]) -> None:
        """Пакетное добавление (по умолчанию — по одной)."""
        for op in ops:
            await self.add_operation(op)

    async def delete_operations(self, op_ids: list[str]) -> int:
        """Пакетное удаление; возвращает число удаленных."""
        deleted = 0
        for op_id in op_ids:
            if await self.delete_operation(op_id) is not None:
                deleted += 1
        return deleted

    @abstractmethod
    async def list_categories(self) -> list[Category]: ...

    @abstractmethod
    async def add_keyword(self, category: str, op_type: OpType, keyword: str) -> None:
        """Привязывает ключевое слово к категории и убирает его у других категорий того же типа."""

    @abstractmethod
    async def list_accounts(self) -> list[Account]:
        """Счета в порядке листа «Счета»; первый — счет по умолчанию."""

    @abstractmethod
    async def list_balances(self) -> list[BalanceEntry]: ...

    @abstractmethod
    async def add_balance(self, entry: BalanceEntry) -> None:
        """Добавляет остаток; запись за ту же дату и тот же счет заменяется."""

    async def list_big_purchases(self) -> list[BigPurchase]:
        """Разовые крупные траты (лист «Большие покупки»)."""
        return []

    async def refresh(self) -> None:
        """Сбросить кэш (если он есть)."""


class MemoryStorage(Storage):
    def __init__(
        self,
        operations: list[Operation] | None = None,
        categories: list[Category] | None = None,
        accounts: list[Account] | None = None,
        balances: list[BalanceEntry] | None = None,
        big_purchases: list[BigPurchase] | None = None,
    ) -> None:
        self.operations = list(operations or [])
        self.categories = copy.deepcopy(categories if categories is not None else DEFAULT_CATEGORIES)
        self.accounts = copy.deepcopy(accounts if accounts is not None else DEFAULT_ACCOUNTS)
        self.balances = list(balances or [])
        self.big_purchases = list(big_purchases or [])

    async def list_operations(self) -> list[Operation]:
        return list(self.operations)

    async def add_operation(self, op: Operation) -> None:
        self.operations.append(op)

    async def update_operation(self, op: Operation) -> bool:
        for i, existing in enumerate(self.operations):
            if existing.id == op.id:
                self.operations[i] = op
                return True
        return False

    async def delete_operation(self, op_id: str) -> Operation | None:
        for i, op in enumerate(self.operations):
            if op.id == op_id:
                return self.operations.pop(i)
        return None

    async def list_categories(self) -> list[Category]:
        return copy.deepcopy(self.categories)

    async def add_keyword(self, category: str, op_type: OpType, keyword: str) -> None:
        for c in self.categories:
            if c.type != op_type:
                continue
            if c.name == category:
                if keyword not in c.keywords:
                    c.keywords.append(keyword)
            elif keyword in c.keywords:
                c.keywords.remove(keyword)

    async def list_accounts(self) -> list[Account]:
        return copy.deepcopy(self.accounts)

    async def list_balances(self) -> list[BalanceEntry]:
        return list(self.balances)

    async def list_big_purchases(self) -> list[BigPurchase]:
        return list(self.big_purchases)

    async def add_balance(self, entry: BalanceEntry) -> None:
        self.balances = [
            e for e in self.balances if (e.date, e.account) != (entry.date, entry.account)
        ] + [entry]


# ---------- конвертация ячеек ----------

SHEETS_EPOCH = date(1899, 12, 30)


def cell_to_date(value: Any) -> date | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return SHEETS_EPOCH + timedelta(days=int(value))
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def cell_to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def number_for_sheet(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def text_for_sheet(value: str) -> str:
    """Апостроф в начале: Sheets (USER_ENTERED) сохранит строку как текст,
    а не превратит «5.10» в дату, «1e5» в число или «=...» в формулу. Сам апостроф не виден."""
    return f"'{value}" if value else ""


def op_to_row(op: Operation) -> list[Any]:
    return [
        op.date.isoformat(),
        op.type.value,
        number_for_sheet(op.amount),
        text_for_sheet(op.category),
        text_for_sheet(op.account),
        text_for_sheet(op.to_account),
        text_for_sheet(op.comment),
        " ".join(f"#{t}" for t in op.tags),
        text_for_sheet(op.id),
    ]


def row_to_op(row: list[Any]) -> Operation | None:
    row = list(row) + [""] * (9 - len(row))
    d = cell_to_date(row[0])
    amount = cell_to_decimal(row[2])
    try:
        op_type = OpType(str(row[1]).strip().lower())
    except ValueError:
        return None
    if d is None or amount is None:
        return None
    tags = [t.lstrip("#").lower() for t in str(row[7]).split() if t.strip("#")]
    return Operation(
        date=d, type=op_type, amount=abs(amount), category=str(row[3]).strip(),
        account=str(row[4]).strip(), to_account=str(row[5]).strip(), comment=str(row[6]).strip(),
        tags=tags, id=str(row[8]).strip(),
    )


def _month_cell(value: Any) -> date | None:
    """«09.2026» / «сентябрь 2026» нет — только дата или «мм.гггг»."""
    m = re.fullmatch(r"(\d{1,2})\.(\d{4})", str(value).strip())
    if m and 1 <= int(m.group(1)) <= 12:
        return date(int(m.group(2)), int(m.group(1)), 1)
    return None


def parse_big_purchases(formatted: list[list[Any]], raw: list[list[Any]]) -> list[BigPurchase]:
    """Лист «Большие покупки»: ищет строку заголовка с «Дата» и «Стоимость» (или «Сумма»),
    колонка описания — «Что купил»/«Покупка»/«Название»; читает строки ниже."""
    def norm(v: Any) -> str:
        return str(v).strip().lower().replace("ё", "е")

    header = date_col = cost_col = what_col = None
    for r, row in enumerate(formatted):
        cells = [norm(v) for v in row]
        d = next((i for i, v in enumerate(cells) if v.startswith("дата")), None)
        c = next((i for i, v in enumerate(cells) if v.startswith(("стоимост", "сумма", "цена"))), None)
        if d is not None and c is not None:
            header, date_col, cost_col = r, d, c
            what_col = next(
                (i for i, v in enumerate(cells) if i not in (d, c) and v.startswith(("что", "покуп", "назван", "описан"))),
                None,
            )
            break
    if header is None:
        return []
    out = []
    for r in range(header + 1, len(formatted)):
        def cell(grid, c):
            row = grid[r] if r < len(grid) else []
            return row[c] if c is not None and c < len(row) else ""
        d = cell(raw, date_col)
        when = cell_to_date(d) if str(d).strip() else None
        when = when or _month_cell(cell(formatted, date_col)) or cell_to_date(cell(formatted, date_col))
        amount = cell_to_decimal(cell(raw, cost_col)) if str(cell(raw, cost_col)).strip() else None
        if when is None or amount is None or amount <= 0:
            continue
        out.append(BigPurchase(when, str(cell(formatted, what_col)).strip(), amount))
    return out


class SheetsStorage(Storage):
    """Google Sheets как источник истины + кэш в памяти с TTL.

    Все вызовы gspread синхронные, поэтому выполняются в отдельном потоке.
    """

    def __init__(
        self,
        credentials_file: str,
        spreadsheet_id: str,
        cache_ttl: int = 300,
    ) -> None:
        import gspread

        self._gc = gspread.service_account(filename=credentials_file)
        self._sh = self._gc.open_by_key(spreadsheet_id)
        self._ttl = cache_ttl
        self._lock = asyncio.Lock()
        self._ops: list[Operation] | None = None
        self._ops_loaded_at = 0.0
        self._cats: list[Category] | None = None
        self._accounts: list[Account] | None = None

    # ----- инициализация -----

    def ensure_sheets(self) -> None:
        """Создает недостающие листы с заголовками и заполняет справочники по умолчанию.
        Существующие листы не трогает."""
        existing = {ws.title for ws in self._sh.worksheets()}
        for title, header in HEADERS.items():
            if title in existing:
                continue
            log.info("Создаю лист «%s»", title)
            ws = self._sh.add_worksheet(title=title, rows=1000, cols=len(header))
            rows: list[list[Any]] = [header]
            if title == SHEET_CATEGORIES:
                rows += [[c.name, c.type.value, ", ".join(c.keywords), c.emoji] for c in DEFAULT_CATEGORIES]
            elif title == SHEET_ACCOUNTS:
                rows += [[a.name, a.question] for a in DEFAULT_ACCOUNTS]
            ws.update(rows, "A1", value_input_option="USER_ENTERED")
            ws.freeze(rows=1)
        if SHEET_CATEGORIES in existing:
            self._add_emoji_column()

    def _add_emoji_column(self) -> None:
        """Для листа «Категории», созданного старой версией: дописать колонку «Эмодзи» с эмодзи
        по умолчанию. Если колонка уже есть, ничего не меняет (правки пользователя сохраняются)."""
        ws = self._ws(SHEET_CATEGORIES)
        rows = ws.get_all_values()
        if not rows or (len(rows[0]) > 3 and str(rows[0][3]).strip()):
            return
        defaults = {(c.name, c.type.value): c.emoji for c in DEFAULT_CATEGORIES}
        column = [["Эмодзи"]]
        for r in rows[1:]:
            r = list(r) + ["", "", "", ""]
            current = str(r[3]).strip()
            column.append([current or defaults.get((str(r[0]).strip(), str(r[1]).strip().lower()), "")])
        if ws.col_count < 4:
            ws.add_cols(4 - ws.col_count)
        log.info("Добавляю колонку «Эмодзи» в лист «Категории»")
        ws.update(column, f"D1:D{len(column)}", value_input_option="RAW")

    def _ws(self, title: str):
        return self._sh.worksheet(title)

    def _read(self, title: str) -> list[list[Any]]:
        from gspread.utils import DateTimeOption, ValueRenderOption

        values = self._ws(title).get_all_values(
            value_render_option=ValueRenderOption.unformatted,
            date_time_render_option=DateTimeOption.serial_number,
        )
        return values[1:]  # без заголовка

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ----- операции -----

    async def _ensure_ops(self) -> list[Operation]:
        if self._ops is None or time.monotonic() - self._ops_loaded_at > self._ttl:
            rows = await self._run(self._read, SHEET_OPERATIONS)
            ops = [op for op in (row_to_op(r) for r in rows) if op is not None]
            self._ops = ops
            self._ops_loaded_at = time.monotonic()
        return self._ops

    async def list_operations(self) -> list[Operation]:
        async with self._lock:
            return list(await self._ensure_ops())

    async def add_operation(self, op: Operation) -> None:
        async with self._lock:
            ops = await self._ensure_ops()
            await self._run(
                self._ws(SHEET_OPERATIONS).append_row,
                op_to_row(op), value_input_option="USER_ENTERED", table_range="A1",
            )
            ops.append(op)

    async def add_operations(self, ops: list[Operation]) -> None:
        if not ops:
            return
        async with self._lock:
            cached = await self._ensure_ops()
            await self._run(
                self._ws(SHEET_OPERATIONS).append_rows,
                [op_to_row(op) for op in ops], value_input_option="USER_ENTERED", table_range="A1",
            )
            cached.extend(ops)

    async def delete_operations(self, op_ids: list[str]) -> int:
        if not op_ids:
            return 0
        async with self._lock:
            ws = self._ws(SHEET_OPERATIONS)
            ids = await self._run(ws.col_values, 9)
            wanted = set(op_ids)
            rows = [i + 1 for i, v in enumerate(ids) if i > 0 and v in wanted]
            if rows:
                # снизу вверх, чтобы номера строк не сдвигались
                requests = [
                    {"deleteDimension": {"range": {
                        "sheetId": ws.id, "dimension": "ROWS", "startIndex": r - 1, "endIndex": r,
                    }}}
                    for r in sorted(rows, reverse=True)
                ]
                await self._run(self._sh.batch_update, {"requests": requests})
            self._ops = None
            return len(rows)

    def read_grid(self, title: str) -> tuple[list[list[Any]], list[list[Any]]]:
        """Произвольный лист целиком: (отформатированные значения, «сырые» значения)."""
        from gspread.utils import DateTimeOption, ValueRenderOption

        ws = self._ws(title)
        formatted = ws.get_all_values()
        raw = ws.get_all_values(
            value_render_option=ValueRenderOption.unformatted,
            date_time_render_option=DateTimeOption.serial_number,
        )
        return formatted, raw

    def _find_row(self, op_id: str) -> int | None:
        ids = self._ws(SHEET_OPERATIONS).col_values(9)
        for idx in range(len(ids) - 1, 0, -1):
            if ids[idx] == op_id:
                return idx + 1  # строки в Sheets нумеруются с 1
        return None

    async def update_operation(self, op: Operation) -> bool:
        async with self._lock:
            row = await self._run(self._find_row, op.id)
            if row is None:
                return False
            await self._run(
                self._ws(SHEET_OPERATIONS).update, [op_to_row(op)], f"A{row}:I{row}",
                value_input_option="USER_ENTERED",
            )
            ops = await self._ensure_ops()
            for i, existing in enumerate(ops):
                if existing.id == op.id:
                    ops[i] = op
            return True

    async def delete_operation(self, op_id: str) -> Operation | None:
        async with self._lock:
            ops = await self._ensure_ops()
            row = await self._run(self._find_row, op_id)
            if row is None:
                return None
            await self._run(self._ws(SHEET_OPERATIONS).delete_rows, row)
            for i, op in enumerate(ops):
                if op.id == op_id:
                    return ops.pop(i)
            return Operation(date=date.today(), type=OpType.EXPENSE, amount=Decimal(0), id=op_id)

    # ----- справочники -----

    async def list_categories(self) -> list[Category]:
        if self._cats is None:
            rows = await self._run(self._read, SHEET_CATEGORIES)
            cats = []
            for r in rows:
                r = list(r) + ["", "", "", ""]
                name = str(r[0]).strip()
                try:
                    op_type = OpType(str(r[1]).strip().lower())
                except ValueError:
                    continue
                if name:
                    kws = [k.strip() for k in str(r[2]).split(",") if k.strip()]
                    cats.append(Category(name, op_type, kws, str(r[3]).strip()))
            self._cats = cats
        return copy.deepcopy(self._cats)

    async def add_keyword(self, category: str, op_type: OpType, keyword: str) -> None:
        async with self._lock:
            ws = self._ws(SHEET_CATEGORIES)
            rows = await self._run(ws.get_all_values)
            for idx, r in enumerate(rows[1:], start=2):
                r = list(r) + ["", "", ""]
                if r[1].strip().lower() != op_type.value:
                    continue
                kws = [k.strip() for k in r[2].split(",") if k.strip()]
                if r[0].strip() == category:
                    new = kws if keyword in kws else [*kws, keyword]
                else:
                    new = [k for k in kws if k != keyword]
                if new != kws:
                    await self._run(ws.update_acell, f"C{idx}", ", ".join(new))
            self._cats = None

    async def list_accounts(self) -> list[Account]:
        if self._accounts is None:
            rows = await self._run(self._read, SHEET_ACCOUNTS)
            self._accounts = [
                Account(str(r[0]).strip(), str(r[1]).strip() if len(r) > 1 else "")
                for r in rows if r and str(r[0]).strip()
            ]
        return copy.deepcopy(self._accounts)

    # ----- остатки по счетам -----

    async def list_balances(self) -> list[BalanceEntry]:
        rows = await self._run(self._read, SHEET_BALANCES)
        out = []
        for r in rows:
            if len(r) < 3:
                continue
            d, account, amount = cell_to_date(r[0]), str(r[1]).strip(), cell_to_decimal(r[2])
            if d and account and amount is not None:
                out.append(BalanceEntry(d, account, amount))
        return out

    async def add_balance(self, entry: BalanceEntry) -> None:
        async with self._lock:
            ws = self._ws(SHEET_BALANCES)
            row = [entry.date.isoformat(), text_for_sheet(entry.account), number_for_sheet(entry.amount)]
            rows = await self._run(self._read, SHEET_BALANCES)
            for idx, r in enumerate(rows, start=2):
                if len(r) >= 2 and cell_to_date(r[0]) == entry.date and str(r[1]).strip() == entry.account:
                    await self._run(ws.update, [row], f"A{idx}:C{idx}", value_input_option="USER_ENTERED")
                    return
            await self._run(ws.append_row, row, value_input_option="USER_ENTERED", table_range="A1")

    # ----- большие покупки -----

    async def list_big_purchases(self) -> list[BigPurchase]:
        try:
            formatted, raw = await self._run(self.read_grid, SHEET_BIG)
        except Exception:
            log.warning("Не удалось прочитать лист «%s»", SHEET_BIG, exc_info=True)
            return []
        return parse_big_purchases(formatted, raw)

    async def refresh(self) -> None:
        async with self._lock:
            self._ops = None
            self._cats = None
            self._accounts = None
