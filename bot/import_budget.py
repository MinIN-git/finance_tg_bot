"""Одноразовый импорт истории из старого листа «Бюджет» в лист «Операции».

Лист «Бюджет» состоит из месячных блоков: таблица «Доходы | Сумма», таблица «Расходы | Сумма»
и справа «Отчет» с «Доходы за месяц» / «Расходы за месяц». Месяц блока определяется по ближайшей
сверху-слева ячейке с названием месяца («Сентябрь», «Сентябрь 2026», «01.09.2026», «09.2026»).

На каждую категорию бота за месяц создается одна операция (старые категории, попавшие в одну
категорию бота, складываются): дата — последний день месяца, счет «Карта», тег #импорт. Без --apply ничего не пишется (dry-run).

Запуск на сервере:
    docker compose run --rm bot python -m bot.import_budget            # dry-run
    docker compose run --rm bot python -m bot.import_budget --apply    # запись
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Sequence

from .models import Category, Operation, OpType
from .reports import month_end, month_start
from .storage import cell_to_decimal
from .textutil import fmt_money, month_title, normalize

IMPORT_TAG = "импорт"
IMPORT_COMMENT = "импорт из Бюджета"
TOLERANCE = Decimal("0.01")

MONTH_STEMS = [
    "январ", "феврал", "март", "апрел", "ма", "июн",
    "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
]
MONTH_WORDS = {  # точные формы для «май» и т.п., чтобы «ма» не ловило лишнее
    4: {"май", "мая", "мае", "маю", "маем"},
}
EN_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

# Явные соответствия старых названий категориям бота (ключ — нормализованное старое название).
EXPLICIT_MAP: dict[tuple[OpType, str], str] = {
    (OpType.EXPENSE, "жку+интернет"): "ЖКУ",
    (OpType.EXPENSE, "транспорт/ржд/такси"): "Транспорт/РЖД",
    (OpType.EXPENSE, "еда вне дома/доставки"): "Еда вне дома",
    (OpType.EXPENSE, "связь"): "Подписки",  # «Связь» объединена с «Подписками»
}


# ---------- разбор листа ----------

@dataclass
class Item:
    name: str
    amount: Decimal
    cell: str


@dataclass
class MonthBlock:
    month: date                      # первое число месяца
    label_cell: str
    income: list[Item] = field(default_factory=list)
    expense: list[Item] = field(default_factory=list)
    income_total: Decimal | None = None
    expense_total: Decimal | None = None
    problems: list[str] = field(default_factory=list)
    _tables: dict[str, int] = field(default_factory=dict)

    @property
    def income_sum(self) -> Decimal:
        return sum((i.amount for i in self.income), Decimal(0))

    @property
    def expense_sum(self) -> Decimal:
        return sum((i.amount for i in self.expense), Decimal(0))

    def reconcile(self) -> list[str]:
        """Расхождения сумм категорий с итогами из «Отчета»."""
        issues = []
        for title, got, total in (
            ("доходы", self.income_sum, self.income_total),
            ("расходы", self.expense_sum, self.expense_total),
        ):
            if total is None:
                issues.append(f"не найден итог «{title.capitalize()} за месяц»")
            elif abs(got - total) > TOLERANCE:
                issues.append(f"{title}: сумма категорий {got} ≠ итог {total} (разница {got - total})")
        return issues


def a1(row: int, col: int) -> str:
    letters = ""
    col += 1
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row + 1}"


def _norm(value: Any) -> str:
    text = normalize(str(value)).strip().rstrip(":").strip()
    return re.sub(r"\s+", " ", text)


def _norm_name(value: str) -> str:
    """Название категории без пробелов вокруг / и +."""
    return re.sub(r"\s*([/+,])\s*", r"\1", _norm(value))


def _cell(grid: Sequence[Sequence[Any]], r: int, c: int) -> Any:
    if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
        return grid[r][c]
    return ""


def parse_month_label(text: Any, today: date) -> date | None:
    """«Сентябрь», «сентябрь 2026», «Сентябрь 2026 г.», «01.09.2026», «09.2026» -> 1 число месяца."""
    t = _norm(text)
    if not t:
        return None
    m = re.fullmatch(r"(?:\d{1,2}\.)?(\d{1,2})\.(\d{4})", t)
    if m:
        month, year = int(m.group(1)), int(m.group(2))
        return date(year, month, 1) if 1 <= month <= 12 else None
    words = re.findall(r"[a-zа-я]+|\d+", t)
    if not words or len(words) > 4:
        return None
    month, month_word = None, None
    for w in words:
        if w.isdigit():
            continue
        for i, stem in enumerate(MONTH_STEMS):
            if (i == 4 and w in MONTH_WORDS[4]) or (i != 4 and w.startswith(stem)):
                month = i + 1
        if month is None and w[:3] in EN_MONTHS and len(w) <= 9:
            month = EN_MONTHS.index(w[:3]) + 1
        if month:
            month_word = w
            break
    if month is None:
        return None
    others = [w for w in words if not w.isdigit() and w != month_word]
    if any(w not in {"г", "год", "года"} for w in others):
        return None  # «Доходы за сентябрь» и т.п. — не заголовок месяца
    years = [int(w) for w in words if w.isdigit() and len(w) == 4]
    if years:
        return date(years[0], month, 1)
    if any(w.isdigit() for w in words):
        return None
    year = today.year if month <= today.month else today.year - 1
    return date(year, month, 1)


def _row_name(formatted: Sequence[Sequence[Any]], r: int, c: int, k: int) -> tuple[str, int]:
    """Название категории в строке: первая нечисловая ячейка левее суммы.
    Порядковый номер («1», «2.», «№3») в начале строки пропускается."""
    first = ""
    for cc in range(c, c + k):
        text = str(_cell(formatted, r, cc)).strip()
        if not text:
            continue
        if not first:
            first = text
        if not re.fullmatch(r"№?\s*\d+[.)]?", text):
            return text, cc
    return first, c


def _number(raw: Any, formatted: Any) -> Decimal | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return Decimal(str(raw))
    text = str(formatted).strip()
    if not text or not re.search(r"\d", text):
        return None
    return cell_to_decimal(text)


def parse_budget(formatted: Sequence[Sequence[Any]], raw: Sequence[Sequence[Any]], today: date) -> tuple[list[MonthBlock], list[str]]:
    """Разбирает лист «Бюджет». Возвращает блоки по месяцам и общие предупреждения."""
    warnings: list[str] = []
    labels: list[tuple[int, int, date]] = []
    anchors: list[tuple[int, int, str]] = []  # (row, col, kind)

    for r, row in enumerate(formatted):
        for c, value in enumerate(row):
            t = _norm(value)
            if not t:
                continue
            if t in ("доходы", "расходы"):
                # заголовок таблицы: справа (через 1–3 колонки) должно быть «Сумма»
                for k in range(1, 4):
                    right = _norm(_cell(formatted, r, c + k))
                    if right:
                        if right.startswith("сумма"):
                            anchors.append((r, c, f"{t}:{k}"))
                        break
            elif t in ("доходы за месяц", "расходы за месяц"):
                anchors.append((r, c, t))
            else:
                month = parse_month_label(value, today)
                if month is not None:
                    labels.append((r, c, month))

    if not labels:
        warnings.append("не найдено ни одной ячейки с названием месяца")
        return [], warnings

    blocks: dict[tuple[int, int], MonthBlock] = {}
    for r, c, kind in anchors:
        candidates = [(lr, lc, m) for lr, lc, m in labels if lr <= r and lc <= c]
        if not candidates:
            warnings.append(f"{a1(r, c)} «{_cell(formatted, r, c)}»: не нашел месяц выше-левее, пропускаю")
            continue
        lr, lc, month = max(candidates, key=lambda x: (x[0], x[1]))
        block = blocks.setdefault((lr, lc), MonthBlock(month=month, label_cell=a1(lr, lc)))

        if kind.endswith("за месяц"):
            value = None
            for rr, cc in [(r, c + k) for k in range(1, 4)] + [(r + 1, c)]:
                value = _number(_cell(raw, rr, cc), _cell(formatted, rr, cc))
                if value is not None:
                    break
            if value is None:
                block.problems.append(f"{a1(r, c)}: не нашел число рядом с «{_cell(formatted, r, c)}»")
            elif kind.startswith("доходы"):
                block.income_total = value
            else:
                block.expense_total = value
            continue

        table, k = kind.split(":")
        k = int(k)
        if table in block._tables:
            block.problems.append(f"{a1(r, c)}: вторая таблица «{table}» в одном месяце")
            continue
        block._tables[table] = r
        items = block.income if table == "доходы" else block.expense
        rr = r + 1
        while rr < len(formatted):
            name, name_col = _row_name(formatted, rr, c, k)
            n = _norm(name)
            amount = _number(_cell(raw, rr, c + k), _cell(formatted, rr, c + k))
            if not name and amount is None:
                break
            if n.startswith(("итого", "всего")) or n in ("доходы", "расходы"):
                break
            if not name:
                block.problems.append(f"{a1(rr, c + k)}: сумма без названия категории")
            elif amount is None or amount == 0:
                pass  # пустая категория
            elif amount < 0:
                block.problems.append(f"{a1(rr, c + k)}: отрицательная сумма у «{name}»")
            else:
                items.append(Item(name, amount, a1(rr, name_col)))
            rr += 1

    out: dict[date, MonthBlock] = {}
    for block in blocks.values():
        if block.month in out:
            warnings.append(f"месяц {month_title(block.month)} встречается дважды ({out[block.month].label_cell} и {block.label_cell})")
            out[block.month].problems.append("месяц встречается в листе дважды")
            continue
        out[block.month] = block
    return [out[m] for m in sorted(out)], warnings


# ---------- сопоставление категорий ----------

def map_category(
    name: str,
    op_type: OpType,
    categories: Sequence[Category],
    overrides: dict[str, str] | None = None,
) -> str | None:
    """Старое название -> категория бота того же типа, или None, если не сопоставилось."""
    key = _norm_name(name)
    names = {_norm_name(c.name): c.name for c in categories if c.type == op_type}
    for source in (overrides or {}, {k[1]: v for k, v in EXPLICIT_MAP.items() if k[0] == op_type}):
        target = {_norm_name(k): v for k, v in source.items()}.get(key)
        if target is not None:
            return names.get(_norm_name(target))
    if key in names:
        return names[key]
    # «Транспорт/РЖД/Такси» -> «Транспорт/РЖД»: старое название начинается с названия категории
    for norm, real in sorted(names.items(), key=lambda kv: -len(kv[0])):
        if key.startswith(norm) and key[len(norm):][:1] in ("/", "+", ",", " "):
            return real
    # «ЖКУ + интернет» -> «ЖКУ»: совпадает первая часть
    first = re.split(r"[/+,]", key)[0]
    matches = [real for norm, real in names.items() if re.split(r"[/+,]", norm)[0] == first]
    return matches[0] if len(matches) == 1 else None


# ---------- план импорта ----------

@dataclass
class MonthPlan:
    block: MonthBlock
    operations: list[Operation]
    unmapped: list[Item]
    issues: list[str]
    existing_import_ids: list[str]


def build_plan(
    blocks: Sequence[MonthBlock],
    categories: Sequence[Category],
    existing: Sequence[Operation],
    today: date,
    account: str = "Карта",
    overrides: dict[str, str] | None = None,
) -> list[MonthPlan]:
    current = month_start(today)
    plans = []
    for block in blocks:
        if block.month >= current or not (block.income or block.expense):
            continue
        last_day = month_end(block.month)
        unmapped: list[Item] = []
        grouped: dict[tuple[OpType, str], list[Item]] = {}
        for op_type, items in ((OpType.INCOME, block.income), (OpType.EXPENSE, block.expense)):
            for item in items:
                cat = map_category(item.name, op_type, categories, overrides)
                if cat is None:
                    unmapped.append(item)
                else:
                    grouped.setdefault((op_type, cat), []).append(item)
        ops = []
        for (op_type, cat), items in grouped.items():
            renamed = [i.name for i in items if _norm(i.name) != _norm(cat)]
            comment = f"{IMPORT_COMMENT}: {', '.join(i.name for i in items)}" if renamed else IMPORT_COMMENT
            ops.append(Operation(
                date=last_day, type=op_type, amount=sum((i.amount for i in items), Decimal(0)),
                category=cat, account=account, comment=comment, tags=[IMPORT_TAG],
            ))
        existing_ids = [
            op.id for op in existing
            if IMPORT_TAG in op.tags and month_start(op.date) == block.month
        ]
        plans.append(MonthPlan(block, ops, unmapped, block.problems + block.reconcile(), existing_ids))
    return plans


def format_plan(plans: Sequence[MonthPlan], warnings: Sequence[str], cur: str = "") -> str:
    lines = []
    for w in warnings:
        lines.append(f"⚠️  {w}")
    for p in plans:
        b = p.block
        lines.append("")
        lines.append(f"=== {month_title(b.month)} (ячейка {b.label_cell}) ===")
        for title, items, total, s in (
            ("Доходы", b.income, b.income_total, b.income_sum),
            ("Расходы", b.expense, b.expense_total, b.expense_sum),
        ):
            total_txt = fmt_money(total, cur, exact=True) if total is not None else "не найден"
            lines.append(f"{title}: сумма категорий {fmt_money(s, cur, exact=True)}, итог в листе {total_txt}")
        for op in p.operations:
            sign = "+" if op.type == OpType.INCOME else "−"
            lines.append(f"  {op.date:%d.%m.%Y} {sign}{fmt_money(op.amount, cur, exact=True):>14}  {op.category:<20} {op.comment}")
        for item in p.unmapped:
            lines.append(f"  ⚠️  НЕ СОПОСТАВЛЕНО: «{item.name}» ({item.cell}) {fmt_money(item.amount, cur, exact=True)}")
        for issue in p.issues:
            lines.append(f"  ⚠️  {issue}")
        if p.existing_import_ids:
            lines.append(f"  ℹ️  уже импортировано ранее: {len(p.existing_import_ids)} операций #импорт")
    return "\n".join(lines)


def month_status(p: MonthPlan, replace: bool, force: bool) -> str:
    """'ok' — писать, иначе причина пропуска."""
    if p.existing_import_ids and not replace:
        return "уже импортирован (используйте --replace, чтобы перезаписать)"
    if p.unmapped:
        return "есть несопоставленные категории (добавьте --map 'Старое=Новое')"
    if p.issues and not force:
        return "суммы не сходятся или блок разобран с ошибками (проверьте; --force — записать все равно)"
    return "ok"


# ---------- запуск ----------

def parse_overrides(values: Sequence[str]) -> dict[str, str]:
    out = {}
    for v in values:
        if "=" not in v:
            raise SystemExit(f"--map ожидает «Старое=Новое», получено: {v}")
        old, new = v.split("=", 1)
        out[old.strip()] = new.strip()
    return out


async def run(args: argparse.Namespace) -> int:
    from .config import Config
    from .storage import SheetsStorage

    config = Config.from_env()
    storage = SheetsStorage(config.credentials_file, config.spreadsheet_id, cache_ttl=0)
    today = datetime.now(config.tz).date()

    formatted, raw = await asyncio.to_thread(storage.read_grid, args.sheet)
    blocks, warnings = parse_budget(formatted, raw, today)
    if args.debug:
        for b in blocks:
            print(f"[debug] {b.label_cell}: {b.month} доходов {len(b.income)}, расходов {len(b.expense)}, "
                  f"итоги {b.income_total}/{b.expense_total}")
    categories = await storage.list_categories()
    existing = await storage.list_operations()
    plans = build_plan(blocks, categories, existing, today, args.account, parse_overrides(args.map))

    print(format_plan(plans, warnings, config.currency))
    if not plans:
        print("\nНечего импортировать.")
        return 1

    print("\nИтог:")
    to_write: list[MonthPlan] = []
    for p in plans:
        status = month_status(p, args.replace, args.force)
        print(f"  {month_title(p.block.month)}: {'будет записан' if status == 'ok' else 'ПРОПУСК — ' + status}"
              f" ({len(p.operations)} операций)")
        if status == "ok":
            to_write.append(p)

    if not args.apply:
        print("\nЭто dry-run, в таблицу ничего не записано. Для записи добавьте --apply.")
        return 0
    if not to_write:
        print("\nЗаписывать нечего.")
        return 1
    for p in to_write:
        if p.existing_import_ids:
            removed = await storage.delete_operations(p.existing_import_ids)
            print(f"  {month_title(p.block.month)}: удалено старых операций #импорт: {removed}")
        await storage.add_operations(p.operations)
        print(f"  {month_title(p.block.month)}: записано {len(p.operations)} операций")
    print("\nГотово. В боте выполните /refresh, затем /report или /month.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Импорт истории из листа «Бюджет» в «Операции».")
    parser.add_argument("--apply", action="store_true", help="записать в таблицу (без флага — только показать)")
    parser.add_argument("--replace", action="store_true", help="перезаписать месяцы, уже импортированные ранее")
    parser.add_argument("--force", action="store_true", help="записать, даже если суммы не сходятся с итогами")
    parser.add_argument("--map", action="append", default=[], metavar="СТАРОЕ=НОВОЕ",
                        help="сопоставить старую категорию категории бота (можно несколько раз)")
    parser.add_argument("--sheet", default="Бюджет", help="имя листа (по умолчанию «Бюджет»)")
    parser.add_argument("--account", default="Карта", help="счет для импортированных операций")
    parser.add_argument("--debug", action="store_true", help="показать найденные блоки")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
