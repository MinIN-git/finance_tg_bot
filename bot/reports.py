"""Расчеты и тексты отчетов. Чистые функции без обращения к хранилищу."""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Sequence

from .models import BalanceEntry, Operation, OpType
from .textutil import (
    MONTHS_DATIVE, MONTHS_NOMINATIVE, MONTHS_SHORT, fmt_money, fmt_percent, fmt_short_date, month_title,
)

ZERO = Decimal(0)


# ---------- даты ----------

def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def add_months(d: date, n: int) -> date:
    """Первое число месяца, сдвинутого на n месяцев."""
    idx = d.year * 12 + d.month - 1 + n
    return date(idx // 12, idx % 12 + 1, 1)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------- сводка ----------

@dataclass
class TagStat:
    expense: Decimal = ZERO
    income: Decimal = ZERO
    count: int = 0


@dataclass
class Summary:
    start: date
    end: date
    income: Decimal = ZERO
    expense: Decimal = ZERO
    expense_by_category: list[tuple[str, Decimal]] = field(default_factory=list)
    income_by_category: list[tuple[str, Decimal]] = field(default_factory=list)
    tags: dict[str, TagStat] = field(default_factory=dict)
    count: int = 0

    @property
    def balance(self) -> Decimal:
        return self.income - self.expense

    @property
    def savings_rate(self) -> Decimal | None:
        if self.income <= 0:
            return None
        return self.balance / self.income * 100


def _sorted_totals(totals: dict[str, Decimal]) -> list[tuple[str, Decimal]]:
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


def summarize(ops: Iterable[Operation], start: date, end: date) -> Summary:
    """Сводка за период [start, end] включительно. Переводы между счетами не учитываются."""
    s = Summary(start=start, end=end)
    exp: dict[str, Decimal] = defaultdict(lambda: ZERO)
    inc: dict[str, Decimal] = defaultdict(lambda: ZERO)
    tags: dict[str, TagStat] = defaultdict(TagStat)
    for op in ops:
        if not (start <= op.date <= end) or op.type == OpType.TRANSFER:
            continue
        s.count += 1
        cat = op.category or "Без категории"
        if op.type == OpType.EXPENSE:
            s.expense += op.amount
            exp[cat] += op.amount
        else:
            s.income += op.amount
            inc[cat] += op.amount
        for t in op.tags:
            st = tags[t]
            st.count += 1
            if op.type == OpType.EXPENSE:
                st.expense += op.amount
            else:
                st.income += op.amount
    s.expense_by_category = _sorted_totals(exp)
    s.income_by_category = _sorted_totals(inc)
    s.tags = dict(sorted(tags.items()))
    return s


def summarize_month(ops: Sequence[Operation], any_day: date) -> Summary:
    return summarize(ops, month_start(any_day), month_end(any_day))


# ---------- месячный отчет ----------

@dataclass
class MonthlyReport:
    summary: Summary
    avg_months: list[date]          # первые числа месяцев, по которым считалось среднее
    avg_income: Decimal | None
    avg_expense: Decimal | None
    avg_by_category: dict[str, Decimal]


def monthly_report(ops: Sequence[Operation], month: date, avg_window: int = 3) -> MonthlyReport:
    """Отчет за месяц `month` + сравнение со средним за предыдущие `avg_window` месяцев.

    В среднее берутся только месяцы, в которых есть операции (чтобы первые месяцы
    использования бота не занижали среднее).
    """
    first = month_start(month)
    summary = summarize_month(ops, first)
    prev = [summarize_month(ops, add_months(first, -i)) for i in range(1, avg_window + 1)]
    prev = [p for p in prev if p.count > 0]
    if not prev:
        return MonthlyReport(summary, [], None, None, {})
    n = Decimal(len(prev))
    by_cat: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in prev:
        for name, amount in p.expense_by_category:
            by_cat[name] += amount
    return MonthlyReport(
        summary=summary,
        avg_months=sorted(p.start for p in prev),
        avg_income=sum((p.income for p in prev), ZERO) / n,
        avg_expense=sum((p.expense for p in prev), ZERO) / n,
        avg_by_category={k: v / n for k, v in by_cat.items()},
    )


def _delta_pct(current: Decimal, base: Decimal) -> str:
    if base == 0:
        return ""
    pct = (current - base) / base * 100
    sign = "+" if pct > 0 else ("−" if pct < 0 else "±")
    return f" ({sign}{abs(round(pct))}%)"


def _share(part: Decimal, total: Decimal) -> str:
    return fmt_percent(part / total * 100) if total > 0 else "—"


# ---------- форматирование ----------

def format_totals(s: Summary, cur: str = "") -> list[str]:
    lines = [
        f"Доход: <b>{fmt_money(s.income, cur)}</b>",
        f"Расход: <b>{fmt_money(s.expense, cur)}</b>",
        f"Остаток: <b>{fmt_money(s.balance, cur)}</b>",
    ]
    if s.savings_rate is not None:
        lines.append(f"Сбережения: <b>{fmt_percent(s.savings_rate)}</b>")
    return lines


def format_categories(items: Sequence[tuple[str, Decimal]], total: Decimal, limit: int | None, cur: str = "") -> list[str]:
    shown = items[:limit] if limit else items
    return [
        f"{i}. {html.escape(name)} — {fmt_money(amount, cur)} ({_share(amount, total)})"
        for i, (name, amount) in enumerate(shown, 1)
    ]


def format_tags(s: Summary, cur: str = "") -> list[str]:
    lines = []
    for tag, st in s.tags.items():
        parts = []
        if st.expense:
            parts.append(f"расход {fmt_money(st.expense, cur)}")
        if st.income:
            parts.append(f"доход {fmt_money(st.income, cur)}")
        lines.append(f"#{html.escape(tag)}: {', '.join(parts)} · {st.count} оп.")
    return lines


def format_period(title: str, s: Summary, cur: str = "", top: int | None = 10) -> str:
    lines = [f"<b>{title}</b>", *format_totals(s, cur)]
    if s.expense_by_category:
        lines += ["", "<b>Расходы по категориям:</b>", *format_categories(s.expense_by_category, s.expense, top, cur)]
    if s.tags:
        lines += ["", "<b>Теги:</b>", *format_tags(s, cur)]
    if s.count == 0:
        lines += ["", "Операций пока нет."]
    return "\n".join(lines)


def format_month(s: Summary, today: date, cur: str = "") -> str:
    title = f"📅 {month_title(s.start).capitalize()} (1–{today.day} {MONTHS_SHORT[today.month - 1]})"
    return format_period(title, s, cur)


def format_week(s: Summary, cur: str = "") -> str:
    title = f"🗓 Неделя {fmt_short_date(s.start)}–{fmt_short_date(s.end)}"
    return format_period(title, s, cur)


def format_monthly_report(r: MonthlyReport, cur: str = "") -> str:
    s = r.summary
    lines = [f"📊 <b>Отчет за {month_title(s.start)}</b>", *format_totals(s, cur)]
    if s.count == 0:
        lines += ["", "За этот месяц операций нет."]
    if s.expense_by_category:
        lines += ["", "<b>Топ-5 расходов:</b>", *format_categories(s.expense_by_category, s.expense, 5, cur)]
    if r.avg_months:
        months = ", ".join(MONTHS_SHORT[m.month - 1] for m in r.avg_months)
        lines += ["", f"<b>Сравнение со средним</b> ({len(r.avg_months)} мес.: {months}):"]
        lines.append(f"Расход: {fmt_money(s.expense, cur)} vs {fmt_money(r.avg_expense, cur)}{_delta_pct(s.expense, r.avg_expense)}")
        lines.append(f"Доход: {fmt_money(s.income, cur)} vs {fmt_money(r.avg_income, cur)}{_delta_pct(s.income, r.avg_income)}")
        for name, amount in s.expense_by_category[:5]:
            avg = r.avg_by_category.get(name)
            if avg:
                lines.append(f"· {html.escape(name)}: {fmt_money(amount, cur)} vs {fmt_money(avg, cur)}{_delta_pct(amount, avg)}")
    else:
        lines += ["", "Для сравнения со средним пока мало данных."]
    if s.tags:
        lines += ["", "<b>Теги:</b>", *format_tags(s, cur)]
    return "\n".join(lines)


def format_category_report(
    name: str,
    ops: Sequence[Operation],
    today: date,
    cur: str = "",
    history_months: int = 3,
    max_items: int = 20,
) -> str:
    first = month_start(today)
    in_cat = [op for op in ops if op.category == name and op.type != OpType.TRANSFER]
    current = sorted((op for op in in_cat if first <= op.date <= today), key=lambda o: o.date, reverse=True)
    total = sum((op.amount for op in current), ZERO)
    lines = [f"🏷 <b>{html.escape(name)}</b> — {month_title(first)}", f"Итого: <b>{fmt_money(total, cur)}</b> ({len(current)} оп.)"]
    for op in current[:max_items]:
        comment = f" {html.escape(op.comment)}" if op.comment else ""
        lines.append(f"{fmt_short_date(op.date)} · {fmt_money(op.amount, cur)}{comment}")
    if len(current) > max_items:
        lines.append(f"… и еще {len(current) - max_items}")
    hist = []
    for i in range(1, history_months + 1):
        m = add_months(first, -i)
        amount = sum((op.amount for op in in_cat if m <= op.date <= month_end(m)), ZERO)
        hist.append(f"{MONTHS_SHORT[m.month - 1]} {m.year}: {fmt_money(amount, cur)}")
    lines += ["", "<b>Прошлые месяцы:</b>", *hist]
    return "\n".join(lines)


# ---------- остатки по счетам ----------

def shift_months(d: date, n: int) -> date:
    """Та же дата n месяцев назад/вперед (день обрезается до конца месяца)."""
    first = add_months(d, n)
    return first.replace(day=min(d.day, month_end(first).day))


def group_snapshots(entries: Iterable[BalanceEntry]) -> list[tuple[date, dict[str, Decimal]]]:
    """Снимки по датам (по возрастанию): дата -> {счет: сумма}."""
    by_date: dict[date, dict[str, Decimal]] = defaultdict(dict)
    for e in entries:
        by_date[e.date][e.account] = e.amount
    return sorted(by_date.items())


@dataclass
class AccountDelta:
    account: str
    amount: Decimal
    delta: Decimal | None  # None — счета не было в прошлом снимке


@dataclass
class CapitalSummary:
    date: date
    total: Decimal
    accounts: list[AccountDelta]
    prev_date: date | None = None
    total_delta: Decimal | None = None
    ops_month: date | None = None        # первое число месяца, за который считались операции
    ops_balance: Decimal | None = None   # доход − траты за этот месяц (без переводов)
    dynamics: list[tuple[int, Decimal]] = field(default_factory=list)  # (месяцев, изменение итога)

    @property
    def unaccounted(self) -> Decimal | None:
        """Изменение итога минус (доход − траты): проценты, забытые траты и т.п."""
        if self.total_delta is None or self.ops_balance is None:
            return None
        return self.total_delta - self.ops_balance


def capital_summary(
    entries: Iterable[BalanceEntry],
    ops: Sequence[Operation],
    account_order: Sequence[str] = (),
    periods: Sequence[int] = (1, 3, 6, 12),
) -> CapitalSummary | None:
    snapshots = group_snapshots(entries)
    if not snapshots:
        return None
    last_date, last = snapshots[-1]
    prev_date, prev = snapshots[-2] if len(snapshots) > 1 else (None, {})

    order = {name: i for i, name in enumerate(account_order)}
    names = sorted(set(last) | set(prev), key=lambda n: (order.get(n, len(order)), n))
    accounts = []
    for name in names:
        amount = last.get(name, ZERO)
        delta = amount - prev[name] if name in prev else None
        accounts.append(AccountDelta(name, amount, delta if prev_date else None))

    total = sum(last.values(), ZERO)
    s = CapitalSummary(date=last_date, total=total, accounts=accounts)
    if prev_date is not None:
        s.prev_date = prev_date
        s.total_delta = total - sum(prev.values(), ZERO)

    month = add_months(last_date, -1)
    month_sum = summarize_month(ops, month)
    if month_sum.count:
        s.ops_month, s.ops_balance = month_sum.start, month_sum.balance

    for n in periods:
        target = shift_months(last_date, -n)
        base = [snap for d, snap in snapshots[:-1] if d <= target]
        if base:
            s.dynamics.append((n, total - sum(base[-1].values(), ZERO)))
    return s


def format_capital(s: CapitalSummary | None, cur: str = "") -> str:
    if s is None:
        return "Остатков пока нет. Пройдите опрос по счетам: /snapshot"
    m = lambda v, signed=False: fmt_money(v, cur, signed=signed)  # noqa: E731
    head = f"💼 <b>{month_title(s.date).capitalize()}</b> ({fmt_short_date(s.date)}): всего <b>{m(s.total)}</b>"
    if s.total_delta is not None:
        if (s.prev_date.year, s.prev_date.month) != (s.date.year, s.date.month):
            ref = f"к {MONTHS_DATIVE[s.prev_date.month - 1]}"
        else:
            ref = f"к {fmt_short_date(s.prev_date)}"
        head += f" ({m(s.total_delta, True)} {ref})"
    lines = [head]
    for a in s.accounts:
        delta = f" ({m(a.delta, True)})" if a.delta is not None else ""
        lines.append(f"• {html.escape(a.account)}: {m(a.amount)}{delta}")
    if s.ops_balance is not None:
        lines.append(f"Доход − траты за {MONTHS_NOMINATIVE[s.ops_month.month - 1]}: {m(s.ops_balance, True)}")
    if s.unaccounted is not None:
        lines.append(f"Не учтено: <b>{m(s.unaccounted, True)}</b> <i>(проценты, забытые траты)</i>")
    if s.dynamics:
        lines.append("Динамика: " + " · ".join(f"за {n} мес {m(v, True)}" for n, v in s.dynamics))
    elif s.prev_date is None:
        lines.append("Это первый снимок — сравнение появится со следующего.")
    return "\n".join(lines)
