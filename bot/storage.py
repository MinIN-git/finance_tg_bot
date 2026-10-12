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
from .models import (
    Account, BalanceEntry, Category, Kind, Operation, OpType, Trip, kind_from_tags, new_op_id,
)

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
SHEET_TRIPS = "Поездки"

HEADERS = {
    SHEET_OPERATIONS: ["Дата", "Тип", "Сумма", "Категория", "Счет", "Счет-получатель", "Комментарий", "Теги", "id", "Вид",
                       "Статья поездки"],
    SHEET_ACCOUNTS: ["Название", "Вопрос для ежемесячного снимка"],
    SHEET_CATEGORIES: ["Название", "Тип", "Ключевые слова", "Эмодзи"],
    SHEET_BALANCES: ["Дата", "Счет", "Сумма"],
    SHEET_TRIPS: ["Город", "Начало", "Конец", "id"],
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

    async def list_trips(self) -> list[Trip]:
        return []

    async def update_operations(self, ops: list[Operation]) -> int:
        n = 0
        for op in ops:
            n += await self.update_operation(op)
        return n

    async def add_trip(self, trip: Trip) -> None:
        raise NotImplementedError

    async def delete_trip(self, trip_id: str) -> None:
        raise NotImplementedError

    async def refresh(self) -> None:
        """Сбросить кэш (если он есть)."""


class MemoryStorage(Storage):
    def __init__(
        self,
        operations: list[Operation] | None = None,
        categories: list[Category] | None = None,
        accounts: list[Account] | None = None,
        balances: list[BalanceEntry] | None = None,
        trips: list[Trip] | None = None,
    ) -> None:
        self.operations = list(operations or [])
        self.categories = copy.deepcopy(categories if categories is not None else DEFAULT_CATEGORIES)
        self.accounts = copy.deepcopy(accounts if accounts is not None else DEFAULT_ACCOUNTS)
        self.balances = list(balances or [])
        self.trips = list(trips or [])

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

    async def list_trips(self) -> list[Trip]:
        return list(self.trips)

    async def add_trip(self, trip: Trip) -> None:
        self.trips.append(trip)

    async def delete_trip(self, trip_id: str) -> None:
        self.trips = [t for t in self.trips if t.id != trip_id]

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
        op.kind.value,
        op.trip_item,
    ]


def row_to_op(row: list[Any]) -> Operation | None:
    row = list(row) + [""] * (11 - len(row))
    d = cell_to_date(row[0])
    amount = cell_to_decimal(row[2])
    try:
        op_type = OpType(str(row[1]).strip().lower())
    except ValueError:
        return None
    if d is None or amount is None:
        return None
    tags = [t.lstrip("#").lower() for t in str(row[7]).split() if t.strip("#")]
    try:
        kind = Kind(str(row[9]).strip().lower())
    except ValueError:
        kind = kind_from_tags(tags)
    if op_type != OpType.EXPENSE:
        kind = Kind.REGULAR
    return Operation(
        date=d, type=op_type, amount=abs(amount), category=str(row[3]).strip(),
        account=str(row[4]).strip(), to_account=str(row[5]).strip(), comment=str(row[6]).strip(),
        tags=tags, id=str(row[8]).strip(), kind=kind, trip_item=str(row[10]).strip(),
    )


