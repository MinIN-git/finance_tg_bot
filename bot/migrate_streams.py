"""Разовая миграция истории на три потока трат (обычные / тяжелые / командировки).

В истории из «Бюджета» (#импорт) тяжелые траты и командировки уже сидят внутри категорий. Скрипт:

1. Для строк «Больших покупок» создает операции вида «тяжелая» (дата и сумма из листа, категория по
   маппингу), уменьшает #импорт-операцию той же категории того же месяца на эту сумму и вписывает
   категорию и id операции в строку листа.
2. Переносит поездки из старого листа «Командировки»: заводит поездку в «Поездки» и по операции вида
   «командировка» на каждую статью; для поездок с вычетом уменьшает #импорт-операции месяца по маппингу статей.
3. Существующим операциям с #тяжелое или тегом поездки проставляет «Вид».

Повторный запуск ничего не дублирует: созданные операции связаны с исходными строками через id
и метки в комментариях #импорт-операций. Без --apply ничего не пишется (dry-run).

Запуск на сервере:
    docker compose run --rm bot python -m bot.migrate_streams            # dry-run
    docker compose run --rm bot python -m bot.migrate_streams --apply    # запись
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from .models import HEAVY_TAG, BigPurchase, Kind, Operation, OpType, Trip, kind_from_tags, trip_tag
from .storage import Storage, cell_to_date, cell_to_decimal
from .textutil import fmt_money, normalize

IMPORT_TAG = "импорт"
TRIPS_SHEET = "Командировки"
TRIP_CITY = "Командировка"
TOLERANCE = Decimal("0.01")

# Тяжелые траты: (дата, сумма) -> категория. Подтверждено Иваном.
HEAVY_MAP: dict[tuple[date, Decimal], str] = {
    (date(2026, 7, 20), Decimal(100000)): "Семейные расходы",       # Танюшке с мамкой на отпуск
    (date(2026, 8, 12), Decimal(77960)): "Одежда / украшения",      # Кольцо для Танюшки
    (date(2026, 8, 17), Decimal(62838)): "Подарки",                 # Круиз для Родителей
    (date(2026, 9, 6), Decimal(283122)): "Досуг",                   # Отпуск в Турцию
}

# Статьи листа «Командировки» -> категория бота.
TRIP_ITEM_MAP: dict[str, str] = {
    "кафешки": "Еда вне дома",
    "транспорт": "Транспорт/РЖД",
    "жд билеты": "Транспорт/РЖД",
    "жилье": "Прочее",
    "прочее": "Прочее",
}

# Поездки, траты которых нужно вычесть из #импорт-операций (остальные — до начала истории).
DEDUCT_TRIPS: set[date] = {date(2026, 9, 6)}


# ---------- разбор листа «Командировки» ----------

RANGE_RE = re.compile(r"(\d{1,2}\.\d{1,2}\.\d{4})\s*[–—-]\s*(\d{1,2}\.\d{1,2}\.\d{4})")
TOTAL_LABEL = "расход за командировку"


@dataclass
class OldTrip:
    start: date
    end: date
    items: list[tuple[str, Decimal]] = field(default_factory=list)   # (статья, сумма)
    total: Decimal | None = None
    cell: str = ""

    @property
    def items_sum(self) -> Decimal:
        return sum((a for _, a in self.items), Decimal(0))


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", normalize(str(v)).strip().rstrip(":"))


def _cell(grid: Sequence[Sequence[Any]], r: int, c: int) -> Any:
    return grid[r][c] if 0 <= r < len(grid) and 0 <= c < len(grid[r]) else ""


def _amount_right(formatted, raw, r: int, c: int) -> Decimal | None:
    for k in range(1, 4):
        rv, fv = _cell(raw, r, c + k), _cell(formatted, r, c + k)
        if isinstance(rv, (int, float)) and not isinstance(rv, bool):
            return Decimal(str(rv))
        if str(fv).strip() and re.search(r"\d", str(fv)):
            return cell_to_decimal(fv)
    return None


def parse_trips_sheet(formatted: Sequence[Sequence[Any]], raw: Sequence[Sequence[Any]]) -> list[OldTrip]:
    """Блоки: заголовок «ДД.ММ.ГГГГ – ДД.ММ.ГГГГ», ниже статьи с суммами справа, «Расход за командировку»."""
    trips = []
    for r, row in enumerate(formatted):
        for c, value in enumerate(row):
            m = RANGE_RE.search(str(value))
            if not m:
                continue
            start, end = cell_to_date(m.group(1)), cell_to_date(m.group(2))
            if start is None or end is None:
                continue
            trip = OldTrip(start, end, cell=f"{chr(65 + c)}{r + 1}" if c < 26 else str(c))
            rr = r + 1
            while rr < len(formatted):
                label = str(_cell(formatted, rr, c)).strip()
                if RANGE_RE.search(label):
                    break
                amount = _amount_right(formatted, raw, rr, c)
                if not label and amount is None:
                    if trip.items:   # пустая строка после статей — конец блока
                        break
                    rr += 1
                    continue
                if _norm(label).startswith(TOTAL_LABEL):
                    trip.total = amount
                    break
                if label and amount:
                    trip.items.append((label, amount))
                rr += 1
            trips.append(trip)
    return trips


# ---------- план ----------

@dataclass
class Reduction:
    import_op: Operation
    amount: Decimal
    marker: str          # метка в комментарии #импорт-операции: по ней видно, что вычет уже сделан


@dataclass
class Plan:
    new_ops: list[Operation] = field(default_factory=list)
    links: list[tuple[BigPurchase, Operation]] = field(default_factory=list)   # строка листа -> операция
    reductions: list[Reduction] = field(default_factory=list)
    new_trips: list[Trip] = field(default_factory=list)
    kind_fixes: list[Operation] = field(default_factory=list)
    done: list[str] = field(default_factory=list)       # что уже перенесено ранее
    warnings: list[str] = field(default_factory=list)


def _import_op(ops: Sequence[Operation], category: str, month: date) -> Operation | None:
    return next((op for op in ops if op.type == OpType.EXPENSE and IMPORT_TAG in op.tags
                 and op.category == category and (op.date.year, op.date.month) == (month.year, month.month)), None)


def _plan_reduction(plan: Plan, ops: Sequence[Operation], category: str, when: date, amount: Decimal, marker: str,
                    what: str) -> None:
    imp = _import_op(ops, category, when)
    if imp is None:
        plan.warnings.append(f"{what}: нет #импорт-операции «{category}» за {when:%m.%Y} — вычитать не из чего")
        return
    if marker in imp.comment or any(r.import_op.id == imp.id and r.marker == marker for r in plan.reductions):
        return
    already = sum((r.amount for r in plan.reductions if r.import_op.id == imp.id), Decimal(0))
    if imp.amount - already < amount - TOLERANCE:
        plan.warnings.append(f"{what}: в «{category}» за {when:%m.%Y} только {fmt_money(imp.amount - already, exact=True)}, "
                             f"а вычесть нужно {fmt_money(amount, exact=True)} — пропускаю")
        return
    plan.reductions.append(Reduction(imp, amount, marker))


def build_plan(
    ops: Sequence[Operation],
    bigs: Sequence[BigPurchase],
    old_trips: Sequence[OldTrip],
    trips: Sequence[Trip],
    account: str,
    heavy_map: dict[tuple[date, Decimal], str] | None = None,
    item_map: dict[str, str] | None = None,
    deduct: set[date] | None = None,
) -> Plan:
    heavy_map = HEAVY_MAP if heavy_map is None else heavy_map
    item_map = TRIP_ITEM_MAP if item_map is None else item_map
    deduct = DEDUCT_TRIPS if deduct is None else deduct
    plan = Plan()
    by_id = {op.id: op for op in ops}

    # 1. тяжелые траты из «Больших покупок»
    for b in bigs:
        what = f"«{b.what}» {b.date:%d.%m.%Y}"
        linked = by_id.get(b.op_id) if b.op_id else None
        category = next((cat for (d, a), cat in heavy_map.items() if d == b.date and abs(a - b.amount) < 1), None)
        category = category or (linked.category if linked else "") or b.category
        if not category:
            plan.warnings.append(f"{what}: категория неизвестна — добавьте --heavy «{b.date:%d.%m.%Y}:{b.amount}=Категория»")
            continue
        if linked is None:
            op = Operation(date=b.date, type=OpType.EXPENSE, amount=b.amount, category=category, account=account,
                           comment=b.what, tags=[HEAVY_TAG], kind=Kind.HEAVY)
            plan.new_ops.append(op)
            plan.links.append((b, op))
        else:
            op = linked
            plan.done.append(f"{what}: тяжелая операция уже есть")
        _plan_reduction(plan, ops, category, b.date, b.amount, f"[тяжелое {op.id}]", what)

    # 2. поездки из листа «Командировки»
    existing_trips = {t.id: t for t in trips}
    for ot in old_trips:
        trip = Trip(TRIP_CITY, ot.start, ot.end)
        what = f"Поездка {ot.start:%d.%m}–{ot.end:%d.%m.%Y}"
        if ot.total is not None and abs(ot.items_sum - ot.total) > TOLERANCE:
            plan.warnings.append(f"{what}: сумма статей {ot.items_sum} ≠ «Расход за командировку» {ot.total} — пропускаю")
            continue
        if trip.id not in existing_trips:
            plan.new_trips.append(trip)
        for label, amount in ot.items:
            category = item_map.get(_norm(label))
            if category is None:
                plan.warnings.append(f"{what}: статья «{label}» без категории — добавьте --item «{label}=Категория»")
                continue
            marker = f"[{trip.id} {label}]"
            op = next((o for o in ops if trip.tag in o.tags and marker in o.comment), None)
            if op is None:
                op = Operation(date=ot.start, type=OpType.EXPENSE, amount=amount, category=category, account=account,
                               comment=f"{label} {marker}", tags=[trip.tag], kind=Kind.TRIP)
                plan.new_ops.append(op)
            else:
                plan.done.append(f"{what}: «{label}» уже перенесено")
            if ot.start in deduct:
                _plan_reduction(plan, ops, category, ot.start, amount, f"[{trip.id} {label}]", f"{what}, {label}")

    # 3. «Вид» у существующих операций с тегами
    for op in ops:
        if op.type == OpType.EXPENSE and (HEAVY_TAG in op.tags or trip_tag(op)):
            plan.kind_fixes.append(replace(op, kind=kind_from_tags(op.tags)))
    return plan


def format_plan(plan: Plan) -> str:
    m = lambda v: fmt_money(v, exact=True)  # noqa: E731
    lines = []
    if plan.new_trips:
        lines.append("Новые поездки в «Поездки»:")
        lines += [f"  + {t.city} {t.start:%d.%m.%Y}–{t.end:%d.%m.%Y} ({t.days} дн)" for t in plan.new_trips]
    if plan.new_ops:
        lines.append("Новые операции:")
        for op in plan.new_ops:
            lines.append(f"  + {op.date:%d.%m.%Y} {op.kind.value:<12} {m(op.amount):>12}  {op.category:<20} {op.comment}")
    if plan.reductions:
        lines.append("Уменьшить #импорт-операции:")
        left: dict[str, Decimal] = {}
        for r in plan.reductions:
            imp = r.import_op
            was = left.get(imp.id, imp.amount)
            left[imp.id] = was - r.amount
            lines.append(f"  − {imp.date:%m.%Y} {imp.category:<20} {m(was):>12} → {m(was - r.amount):>12}"
                         f"  (−{m(r.amount)} {r.marker})")
    if plan.links:
        lines.append("Вписать категорию и id в «Большие покупки»:")
        lines += [f"  ✎ {b.date:%d.%m.%Y} «{b.what}» → {op.category}" for b, op in plan.links]
    if plan.kind_fixes:
        lines.append(f"Записать «Вид» в колонку у операций с тегами #тяжелое / поездки: {len(plan.kind_fixes)} "
                     "(повторная запись того же значения безопасна)")
    for d in plan.done:
        lines.append(f"  ✓ {d}")
    for w in plan.warnings:
        lines.append(f"⚠️  {w}")
    if not (plan.new_ops or plan.reductions or plan.new_trips or plan.links):
        lines.append("Переносить нечего — все уже сделано.")
    return "\n".join(lines)


async def apply_plan(storage: Storage, plan: Plan) -> None:
    for trip in plan.new_trips:
        await storage.add_trip(trip)
    if plan.new_ops:
        await storage.add_operations(plan.new_ops)
    for b, op in plan.links:
        await storage.link_big_purchase(b, op.id, op.category)
    reduced: dict[str, Operation] = {}
    for r in plan.reductions:
        imp = reduced.get(r.import_op.id, r.import_op)
        reduced[imp.id] = replace(imp, amount=imp.amount - r.amount, comment=f"{imp.comment} {r.marker}".strip())
    for imp in reduced.values():
        await storage.update_operation(imp)
    for op in plan.kind_fixes:
        await storage.update_operation(op)


# ---------- запуск ----------

def _parse_heavy_overrides(values: Sequence[str]) -> dict[tuple[date, Decimal], str]:
    out = dict(HEAVY_MAP)
    for v in values:
        m = re.fullmatch(r"\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*:\s*([\d\s.,]+)\s*=\s*(.+)", v)
        d = cell_to_date(m.group(1)) if m else None
        amount = cell_to_decimal(m.group(2)) if m else None
        if not (d and amount):
            raise SystemExit(f"--heavy ожидает «ДД.ММ.ГГГГ:СУММА=Категория», получено: {v}")
        out = {k: c for k, c in out.items() if not (k[0] == d and abs(k[1] - amount) < 1)}
        out[(d, amount)] = m.group(3).strip()
    return out


def _parse_item_overrides(values: Sequence[str]) -> dict[str, str]:
    out = dict(TRIP_ITEM_MAP)
    for v in values:
        if "=" not in v:
            raise SystemExit(f"--item ожидает «Статья=Категория», получено: {v}")
        k, c = v.split("=", 1)
        out[_norm(k)] = c.strip()
    return out


async def run(args: argparse.Namespace) -> int:
    from .config import Config
    from .storage import SheetsStorage

    config = Config.from_env()
    storage = SheetsStorage(config.credentials_file, config.spreadsheet_id, cache_ttl=0)
    await asyncio.to_thread(storage.ensure_sheets)
    plan = await make_plan(storage, args)
    print(format_plan(plan))
    if not args.apply:
        print("\nЭто dry-run, в таблицу ничего не записано. Для записи добавьте --apply.")
        return 0
    await apply_plan(storage, plan)
    print(f"\nГотово ({datetime.now():%H:%M}). В боте выполните /refresh.")
    return 0


async def make_plan(storage: Storage, args: argparse.Namespace) -> Plan:
    sheet = await storage.read_sheet(TRIPS_SHEET)
    old_trips = parse_trips_sheet(*sheet) if sheet else []
    accounts = await storage.list_accounts()
    deduct = {d for d in (cell_to_date(x) for x in args.deduct)} if args.deduct else DEDUCT_TRIPS
    return build_plan(
        await storage.list_operations(), await storage.list_big_purchases(), old_trips, await storage.list_trips(),
        accounts[0].name if accounts else "Карта",
        heavy_map=_parse_heavy_overrides(args.heavy), item_map=_parse_item_overrides(args.item), deduct=deduct,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Миграция истории на три потока трат (обычные / тяжелые / командировки).")
    p.add_argument("--apply", action="store_true", help="записать в таблицу (без флага — только показать)")
    p.add_argument("--heavy", action="append", default=[], metavar="ДД.ММ.ГГГГ:СУММА=Категория",
                   help="категория тяжелой траты (можно несколько раз)")
    p.add_argument("--item", action="append", default=[], metavar="Статья=Категория",
                   help="категория статьи командировки (можно несколько раз)")
    p.add_argument("--deduct", action="append", default=[], metavar="ДД.ММ.ГГГГ",
                   help="начало поездки, траты которой вычесть из #импорт (по умолчанию 06.09.2026)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
