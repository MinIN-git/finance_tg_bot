"""Расчеты и тексты отчетов. Чистые функции без обращения к хранилищу."""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from .models import BalanceEntry, BigPurchase, Operation, OpType
from .textutil import (
    MONTHS_DATIVE, MONTHS_NOMINATIVE, MONTHS_SHORT, fmt_date, fmt_money, fmt_percent, fmt_short_date, month_title,
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
    avg_by_category: dict[str, Decimal]            # расходы
    avg_income_by_category: dict[str, Decimal] = field(default_factory=dict)


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
    inc_cat: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in prev:
        for name, amount in p.expense_by_category:
            by_cat[name] += amount
        for name, amount in p.income_by_category:
            inc_cat[name] += amount
    return MonthlyReport(
        summary=summary,
        avg_months=sorted(p.start for p in prev),
        avg_income=sum((p.income for p in prev), ZERO) / n,
        avg_expense=sum((p.expense for p in prev), ZERO) / n,
        avg_by_category={k: v / n for k, v in by_cat.items()},
        avg_income_by_category={k: v / n for k, v in inc_cat.items()},
    )


# ---------- форматирование ----------

HIDDEN_TAGS = {"импорт"}           # служебные теги в отчетах не показываем
NEAR_AVERAGE_PCT = 3               # |отклонение| меньше — «≈ среднее»
MULTIPLIER_FROM = 3                # с ×3 вместо процентов показываем разницу в деньгах и множитель

Icons = Mapping[tuple[OpType, str], str]


def _icon(icons: Icons | None, op_type: OpType, name: str) -> str:
    emoji = (icons or {}).get((op_type, name), "")
    return f"{emoji} " if emoji else ""


def _share(part: Decimal, total: Decimal) -> str:
    return fmt_percent(part / total * 100) if total > 0 else "—"


def compare_to_average(current: Decimal, avg: Decimal | None, cur: str = "", near: str = "≈ среднее") -> str:
    """«⬆️ +12%», «⬇️ −20%», «≈ среднее», «⬆️ +11 400 (×115)», «🆕»."""
    if avg is None or avg <= 0:
        return "🆕" if current > 0 else ""
    ratio = current / avg
    pct = (ratio - 1) * 100
    if abs(pct) < NEAR_AVERAGE_PCT:
        return near
    arrow = "⬆️" if pct > 0 else "⬇️"
    if ratio >= MULTIPLIER_FROM:
        mult = f"{ratio:.0f}" if ratio >= 10 else f"{ratio:.1f}".replace(".", ",")
        return f"{arrow} {fmt_money(current - avg, cur, signed=True)} (×{mult})"
    sign = "+" if pct > 0 else "−"
    return f"{arrow} {sign}{abs(round(pct))}%"


def _balance_line(s: Summary, cur: str) -> str:
    mark = "🟢" if s.balance >= 0 else "🔴"
    line = f"{mark} Сальдо: <b>{fmt_money(s.balance, cur, signed=True)}</b>"
    if s.savings_rate is not None:
        line += f" · сбережения {fmt_percent(s.savings_rate)}"
    return line


def format_totals(s: Summary, cur: str = "", r: MonthlyReport | None = None) -> list[str]:
    inc_cmp = exp_cmp = ""
    if r is not None and r.avg_months:
        inc_cmp = f"  {compare_to_average(s.income, r.avg_income, cur)}"
        exp_cmp = f"  {compare_to_average(s.expense, r.avg_expense, cur)}"
    return [
        f"💰 Доход: <b>{fmt_money(s.income, cur)}</b>{inc_cmp}",
        f"💸 Расход: <b>{fmt_money(s.expense, cur)}</b>{exp_cmp}",
        _balance_line(s, cur),
    ]


def format_categories(
    items: Sequence[tuple[str, Decimal]],
    total: Decimal,
    op_type: OpType,
    cur: str = "",
    icons: Icons | None = None,
    averages: Mapping[str, Decimal] | None = None,
) -> list[str]:
    """Все ненулевые категории по убыванию: «🛒 Продукты домой — 12 000 · 35%  ⬆️ +10%»."""
    lines = []
    for name, amount in items:
        if amount <= 0:
            continue
        line = f"{_icon(icons, op_type, name)}{html.escape(name)} — {fmt_money(amount, cur)} · {_share(amount, total)}"
        if averages is not None:
            line += f"  {compare_to_average(amount, averages.get(name), cur)}"
        lines.append(line.rstrip())
    return lines


def format_tags(s: Summary, cur: str = "") -> list[str]:
    lines = []
    for tag, st in s.tags.items():
        if tag in HIDDEN_TAGS:
            continue
        parts = []
        if st.expense:
            parts.append(f"расход {fmt_money(st.expense, cur)}")
        if st.income:
            parts.append(f"доход {fmt_money(st.income, cur)}")
        lines.append(f"🏷 #{html.escape(tag)}: {', '.join(parts)} · {st.count} оп.")
    return lines


def _sections(s: Summary, cur: str, icons: Icons | None, r: MonthlyReport | None = None) -> list[str]:
    lines: list[str] = []
    if s.expense_by_category:
        lines += ["", "<b>💸 Расходы</b>", *format_categories(
            s.expense_by_category, s.expense, OpType.EXPENSE, cur, icons,
            r.avg_by_category if r and r.avg_months else None,
        )]
    if s.income_by_category:
        lines += ["", "<b>💰 Доходы</b>", *format_categories(
            s.income_by_category, s.income, OpType.INCOME, cur, icons,
            r.avg_income_by_category if r and r.avg_months else None,
        )]
    tags = format_tags(s, cur)
    if tags:
        lines += ["", "<b>Теги</b>", *tags]
    return lines


def format_period(title: str, s: Summary, cur: str = "", icons: Icons | None = None) -> str:
    lines = [f"<b>{title}</b>", "", *format_totals(s, cur), *_sections(s, cur, icons)]
    if s.count == 0:
        lines += ["", "Операций пока нет."]
    return "\n".join(lines)


def format_month(s: Summary, today: date, cur: str = "", icons: Icons | None = None, pace: str = "") -> str:
    title = f"📅 {month_title(s.start).capitalize()} · 1–{today.day} {MONTHS_SHORT[today.month - 1]}"
    text = format_period(title, s, cur, icons)
    if pace:
        head, _, rest = text.partition("\n\n")
        lines = rest.split("\n")
        # темп — сразу после строк доход/расход/сальдо
        text = "\n".join([head, "", *lines[:3], pace, *lines[3:]])
    return text


def format_week(s: Summary, cur: str = "", icons: Icons | None = None) -> str:
    title = f"🗓 Неделя {fmt_short_date(s.start)}–{fmt_short_date(s.end)}"
    return format_period(title, s, cur, icons)


DEVIATION_MIN_PCT = 20


def top_deviations(r: MonthlyReport, limit: int = 3) -> list[tuple[str, Decimal, Decimal | None]]:
    """Категории расходов, сильнее всего отклонившиеся от среднего (в деньгах), |откл.| ≥ 20%.
    Возвращает (категория, сумма за месяц, среднее)."""
    if not r.avg_months:
        return []
    current = dict(r.summary.expense_by_category)
    out = []
    for name in set(current) | set(r.avg_by_category):
        amount, avg = current.get(name, ZERO), r.avg_by_category.get(name)
        base = avg or ZERO
        if base > 0 and abs(amount - base) / base * 100 < DEVIATION_MIN_PCT:
            continue
        out.append((abs(amount - base), name, amount, avg))
    out.sort(key=lambda x: (-x[0], x[1]))
    return [(name, amount, avg) for _, name, amount, avg in out[:limit] if _ > 0]


def report_conclusion(r: MonthlyReport, cur: str = "", icons: Icons | None = None) -> str:
    """Короткий вывод в одну строку."""
    s = r.summary
    if s.balance >= 0:
        rate = f", отложено {fmt_percent(s.savings_rate)} дохода" if s.savings_rate is not None else ""
        parts = [f"месяц в плюсе на {fmt_money(s.balance, cur)}{rate}"]
    else:
        parts = [f"расходы превысили доходы на {fmt_money(-s.balance, cur)}"]
    if r.avg_months and r.avg_expense:
        pct = round((s.expense - r.avg_expense) / r.avg_expense * 100)
        ratio = s.expense / r.avg_expense
        if ratio >= MULTIPLIER_FROM:
            mult = f"{ratio:.0f}" if ratio >= 10 else f"{ratio:.1f}".replace(".", ",")
            parts.append(f"траты в {mult} раза выше среднего")
        elif abs(pct) >= NEAR_AVERAGE_PCT:
            parts.append(f"траты {'выше' if pct > 0 else 'ниже'} среднего на {abs(pct)}%")
        devs = top_deviations(r)
        if devs:
            items = [
                f"{_icon(icons, OpType.EXPENSE, name)}{html.escape(name)} {compare_to_average(amount, avg, cur)}"
                for name, amount, avg in devs
            ]
            parts.append("сильнее всего отклонились: " + ", ".join(items))
    text = "; ".join(parts)
    return f"💬 {text[0].upper()}{text[1:]}."


def format_monthly_report(r: MonthlyReport, cur: str = "", icons: Icons | None = None) -> str:
    s = r.summary
    lines = [f"📊 <b>{month_title(s.start).capitalize()}</b> — итоги месяца", "", *format_totals(s, cur, r)]
    if s.count == 0:
        lines += ["", "За этот месяц операций нет."]
        return "\n".join(lines)
    if r.avg_months:
        months = ", ".join(MONTHS_SHORT[m.month - 1] for m in r.avg_months)
        lines.append(f"<i>Сравнение — со средним за {len(r.avg_months)} мес. ({months})</i>")
    lines += _sections(s, cur, icons, r)
    if not r.avg_months:
        lines += ["", "Для сравнения со средним пока мало данных."]
    lines += ["", report_conclusion(r, cur, icons)]
    return "\n".join(lines)


def format_category_report(
    name: str,
    ops: Sequence[Operation],
    today: date,
    cur: str = "",
    history_months: int = 3,
    max_items: int = 20,
    icon: str = "",
) -> str:
    first = month_start(today)
    in_cat = [op for op in ops if op.category == name and op.type != OpType.TRANSFER]
    current = sorted((op for op in in_cat if first <= op.date <= today), key=lambda o: o.date, reverse=True)
    total = sum((op.amount for op in current), ZERO)
    head = f"{icon} " if icon else "🏷 "
    lines = [f"{head}<b>{html.escape(name)}</b> — {month_title(first)}", f"Итого: <b>{fmt_money(total, cur)}</b> ({len(current)} оп.)"]
    for op in current[:max_items]:
        comment = f" {html.escape(op.comment)}" if op.comment else ""
        lines.append(f"{fmt_short_date(op.date)} · {fmt_money(op.amount, cur)}{comment}")
    if len(current) > max_items:
        lines.append(f"… и еще {len(current) - max_items}")
    hist, counted = [], []
    for i in range(1, history_months + 1):
        m = add_months(first, -i)
        amount = sum((op.amount for op in in_cat if m <= op.date <= month_end(m)), ZERO)
        hist.append(f"{MONTHS_SHORT[m.month - 1]} {m.year}: {fmt_money(amount, cur)}")
        if any(m <= op.date <= month_end(m) for op in ops):   # месяцы без операций в среднее не идут
            counted.append(amount)
    lines += ["", "<b>Прошлые месяцы:</b>", *hist]
    if counted:
        avg = sum(counted, ZERO) / len(counted)
        lines.append(f"Среднее за {len(counted)} мес.: <b>{fmt_money(avg.quantize(Decimal(1)), cur)}</b> в месяц")
    return "\n".join(lines)


# ---------- сравнение двух месяцев ----------

def format_compare(ops: Sequence[Operation], m1: date, m2: date, cur: str = "", icons: Icons | None = None) -> str:
    a, b = summarize_month(ops, m1), summarize_month(ops, m2)
    chg = lambda x, y: compare_to_average(y, x, cur, near="≈ без изменений") if x > 0 else ("🆕" if y > 0 else "")  # noqa: E731

    def row(label: str, x: Decimal, y: Decimal, signed: bool = False) -> str:
        return f"{label}: {fmt_money(x, cur, signed=signed)} → <b>{fmt_money(y, cur, signed=signed)}</b>  {chg(x, y)}".rstrip()

    lines = [
        f"⚖️ <b>{month_title(m1).capitalize()} → {month_title(m2)}</b>", "",
        row("💰 Доход", a.income, b.income),
        row("💸 Расход", a.expense, b.expense),
        f"Сальдо: {fmt_money(a.balance, cur, signed=True)} → <b>{fmt_money(b.balance, cur, signed=True)}</b>",
    ]
    for title, op_type, xs, ys in (
        ("💸 Расходы", OpType.EXPENSE, dict(a.expense_by_category), dict(b.expense_by_category)),
        ("💰 Доходы", OpType.INCOME, dict(a.income_by_category), dict(b.income_by_category)),
    ):
        names = sorted(set(xs) | set(ys), key=lambda n: (-max(xs.get(n, ZERO), ys.get(n, ZERO)), n))
        if not names:
            continue
        lines += ["", f"<b>{title}</b>"]
        for n in names:
            x, y = xs.get(n, ZERO), ys.get(n, ZERO)
            mark = chg(x, y) if y > 0 else "⬇️ не было"
            lines.append(f"{_icon(icons, op_type, n)}{html.escape(n)}: {fmt_money(x, cur)} → {fmt_money(y, cur)}  {mark}".rstrip())
    if not (a.count or b.count):
        lines += ["", "За эти месяцы операций нет."]
    return "\n".join(lines)


# ---------- разовые траты за год ----------

def format_big(bigs: Sequence[BigPurchase], ops: Sequence[Operation], today: date, cur: str = "", limit: int = 30) -> str:
    start = add_months(month_start(today), -11)
    items = sorted((b for b in bigs if start <= b.date <= today), key=lambda b: b.date, reverse=True)
    total = sum((b.amount for b in items), ZERO)
    expense = summarize(ops, start, today).expense
    head = f"🧾 <b>Разовые траты</b> с {MONTHS_SHORT[start.month - 1]} {start.year}: <b>{fmt_money(total, cur)}</b>"
    if expense > 0 and total > 0:
        head += f" · {fmt_percent(total / expense * 100)} всех расходов"
    lines = [head]
    if not items:
        lines.append("Пока нет. Крупные покупки записываются в лист «Большие покупки» (Дата, Что купил, Стоимость).")
    for b in items[:limit]:
        lines.append(f"• {fmt_date(b.date)} {html.escape(b.what) or 'покупка'} — {fmt_money(b.amount, cur)}")
    if len(items) > limit:
        lines.append(f"… и еще {len(items) - limit}")
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


def format_capital(
    s: CapitalSummary | None,
    cur: str = "",
    history: Sequence[tuple[date, Decimal]] = (),
    safety: Decimal | None = None,
) -> str:
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
        lines.append("📈 Рост: " + " · ".join(f"за {n} мес {m(v, True)}" for n, v in s.dynamics))
    elif s.prev_date is None:
        lines.append("Это первый снимок — сравнение появится со следующего.")
    if safety is not None:
        lines.append(f"🛟 Запас прочности: <b>{safety:.1f}</b> мес. обычных расходов (без разовых)".replace(".", ",", 1))
    if len(history) > 1:
        lines += ["", "<b>Снимки по месяцам:</b>"]
        rows = list(history)[-12:]
        for i in range(len(rows) - 1, -1, -1):
            d, total = rows[i]
            delta = f" ({m(total - rows[i - 1][1], True)})" if i > 0 else ""
            lines.append(f"{fmt_date(d)} — {m(total)}{delta}")
    return "\n".join(lines)


def snapshot_totals(entries: Iterable[BalanceEntry]) -> list[tuple[date, Decimal]]:
    """Итог по каждому снимку (по возрастанию даты)."""
    return [(d, sum(snap.values(), ZERO)) for d, snap in group_snapshots(entries)]


def avg_monthly_expense_wo_big(
    ops: Sequence[Operation], bigs: Sequence[BigPurchase], today: date, window: int = 3,
) -> Decimal | None:
    """Средний месячный расход без разовых за `window` прошлых полных месяцев (месяцы без операций не считаются)."""
    values = []
    for i in range(1, window + 1):
        m = add_months(month_start(today), -i)
        s = summarize_month(ops, m)
        if s.count:
            values.append(max(s.expense - big_total(bigs, m, month_end(m)), ZERO))
    return sum(values, ZERO) / len(values) if values else None


def safety_months(total: Decimal, ops: Sequence[Operation], bigs: Sequence[BigPurchase], today: date) -> Decimal | None:
    avg = avg_monthly_expense_wo_big(ops, bigs, today)
    return total / avg if avg else None


def month_pace(ops: Sequence[Operation], bigs: Sequence[BigPurchase], today: date) -> tuple[Decimal, Decimal | None]:
    """(потрачено с начала месяца без разовых, сколько обычно уходит к этому дню).
    «Обычно» — средний расход без разовых за 3 прошлых полных месяца пропорционально дню месяца."""
    first = month_start(today)
    spent = summarize(ops, first, today).expense - big_total(bigs, first, today)
    avg = avg_monthly_expense_wo_big(ops, bigs, today)
    expected = avg * today.day / month_end(today).day if avg is not None else None
    return max(spent, ZERO), expected


def format_pace(spent: Decimal, expected: Decimal | None, today: date, cur: str = "") -> str:
    head = f"⏱ <b>Темп</b>: потрачено {fmt_money(spent, cur)} (без разовых) к {today.day}-му числу"
    if expected is None:
        return head
    return f"{head}; обычно к этому дню ≈ {fmt_money(expected.quantize(Decimal(1)), cur)}  {compare_to_average(spent, expected, cur)}".rstrip()


# ---------- итоги месяца (отчет 1 числа) ----------

SNAPSHOT_GRACE_DAYS = 6       # снимок остатков до 6-го числа следующего месяца относится к закрытию месяца
RECONCILE_WARN_SHARE = Decimal("0.10")


def big_total(bigs: Iterable[BigPurchase], start: date, end: date) -> Decimal:
    return sum((b.amount for b in bigs if start <= b.date <= end), ZERO)


@dataclass
class MonthClose:
    report: MonthlyReport
    big: list[BigPurchase]
    capital: CapitalSummary | None
    rolling: Summary
    rolling_big: Decimal

    @property
    def big_total(self) -> Decimal:
        return sum((b.amount for b in self.big), ZERO)

    @property
    def expense_wo_big(self) -> Decimal:
        return max(self.report.summary.expense - self.big_total, ZERO)

    @property
    def balance_wo_big(self) -> Decimal:
        return self.report.summary.income - self.expense_wo_big

    @property
    def reconcile_diff(self) -> Decimal | None:
        """Прирост капитала − сальдо месяца (то, что операции не объясняют)."""
        c = self.capital
        if c is None or c.total_delta is None:
            return None
        return c.total_delta - self.report.summary.balance

    @property
    def reconcile_warning(self) -> bool:
        diff, income = self.reconcile_diff, self.report.summary.income
        return diff is not None and income > 0 and abs(diff) > income * RECONCILE_WARN_SHARE


def month_close(
    ops: Sequence[Operation],
    bigs: Sequence[BigPurchase],
    balances: Sequence[BalanceEntry],
    month: date,
    account_order: Sequence[str] = (),
) -> MonthClose:
    first, last = month_start(month), month_end(month)
    report = monthly_report(ops, first)
    cutoff = last + timedelta(days=SNAPSHOT_GRACE_DAYS)
    capital = capital_summary([b for b in balances if b.date <= cutoff], ops, account_order)
    if capital is not None and capital.date < last - timedelta(days=SNAPSHOT_GRACE_DAYS):
        capital = None   # свежего снимка на конец месяца нет
    rolling_start = add_months(first, -2)
    return MonthClose(
        report=report,
        big=sorted((b for b in bigs if first <= b.date <= last), key=lambda b: b.date),
        capital=capital,
        rolling=summarize(ops, rolling_start, last),
        rolling_big=big_total(bigs, rolling_start, last),
    )


def format_capital_block(c: CapitalSummary, cur: str = "") -> list[str]:
    """Капитал в отчете месяца: итог, счета, изменение (без строки «доход − траты» — она выше)."""
    m = lambda v, signed=False: fmt_money(v, cur, signed=signed)  # noqa: E731
    head = f"💼 <b>Капитал на {fmt_short_date(c.date)}</b>: <b>{m(c.total)}</b>"
    if c.total_delta is not None:
        head += f" ({m(c.total_delta, True)} к {fmt_short_date(c.prev_date)})"
    lines = [head]
    for a in c.accounts:
        delta = f" ({m(a.delta, True)})" if a.delta is not None else ""
        lines.append(f"• {html.escape(a.account)}: {m(a.amount)}{delta}")
    return lines


def format_month_close(mc: MonthClose, cur: str = "", icons: Icons | None = None) -> str:
    r, s = mc.report, mc.report.summary
    m = lambda v, signed=False: fmt_money(v, cur, signed=signed)  # noqa: E731
    lines = [f"📊 <b>{month_title(s.start).capitalize()}</b> — итоги месяца", "", *format_totals(s, cur, r)]
    if s.count == 0:
        lines += ["", "За этот месяц операций нет."]
    if mc.big:
        share = f" · {fmt_percent(mc.big_total / s.income * 100)} дохода" if s.income > 0 else ""
        lines += ["", f"🧾 Разовые траты: <b>{m(mc.big_total)}</b>{share}"]
        lines += [f"• {fmt_short_date(b.date)} {html.escape(b.what) or 'покупка'} — {m(b.amount)}" for b in mc.big]
        rate = f" · сбережения {fmt_percent(mc.balance_wo_big / s.income * 100)}" if s.income > 0 else ""
        lines.append(f"Без разовых: расход {m(mc.expense_wo_big)} · сальдо {m(mc.balance_wo_big, True)}{rate}")
    if r.avg_months:
        months = ", ".join(MONTHS_SHORT[x.month - 1] for x in r.avg_months)
        lines.append(f"<i>Сравнение — со средним за {len(r.avg_months)} мес. ({months})</i>")

    lines.append("")
    if mc.capital is None:
        lines.append("💼 Снимок остатков на конец месяца не заполнен — /snapshot")
    else:
        lines += format_capital_block(mc.capital, cur)
        diff = mc.reconcile_diff
        if diff is not None:
            lines.append(
                f"Сверка: прирост {m(mc.capital.total_delta, True)} − сальдо {m(s.balance, True)} "
                f"= <b>{m(diff, True)}</b> не учтено"
            )
            if mc.reconcile_warning:
                lines.append("⚠️ Расхождение больше 10% дохода — похоже, часть трат или доходов не записана.")

    if s.count:
        lines += _sections(s, cur, icons, r)
    roll = mc.rolling
    if roll.count:
        months = f"{MONTHS_SHORT[roll.start.month - 1]}–{MONTHS_SHORT[roll.end.month - 1]}"
        rate = f" · сбережения {fmt_percent(roll.savings_rate)}" if roll.savings_rate is not None else ""
        lines += ["", f"📆 <b>3 месяца ({months})</b>: доход {m(roll.income)}, расход {m(roll.expense)}, "
                      f"сальдо {m(roll.balance, True)}{rate}"]
    if not r.avg_months and s.count:
        lines += ["", "Для сравнения со средним пока мало данных."]
    if s.count:
        lines += ["", report_conclusion(r, cur, icons)]
    return "\n".join(lines)


# ---------- квартальный отчет ----------

QUARTER_START_MONTHS = (1, 4, 7, 10)


def capital_at_month_end(balances: Sequence[BalanceEntry], month: date) -> Decimal | None:
    """Итог снимка, закрывающего месяц: последний снимок в пределах ±6 дней от конца месяца."""
    end = month_end(month)
    lo, hi = end - timedelta(days=SNAPSHOT_GRACE_DAYS), end + timedelta(days=SNAPSHOT_GRACE_DAYS + 1)
    candidates = [(d, total) for d, total in snapshot_totals(balances) if lo < d <= hi]
    return candidates[-1][1] if candidates else None


@dataclass
class QuarterReport:
    months: list[date]                 # три полных месяца квартала
    summary: Summary
    monthly: list[Summary]
    big_total: Decimal
    capital_start: Decimal | None      # на конец месяца перед кварталом
    capital_end: Decimal | None


def quarter_report(ops: Sequence[Operation], bigs: Sequence[BigPurchase], balances: Sequence[BalanceEntry], today: date) -> QuarterReport:
    """Квартал — три полных месяца перед текущим."""
    first = add_months(month_start(today), -3)
    months = [add_months(first, i) for i in range(3)]
    last = month_end(months[-1])
    return QuarterReport(
        months=months,
        summary=summarize(ops, first, last),
        monthly=[summarize_month(ops, m) for m in months],
        big_total=big_total(bigs, first, last),
        capital_start=capital_at_month_end(balances, add_months(first, -1)),
        capital_end=capital_at_month_end(balances, months[-1]),
    )


def quarter_title(q: QuarterReport) -> str:
    a, b = q.months[0], q.months[-1]
    return f"Квартал: {MONTHS_NOMINATIVE[a.month - 1]}–{MONTHS_NOMINATIVE[b.month - 1]} {b.year}"


def format_quarter(q: QuarterReport, cur: str = "") -> str:
    s = q.summary
    m = lambda v, signed=False: fmt_money(v, cur, signed=signed)  # noqa: E731
    lines = [f"📈 <b>{quarter_title(q)}</b>", "", *format_totals(s, cur)]
    if q.big_total:
        wo = s.income - max(s.expense - q.big_total, ZERO)
        rate = f" · сбережения {fmt_percent(wo / s.income * 100)}" if s.income > 0 else ""
        lines.append(f"🧾 Разовые: {m(q.big_total)} · без них сальдо {m(wo, True)}{rate}")
    if q.capital_end is not None:
        line = f"💼 Капитал на конец квартала: <b>{m(q.capital_end)}</b>"
        if q.capital_start is not None:
            line += f" ({m(q.capital_end - q.capital_start, True)} за квартал)"
        lines.append(line)
    lines += ["", "<b>По месяцам</b>"]
    for ms in q.monthly:
        mark = "🟢" if ms.balance >= 0 else "🔴"
        lines.append(f"{mark} {MONTHS_SHORT[ms.start.month - 1]}: доход {m(ms.income)} · расход {m(ms.expense)} · сальдо {m(ms.balance, True)}")
    return "\n".join(lines)


def chart_series(ops: Sequence[Operation], balances: Sequence[BalanceEntry], today: date, n: int = 12):
    """Данные для картинки: последние n полных месяцев (без текущего)."""
    months = [add_months(month_start(today), -i) for i in range(n, 0, -1)]
    sums = [summarize_month(ops, mo) for mo in months]
    # обрезать пустые месяцы в начале истории
    while len(months) > 3 and sums[0].count == 0 and capital_at_month_end(balances, months[0]) is None:
        months, sums = months[1:], sums[1:]
    return (
        months,
        [s.income for s in sums],
        [s.expense for s in sums],
        [capital_at_month_end(balances, mo) for mo in months],
    )


# ---------- мягкие сигналы ----------

SIGNAL_CATEGORY_FROM_DAY = 20
OTHER_SHARE = Decimal("0.10")
OTHER_FROM_DAY = 10


def category_month_averages(ops: Sequence[Operation], today: date, window: int = 3) -> dict[str, Decimal]:
    """Средний расход по категориям за полный месяц (прошлые месяцы с операциями, текущий не входит)."""
    prev = [summarize_month(ops, add_months(month_start(today), -i)) for i in range(1, window + 1)]
    prev = [p for p in prev if p.count]
    if not prev:
        return {}
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in prev:
        for name, amount in p.expense_by_category:
            totals[name] += amount
    return {k: v / len(prev) for k, v in totals.items()}


def find_signals(
    ops: Sequence[Operation],
    balances: Sequence[BalanceEntry],
    today: date,
    cur: str = "",
    icons: Icons | None = None,
) -> list[tuple[str, str]]:
    """Список (ключ, текст). Ключ нужен, чтобы один и тот же сигнал не повторялся."""
    out: list[tuple[str, str]] = []
    first = month_start(today)
    key_month = first.strftime("%Y-%m")
    s = summarize(ops, first, today)

    if today.day >= SIGNAL_CATEGORY_FROM_DAY:
        avgs = category_month_averages(ops, today)
        for name, amount in s.expense_by_category:
            avg = avgs.get(name)
            if avg and amount > avg:
                out.append((
                    f"cat:{key_month}:{name}",
                    f"{_icon(icons, OpType.EXPENSE, name)}{html.escape(name)}: уже {fmt_money(amount, cur)} к {today.day}-му — "
                    f"больше, чем обычно уходит за весь месяц (≈ {fmt_money(avg.quantize(Decimal(1)), cur)})",
                ))

    other = dict(s.expense_by_category).get("Прочее", ZERO)
    if today.day >= OTHER_FROM_DAY and s.expense > 0 and other > s.expense * OTHER_SHARE:
        out.append((
            f"other:{key_month}",
            f"{_icon(icons, OpType.EXPENSE, 'Прочее')}«Прочее» — {fmt_percent(other / s.expense * 100)} расходов месяца "
            f"({fmt_money(other, cur)}). Возможно, стоит разнести по категориям или завести новую.",
        ))

    c = capital_summary(balances, ops)
    if c is not None and c.unaccounted is not None and c.ops_month is not None:
        income = summarize_month(ops, c.ops_month).income
        if income > 0 and abs(c.unaccounted) > income * RECONCILE_WARN_SHARE:
            out.append((
                f"reconcile:{c.date.isoformat()}",
                f"💼 Сверка за {MONTHS_NOMINATIVE[c.ops_month.month - 1]} не сходится на "
                f"{fmt_money(c.unaccounted, cur, signed=True)} — похоже, часть трат или доходов не записана.",
            ))
    return out