def _not_sent(e: BaseException) -> bool:
    """Запрос не ушел в Google: не разрешилось имя или не установилось соединение."""
    import requests
    from urllib3.exceptions import NewConnectionError   # в т.ч. NameResolutionError

    if isinstance(e, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(e, requests.exceptions.ConnectionError):
        return False
    reason = getattr(e.args[0], "reason", None) if e.args else None
    return isinstance(reason, NewConnectionError)


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
        self._sleep = time.sleep
        self._worksheets: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._ops: list[Operation] | None = None
        self._ops_loaded_at = 0.0
        self._cats: list[Category] | None = None
        self._accounts: list[Account] | None = None
        self._trips: list[Trip] | None = None

    # ----- инициализация -----

    def ensure_sheets(self) -> None:
        """Создает недостающие листы с заголовками и заполняет справочники по умолчанию.
        Существующие листы не трогает."""
        self._worksheets = {ws.title: ws for ws in self._sh.worksheets()}
        existing = set(self._worksheets)
        for title, header in HEADERS.items():
            if title in existing:
                continue
            log.info("Создаю лист «%s»", title)
            ws = self._sh.add_worksheet(title=title, rows=1000, cols=len(header))
            self._worksheets[title] = ws
            rows: list[list[Any]] = [header]
            if title == SHEET_CATEGORIES:
                rows += [[c.name, c.type.value, ", ".join(c.keywords), c.emoji] for c in DEFAULT_CATEGORIES]
            elif title == SHEET_ACCOUNTS:
                rows += [[a.name, a.question] for a in DEFAULT_ACCOUNTS]
            ws.update(rows, "A1", value_input_option="USER_ENTERED")
            ws.freeze(rows=1)
        if SHEET_CATEGORIES in existing:
            self._add_emoji_column()
        if SHEET_OPERATIONS in existing:
            self._add_kind_column()

    def _add_kind_column(self) -> None:
        """Для «Операций» старой версии: дописать заголовки «Вид» (J) и «Статья поездки» (K). Пустые значения
        читаются по тегам и меткам, а при следующей записи строки заполняются."""
        ws = self._ws(SHEET_OPERATIONS)
        header = ws.row_values(1)
        wanted = HEADERS[SHEET_OPERATIONS]
        if len(header) >= len(wanted) and all(str(h).strip() for h in header[9:len(wanted)]):
            return
        if ws.col_count < len(wanted):
            ws.add_cols(len(wanted) - ws.col_count)
        log.info("Добавляю колонки «Вид» и «Статья поездки» в лист «Операции»")
        ws.update([wanted[9:]], "J1:K1", value_input_option="RAW")

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
        """Объект листа из кэша: gspread.worksheet() каждый раз читает метаданные таблицы,
        а это отдельный запрос в квоту чтений."""
        ws = self._worksheets.get(title)
        if ws is None:
            self._worksheets = {w.title: w for w in self._sh.worksheets()}
            ws = self._worksheets.get(title)
            if ws is None:
                from gspread.exceptions import WorksheetNotFound

                raise WorksheetNotFound(title)
        return ws

    def _read(self, title: str) -> list[list[Any]]:
        from gspread.utils import DateTimeOption, ValueRenderOption

        values = self._ws(title).get_all_values(
            value_render_option=ValueRenderOption.unformatted,
            date_time_render_option=DateTimeOption.serial_number,
        )
        return values[1:]  # без заголовка

    RETRY_DELAYS = (5, 10, 20, 40, 60)   # паузы при 429 (квота Google Sheets на запросы в минуту)

    NETWORK_RETRY_DELAYS = (2, 5, 10)   # паузы, если до Google не удалось даже подключиться (DNS, соединение)

    def call(self, fn, *args, **kwargs):
        """Синхронный вызов gspread с повтором при 429 «Quota exceeded» (5, 10, 20, 40, 60 с) и при сбое
        подключения (2, 5, 10 с). Повторяется только то, что точно не дошло до Google, — без риска записать дважды."""
        from gspread.exceptions import APIError

        quota = iter(self.RETRY_DELAYS)
        network = iter(self.NETWORK_RETRY_DELAYS)
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn(*args, **kwargs)
            except APIError as e:
                delay = next(quota, None) if getattr(e, "code", None) == 429 else None
                if delay is None:
                    raise
                log.warning("Google Sheets: превышена квота (429), повтор через %s с (попытка %s)", delay, attempt)
            except Exception as e:
                delay = next(network, None) if _not_sent(e) else None
                if delay is None:
                    raise
                log.warning("Google Sheets: не удалось подключиться (%s), повтор через %s с", type(e).__name__, delay)
            self._sleep(delay)

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(self.call, fn, *args, **kwargs)

    # ----- операции -----

    async def _ensure_ops(self) -> list[Operation]:
        if self._ops is None or time.monotonic() - self._ops_loaded_at > self._ttl:
            rows = await self._run(self._read, SHEET_OPERATIONS)
            ops, missing = [], []
            for i, r in enumerate(rows):
                op = row_to_op(r)
                if op is None:
                    continue
                if not op.id:   # строка вписана в таблицу руками: без id ее нельзя найти для правки и удаления
                    op.id = new_op_id()
                    missing.append((i + 2, op.id))   # +1 за заголовок, +1 — строки с 1
                ops.append(op)
            if missing:
                await self._write_ids(missing)
            self._ops = ops
            self._ops_loaded_at = time.monotonic()
        return self._ops

    async def _write_ids(self, missing: list[tuple[int, str]]) -> None:
        data = [{"range": f"I{row}", "values": [[text_for_sheet(op_id)]]} for row, op_id in missing]
        try:
            await self._run(self._ws(SHEET_OPERATIONS).batch_update, data, value_input_option="USER_ENTERED")
            log.info("Вписал id в %s строк листа «%s», добавленных вручную", len(missing), SHEET_OPERATIONS)
        except Exception:
            log.warning("Не удалось вписать id в строки, добавленные вручную", exc_info=True)

    async def list_operations(self) -> list[Operation]:
        async with self._lock:
            return list(await self._ensure_ops())

    async def add_operation(self, op: Operation) -> None:
        await self.add_operations([op])

    async def add_operations(self, ops: list[Operation]) -> None:
        """Одна запись в лист; кэш дополняется без перечитывания."""
        if not ops:
            return
        async with self._lock:
            await self._run(
                self._ws(SHEET_OPERATIONS).append_rows,
                [op_to_row(op) for op in ops], value_input_option="USER_ENTERED", table_range="A1",
            )
            if self._ops is not None:
                self._ops.extend(ops)

    def _find_row(self, op_id: str) -> int | None:
        if not op_id:
            return None
        ids = self._ws(SHEET_OPERATIONS).col_values(9)
        for idx in range(len(ids) - 1, 0, -1):
            if ids[idx] == op_id:
                return idx + 1  # строки в Sheets нумеруются с 1
        return None

    async def update_operation(self, op: Operation) -> bool:
        return await self.update_operations([op]) == 1

    async def update_operations(self, ops: list[Operation]) -> int:
        """Одно чтение колонки id и одна пакетная запись; кэш правится без перечитывания листа."""
        if not ops:
            return 0
        async with self._lock:
            ws = self._ws(SHEET_OPERATIONS)
            ids = await self._run(ws.col_values, 9)
            row_of = {v: i + 1 for i, v in enumerate(ids) if i > 0 and v}
            data = [{"range": f"A{row_of[op.id]}:K{row_of[op.id]}", "values": [op_to_row(op)]}
                    for op in ops if op.id in row_of]
            if data:
                await self._run(ws.batch_update, data, value_input_option="USER_ENTERED")
            if self._ops is not None:
                updated = {op.id: op for op in ops if op.id in row_of}
                self._ops = [updated.get(o.id, o) for o in self._ops]
            return len(data)

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

    # ----- поездки -----

    async def list_trips(self) -> list[Trip]:
        if self._trips is None:
            rows = await self._run(self._read, SHEET_TRIPS)
            trips = []
            for r in rows:
                r = list(r) + ["", "", "", ""]
                city, start, end = str(r[0]).strip(), cell_to_date(r[1]), cell_to_date(r[2])
                if city and start and end:
                    trips.append(Trip(city, start, end, str(r[3]).strip()))
            self._trips = trips
        return list(self._trips)

    async def add_trip(self, trip: Trip) -> None:
        async with self._lock:
            await self._run(
                self._ws(SHEET_TRIPS).append_row,
                [text_for_sheet(trip.city), trip.start.isoformat(), trip.end.isoformat(), text_for_sheet(trip.id)],
                value_input_option="USER_ENTERED", table_range="A1",
            )
            self._trips = None

    async def delete_trip(self, trip_id: str) -> None:
        async with self._lock:
            ws = self._ws(SHEET_TRIPS)
            ids = await self._run(ws.col_values, 4)
            for idx in range(len(ids) - 1, 0, -1):
                if ids[idx] == trip_id:
                    await self._run(ws.delete_rows, idx + 1)
                    break
            self._trips = None

    async def refresh(self) -> None:
        async with self._lock:
            self._trips = None
            self._ops = None
            self._cats = None
            self._accounts = None
