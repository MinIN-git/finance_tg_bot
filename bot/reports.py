"""Расчеты и тексты отчетов. Чистые функции без обращения к хранилищу.

Правила оформления: суммы без копеек (в итогах — «тыс»), никаких процентов сбережений,
отметки «выше/ниже обычного» и стрелки — только при отклонении больше 30%,
«обычно» — среднее за 3 прошлых полных месяца без тяжелых трат.
"""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from .models import BalanceEntry, BigPurchase, Operation, OpType, is_heavy
from .textutil import (
    MONTHS_GENITIVE, MONTHS_NOMINATIVE, MONTHS_SHORT, fmt_date, fmt_k, fmt_money, fmt_percent, fmt_short_date,
    month_title,
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


def shift_months(d: date, n: int) -> date:
    """Та же дата n месяцев назад/вперед (день обрезается до конца месяца)."""
    first = add_months(d, n)
    return first.replace(day=min(d.day, month_end(first).day))


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def period_title(first: date, last: date) -> str:
    """«Июль – сентябрь» (год — если месяцы в разных годах или не текущий)."""
    a, b = MONTHS_NOMINATIVE[first.month - 1], MONTHS_NOMINATIVE[last.month - 1]
    if first.year != last.year:
        return f"{a.capitalize()} {first.year} – {b} {last.year}"
    return f"{a.capitalize()} – {b}" if first != last else a.capitalize()


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


# ---------- тяжелые траты ----------

def heavy_items(ops: Sequence[Operation], bigs: Sequence[BigPurchase], start: date, end: date) -> list[BigPurchase]:
    """Тяжелые траты периода: строки «Больших покупок» + операции с #тяжелое, которых в листе нет."""
    items = [b for b in bigs if start <= b.date <= end]
    known = {b.op_id for b in bigs if b.op_id}
    items += [
        BigPurchase(op.date, op.comment or op.category, op.amount, op.category, op.id)
        for op in ops if is_heavy(op) and start <= op.date <= end and op.id not in known
    ]
    return sorted(items, key=lambda b: (b.date, b.what))


def summarize_clean(ops: Sequence[Operation], bigs: Sequence[BigPurchase], start: date, end: date) -> Summary:
    """Сводка без тяжелых трат. Операции с #тяжелое просто не учитываются; старые строки листа
    (без операции) вычитаются из своей категории, а если категория не указана — только из общего расхода."""
    s = summarize([op for op in ops if not is_heavy(op)], start, end)
    heavy_ids = {op.id for op in ops if is_heavy(op)}
    cats = dict(s.expense_by_category)
    for b in bigs:
        if not (start <= b.date <= end) or (b.op_id and b.op_id in heavy_ids):
            continue
        s.expense = max(s.expense - b.amount, ZERO)
        if b.category in cats:
            cats[b.category] = cats[b.category] - b.amount
    s.expense_by_category = _sorted_totals({k: v for k, v in cats.items() if v > 0})
    return s


def summarize_clean_month(ops: Sequence[Operation], bigs: Sequence[BigPurchase], any_day: date) -> Summary:
    return summarize_clean(ops, bigs, month_start(any_day), month_end(any_day))


@dataclass
class Usual:
    """«Обычно» = среднее за прошлые полные месяцы (только месяцы с операциями), без тяжелых трат."""
    months: list[date]
    income: Decimal | None
    expense: Decimal | None
    by_category: dict[str, Decimal]


def usual(ops: Sequence[Operation], bigs: Sequence[BigPurchase], month: date, window: int = 3) -> Usual:
    first = month_start(month)
    prev = [summarize_clean_month(ops, bigs, add_months(first, -i)) for i in range(1, window + 1)]
    prev = [p for p in prev if p.count]
    if not prev:
        return Usual([], None, None, {})
    n = Decimal(len(prev))
    cats: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in prev:
        for name, amount in p.expense_by_category:
            cats[name] += amount
    return Usual(
        months=sorted(p.start for p in prev),
        income=sum((p.income for p in prev), ZERO) / n,
        expense=sum((p.expense for p in prev), ZERO) / n,
        by_category={k: v / n for k, v in cats.items()},
    )


# ---------- общие элементы оформления ----------

HIDDEN_TAGS = {"импорт", "тяжелое"}   # служебные теги в отчетах не показываем
DEVIATION_PCT = 30                    # меньше — «как обычно», без отметок
MULTIPLIER_FROM = 3                   # с ×3 вместо процентов — множитель
MIN_NOTABLE = Decimal(1000)           # мелкие категории не попадают в «выше/ниже обычного»

Icons = Mapping[tuple[OpType, str], str]


def _icon(icons: Icons | None, op_type: OpType, name: str) -> str:
    emoji = (icons or {}).get((op_type, name), "")
    return f"{emoji} " if emoji else ""


def _label(icons: Icons | None, op_type: OpType, name: str) -> str:
    return f"{_icon(icons, op_type, name)}{html.escape(name)}"


def deviation(current: Decimal, avg: Decimal | None) -> str:
    """«⬆️ +45%», «⬇️ −40%», «⬆️ ×3,6»; пусто, если отклонение не больше 30% или сравнивать не с чем."""
    if avg is None or avg <= 0:
        return ""
    ratio = current / avg
    pct = (ratio - 1) * 100
    if abs(pct) <= DEVIATION_PCT:
        return ""
    if ratio >= MULTIPLIER_FROM:
        mult = f"{ratio:.0f}" if ratio >= 10 else f"{ratio:.1f}".replace(".", ",")
        return f"⬆️ ×{mult}"
    return f"⬆️ +{round(pct)}%" if pct > 0 else f"⬇️ −{abs(round(pct))}%"


def _result_line(balance: Decimal, cur: str, k: bool = True) -> str:
    mark = "🟢" if balance >= 0 else "🔴"
    value = fmt_k(balance, cur, signed=True) if k else fmt_money(balance, cur, signed=True)
    return f"{mark} Итог <b>{value}</b>"


def category_lines(
    items: Sequence[tuple[str, Decimal]], op_type: OpType, cur: str = "", icons: Icons | None = None,
    limit: int | None = None,
) -> list[str]:
    shown = [(n, a) for n, a in items if a > 0][:limit]
    return [f"{_label(icons, op_type, n)} — {fmt_money(a, cur)}" for n, a in shown]


def format_tags(s: Summary, cur: str = "") -> list[str]:
    lines = []
    for tag, st in s.tags.items():
        if tag in HIDDEN_TAGS or tag.startswith("поездка-"):
            continue
        parts = []
        if st.expense:
            parts.append(f"расход {fmt_money(st.expense, cur)}")
        if st.income:
            parts.append(f"доход {fmt_money(st.income, cur)}")
        lines.append(f"🏷 #{html.escape(tag)}: {', '.join(parts)}")
    return lines


# ---------- текущий месяц (/month) и неделя ----------

TOP_NOW = 5


def format_now(title: str, s: Summary, heavy_total: Decimal, cur: str = "", icons: Icons | None = None) -> str:
    """Только текущий период, без сравнений с прошлым."""
    lines = [
        title,
        f"💸 <b>{fmt_money(s.expense, cur)}</b> потрачено",
        f"🧾 из них тяжелые: {fmt_money(heavy_total, cur) if heavy_total else '—'}",
        f"💰 <b>{fmt_money(s.income, cur)}</b> пришло",
    ]
    top = category_lines(s.expense_by_category, OpType.EXPENSE, cur, icons, TOP_NOW)
    if top:
        lines += ["", "<b>Больше всего:</b>", *top]
    elif s.count == 0:
        lines += ["", "Записей пока нет."]
    return "\n".join(lines)


def month_now_title(today: date) -> str:
    return f"📅 <b>{MONTHS_NOMINATIVE[today.month - 1].capitalize()}</b>, {today.day}-й день"


def week_title(start: date, end: date) -> str:
    return f"🗓 <b>Неделя</b> {fmt_short_date(start)}–{fmt_short_date(end)}"


def format_all_categories(title: str, s: Summary, cur: str = "", icons: Icons | None = None) -> str:
    lines = [title]
    if s.expense_by_category:
        lines += ["", "<b>💸 Расходы</b>", *category_lines(s.expense_by_category, OpType.EXPENSE, cur, icons)]
    if s.income_by_category:
        lines += ["", "<b>💰 Доходы</b>", *category_lines(s.income_by_category, OpType.INCOME, cur, icons)]
    tags = format_tags(s, cur)
    if tags:
        lines += ["", "<b>Теги</b>", *tags]
    if not s.count:
        lines += ["", "Записей нет."]
    return "\n".join(lines)


# ---------- остатки по счетам ----------

def group_snapshots(entries: Iterable[BalanceEntry]) -> list[tuple[date, dict[str, Decimal]]]:
    """Снимки по датам (по возрастанию): дата -> {счет: сумма}."""
    by_date: dict[date, dict[str, Decimal]] = defaultdict(dict)
    for e in entries:
        by_date[e.date][e.account] = e.amount
    return sorted(by_date.items())


def snapshot_totals(entries: Iterable[BalanceEntry]) -> list[tuple[date, Decimal]]:
    """Итог по каждому снимку (по возрастанию даты)."""
    return [(d, sum(snap.values(), ZERO)) for d, snap in group_snapshots(entries)]


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
    prev_total: Decimal | None = None
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
        s.prev_total = sum(prev.values(), ZERO)
        s.total_delta = total - s.prev_total

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


def _on_date(d: date) -> str:
    return f"{d.day} {MONTHS_GENITIVE[d.month - 1]}"


def safety_months(total: Decimal, ops: Sequence[Operation], bigs: Sequence[BigPurchase], today: date) -> Decimal | None:
    """Сколько месяцев обычных трат (без тяжелых) покрывает капитал."""
    u = usual(ops, bigs, today)
    return total / u.expense if u.expense else None


def format_capital(
    s: CapitalSummary | None,
    cur: str = "",
    history: Sequence[tuple[date, Decimal]] = (),
    safety: Decimal | None = None,
) -> str:
    if s is None:
        return "Остатков пока нет. Пройдите опрос по счетам: /snapshot"
    m = lambda v, signed=False: fmt_money(v, cur, signed=signed)  # noqa: E731
    head = f"💼 <b>Капитал на {_on_date(s.date)}</b>: <b>{m(s.total)}</b>"
    if s.total_delta is not None:
        head += f" ({m(s.total_delta, True)} к {_on_date(s.prev_date)})"
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
        lines.append(f"🛟 Запас прочности: <b>{safety:.1f}</b> мес. обычных трат".replace(".", ",", 1))
    if len(history) > 1:
        lines += ["", "<b>Снимки по месяцам:</b>"]
        rows = list(history)[-12:]
        for i in range(len(rows) - 1, -1, -1):
            d, total = rows[i]
            delta = f" ({m(total - rows[i - 1][1], True)})" if i > 0 else ""
            lines.append(f"{fmt_date(d)} — {m(total)}{delta}")
    return "\n".join(lines)


SNAPSHOT_GRACE_DAYS = 6       # снимок до 6-го числа следующего месяца относится к закрытию месяца
RECONCILE_WARN_SHARE = Decimal("0.10")


def capital_at_month_end(balances: Sequence[BalanceEntry], month: date) -> Decimal | None:
    """Итог снимка, закрывающего месяц: последний снимок в пределах ±6 дней от конца месяца."""
    end = month_end(month)
    lo, hi = end - timedelta(days=SNAPSHOT_GRACE_DAYS), end + timedelta(days=SNAPSHOT_GRACE_DAYS + 1)
    candidates = [(d, total) for d, total in snapshot_totals(balances) if lo < d <= hi]
    return candidates[-1][1] if candidates else None


# ---------- итоги месяца (1 числа и /report) ----------

LIST_LIMIT = 5


@dataclass
class MonthClose:
    month: date
    clean: Summary                 # без тяжелых
    full: Summary                  # с тяжелыми
    usual: Usual
    heavy: list[BigPurchase]
    capital: CapitalSummary | None

    @property
    def heavy_total(self) -> Decimal:
        return sum((b.amount for b in self.heavy), ZERO)


def month_close(
    ops: Sequence[Operation],
    bigs: Sequence[BigPurchase],
    balances: Sequence[BalanceEntry],
    month: date,
    account_order: Sequence[str] = (),
) -> MonthClose:
    first, last = month_start(month), month_end(month)
    cutoff = last + timedelta(days=SNAPSHOT_GRACE_DAYS)
    capital = capital_summary([b for b in balances if b.date <= cutoff], ops, account_order)
    if capital is not None and capital.date < last - timedelta(days=SNAPSHOT_GRACE_DAYS):
        capital = None   # свежего снимка на начало следующего месяца нет
    return MonthClose(
        month=first,
        clean=summarize_clean(ops, bigs, first, last),
        full=summarize(ops, first, last),
        usual=usual(ops, bigs, first),
        heavy=heavy_items(ops, bigs, first, last),
        capital=capital,
    )


def above_below(mc: MonthClose) -> tuple[list[tuple[str, Decimal, Decimal]], list[tuple[str, Decimal, Decimal]]]:
    """Категории выше и ниже обычного больше чем на 30%: (категория, за месяц, обычно)."""
    if not mc.usual.months:
        return [], []
    current = dict(mc.clean.expense_by_category)
    above, below = [], []
    for name in set(current) | set(mc.usual.by_category):
        amount, avg = current.get(name, ZERO), mc.usual.by_category.get(name, ZERO)
        if max(amount, avg) < MIN_NOTABLE or avg <= 0:
            continue
        if amount > avg * (1 + Decimal(DEVIATION_PCT) / 100):
            above.append((name, amount, avg))
        elif amount < avg * (1 - Decimal(DEVIATION_PCT) / 100):
            below.append((name, amount, avg))
    above.sort(key=lambda x: -(x[1] - x[2]))
    below.sort(key=lambda x: -(x[2] - x[1]))
    return above[:LIST_LIMIT], below[:LIST_LIMIT]


def _usual_line(name: str, amount: Decimal, avg: Decimal, cur: str, icons: Icons | None) -> str:
    return f"{_label(icons, OpType.EXPENSE, name)} {fmt_k(amount, cur)} · обычно {fmt_k(avg)}"


def _capital_start_line(c: CapitalSummary | None, cur: str) -> str:
    if c is None:
        return "💼 Капитал: снимок остатков не заполнен — /snapshot"
    line = f"💼 Капитал на {_on_date(c.date)} <b>{fmt_k(c.total, cur)}</b>"
    if c.prev_date is not None:
        line += f" (на {_on_date(c.prev_date)} {fmt_k(c.prev_total)}, {fmt_k(c.total_delta, signed=True)})"
    return line


def format_month_close(mc: MonthClose, cur: str = "", icons: Icons | None = None) -> str:
    s = mc.clean
    lines = [
        f"📊 <b>{MONTHS_NOMINATIVE[mc.month.month - 1].capitalize()}</b> без тяжелых трат",
        f"💰 Доход <b>{fmt_k(s.income, cur)}</b>",
        f"💸 Расход <b>{fmt_k(s.expense, cur)}</b>",
        _result_line(s.balance, cur),
        _capital_start_line(mc.capital, cur),
    ]
    if s.count == 0 and not mc.heavy:
        lines += ["", "За этот месяц записей нет."]
        return "\n".join(lines)
    above, below = above_below(mc)
    if above:
        lines += ["", "<b>Выше обычного:</b>", *(_usual_line(*x, cur, icons) for x in above)]
    if below:
        lines += ["", "<b>Ниже обычного:</b>", *(_usual_line(*x, cur, icons) for x in below)]
    if not mc.usual.months:
        lines += ["", "Сравнивать пока не с чем — «обычно» появится после первого полного месяца."]
    if mc.heavy:
        lines += ["", f"🧾 Тяжелые траты: {len(mc.heavy)} на {fmt_k(mc.heavy_total, cur)}"]
    return "\n".join(lines)


def format_heavy_list(items: Sequence[BigPurchase], cur: str = "", icons: Icons | None = None) -> list[str]:
    lines = []
    for b in items:
        icon = _icon(icons, OpType.EXPENSE, b.category) if b.category else "❔ "
        lines.append(f"{fmt_short_date(b.date)} {icon}{html.escape(b.what) or 'покупка'} — {fmt_money(b.amount, cur)}")
    return lines


NO_CATEGORY_HINT = (
    "❔ — категория не указана: такая трата вычитается только из общего расхода. "
    "Впишите категорию в лист «Большие покупки», затем /refresh."
)


def heavy_year(ops: Sequence[Operation], bigs: Sequence[BigPurchase], last: date) -> tuple[int, Decimal, Decimal]:
    """За 12 месяцев до конца `last`: (число тяжелых трат, их сумма, доля в расходах, %)."""
    start = add_months(month_start(last), -11)
    items = heavy_items(ops, bigs, start, last)
    total = sum((b.amount for b in items), ZERO)
    expense = summarize(ops, start, last).expense
    return len(items), total, (total / expense * 100 if expense > 0 else ZERO)


def format_month_heavy(
    mc: MonthClose, ops: Sequence[Operation], bigs: Sequence[BigPurchase], cur: str = "", icons: Icons | None = None,
) -> str:
    title = f"🧾 <b>Тяжелые траты</b> · {MONTHS_NOMINATIVE[mc.month.month - 1]}"
    if not mc.heavy:
        lines = [title, "В этом месяце тяжелых трат нет."]
    else:
        lines = [title, *format_heavy_list(mc.heavy, cur, icons)]
        f = mc.full
        lines += [
            "", "<b>С ними месяц выглядит так:</b>",
            f"💸 Расход {fmt_k(f.expense, cur)} (без них {fmt_k(mc.clean.expense)})",
            _result_line(f.balance, cur) + f" (без них {fmt_k(mc.clean.balance, signed=True)})",
        ]
        clean_cats, full_cats = dict(mc.clean.expense_by_category), dict(f.expense_by_category)
        touched = sorted({b.category for b in mc.heavy if b.category}, key=lambda n: -full_cats.get(n, ZERO))
        for name in touched:
            lines.append(
                f"{_label(icons, OpType.EXPENSE, name)} {fmt_k(full_cats.get(name, ZERO), cur)} "
                f"(без них {fmt_k(clean_cats.get(name, ZERO))})"
            )
        if any(not b.category for b in mc.heavy):
            lines += ["", NO_CATEGORY_HINT]
    n, total, share = heavy_year(ops, bigs, month_end(mc.month))
    if n:
        lines += ["", f"За 12 месяцев: {n} трат на {fmt_k(total, cur)} · {fmt_percent(share)} всех расходов"]
    return "\n".join(lines)


def format_month_categories(mc: MonthClose, cur: str = "", icons: Icons | None = None) -> str:
    title = f"📋 <b>{MONTHS_NOMINATIVE[mc.month.month - 1].capitalize()}</b>: все категории без тяжелых трат"
    return format_all_categories(title, mc.clean, cur, icons)


# ---------- статистика за несколько месяцев (/stats) ----------

TREND_MIN_CHANGE = Decimal(2000)
STATS_PERIODS = (3, 6, 12)
QUARTER_START_MONTHS = (1, 4, 7, 10)


@dataclass
class Trend:
    name: str
    before: Decimal     # среднее в первой половине периода
    after: Decimal      # среднее во второй половине


@dataclass
class Stats:
    months: list[date]
    monthly: list[Summary]                  # без тяжелых
    capital: list[Decimal | None]           # на конец каждого месяца
    capital_before: Decimal | None          # на конец месяца перед периодом
    rising: list[Trend]
    falling: list[Trend]
    heavy: list[BigPurchase]

    @property
    def capital_change(self) -> Decimal | None:
        end = self.capital[-1] if self.capital else None
        if end is None or self.capital_before is None:
            return None
        return end - self.capital_before

    def category_series(self, top: int = 6) -> dict[str, list[Decimal]]:
        """Помесячные суммы для самых крупных категорий периода (для графиков)."""
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for s in self.monthly:
            for name, amount in s.expense_by_category:
                totals[name] += amount
        names = [n for n, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:top]]
        return {n: [dict(s.expense_by_category).get(n, ZERO) for s in self.monthly] for n in names}


def stats(ops: Sequence[Operation], bigs: Sequence[BigPurchase], balances: Sequence[BalanceEntry], today: date, n: int = 3) -> Stats:
    """n полных месяцев перед текущим."""
    first = add_months(month_start(today), -n)
    months = [add_months(first, i) for i in range(n)]
    monthly = [summarize_clean_month(ops, bigs, m) for m in months]
    with_data = [s for s in monthly if s.count]
    rising, falling = [], []
    if len(with_data) >= 2:
        half = max(1, len(with_data) // 2)
        early, late = with_data[:half], with_data[-half:]
        names = {name for s in with_data for name, _ in s.expense_by_category}
        for name in names:
            before = sum((dict(s.expense_by_category).get(name, ZERO) for s in early), ZERO) / len(early)
            after = sum((dict(s.expense_by_category).get(name, ZERO) for s in late), ZERO) / len(late)
            if abs(after - before) < TREND_MIN_CHANGE:
                continue
            if before > 0 and after > before * (1 + Decimal(DEVIATION_PCT) / 100) or before == 0 and after > 0:
                rising.append(Trend(name, before, after))
            elif after < before * (1 - Decimal(DEVIATION_PCT) / 100):
                falling.append(Trend(name, before, after))
        rising.sort(key=lambda t: -(t.after - t.before))
        falling.sort(key=lambda t: -(t.before - t.after))
    return Stats(
        months=months,
        monthly=monthly,
        capital=[capital_at_month_end(balances, m) for m in months],
        capital_before=capital_at_month_end(balances, add_months(first, -1)),
        rising=rising[:LIST_LIMIT],
        falling=falling[:LIST_LIMIT],
        heavy=heavy_items(ops, bigs, first, month_end(months[-1])),
    )


def stats_title(st: Stats) -> str:
    return period_title(st.months[0], st.months[-1])


def format_stats(st: Stats, cur: str = "", icons: Icons | None = None) -> str:
    lines = [f"📈 <b>{stats_title(st)}</b>, без тяжелых", ""]
    trend = lambda t: f"{_label(icons, OpType.EXPENSE, t.name)} ({fmt_k(t.before)} → {fmt_k(t.after, cur)})"  # noqa: E731
    if not any(s.count for s in st.monthly):
        lines.append("За этот период записей нет.")
    else:
        if st.rising:
            lines.append("• Расходы растут: " + ", ".join(trend(t) for t in st.rising))
        if st.falling:
            lines.append("• Расходы снижаются: " + ", ".join(trend(t) for t in st.falling))
        if not st.rising and not st.falling:
            lines.append("• Расходы по категориям без заметных изменений")
    change = st.capital_change
    if change is not None:
        verb = "вырос" if change >= 0 else "уменьшился"
        lines.append(f"• Капитал {verb} на {fmt_k(abs(change), cur)}")
    if st.heavy:
        lines += ["", f"🧾 Тяжелые: {fmt_k(sum((b.amount for b in st.heavy), ZERO), cur)}"]
    return "\n".join(lines)


def format_stats_heavy(st: Stats, cur: str = "", icons: Icons | None = None) -> str:
    lines = [f"🧾 <b>Тяжелые траты</b> · {stats_title(st)}"]
    if not st.heavy:
        lines.append("За этот период тяжелых трат нет.")
        return "\n".join(lines)
    lines += format_heavy_list(st.heavy, cur, icons)
    lines.append(f"Всего: {fmt_k(sum((b.amount for b in st.heavy), ZERO), cur)}")
    if any(not b.category for b in st.heavy):
        lines += ["", NO_CATEGORY_HINT]
    return "\n".join(lines)


# ---------- категория (/cat) ----------

def format_category_report(
    name: str,
    ops: Sequence[Operation],
    bigs: Sequence[BigPurchase],
    today: date,
    cur: str = "",
    max_items: int = 20,
    icon: str = "",
) -> str:
    first = month_start(today)
    in_cat = [op for op in ops if op.category == name and op.type != OpType.TRANSFER]
    current = sorted((op for op in in_cat if first <= op.date <= today), key=lambda o: o.date, reverse=True)
    total = sum((op.amount for op in current), ZERO)
    head = f"{icon} " if icon else "🏷 "
    lines = [f"{head}<b>{html.escape(name)}</b> — {month_title(first)}", f"Итого: <b>{fmt_money(total, cur)}</b>"]
    for op in current[:max_items]:
        comment = f" {html.escape(op.comment)}" if op.comment else ""
        heavy = " 🧾" if is_heavy(op) else ""
        lines.append(f"{fmt_short_date(op.date)} · {fmt_money(op.amount, cur)}{comment}{heavy}")
    if len(current) > max_items:
        lines.append(f"… и еще {len(current) - max_items}")
    hist = []
    for i in range(1, 4):
        m = add_months(first, -i)
        amount = dict(summarize_clean_month(ops, bigs, m).expense_by_category).get(name, ZERO)
        hist.append(f"{MONTHS_SHORT[m.month - 1]} {m.year}: {fmt_money(amount, cur)}")
    lines += ["", "<b>Прошлые месяцы</b> (без тяжелых):", *hist]
    avg = usual(ops, bigs, first).by_category.get(name)
    if avg:
        lines.append(f"Обычно: <b>{fmt_money(avg, cur)}</b> в месяц")
    return "\n".join(lines)


# ---------- сравнение двух месяцев (/compare) ----------

def format_compare(
    ops: Sequence[Operation], bigs: Sequence[BigPurchase], m1: date, m2: date, cur: str = "", icons: Icons | None = None,
) -> str:
    a, b = summarize_clean_month(ops, bigs, m1), summarize_clean_month(ops, bigs, m2)

    def row(label: str, x: Decimal, y: Decimal, signed: bool = False) -> str:
        return (f"{label} {fmt_k(x, signed=signed)} → <b>{fmt_k(y, cur, signed=signed)}</b>  "
                f"{deviation(y, x) if not signed else ''}").rstrip()

    lines = [
        f"⚖️ <b>{month_title(m1).capitalize()} → {month_title(m2)}</b>, без тяжелых", "",
        row("💰 Доход", a.income, b.income),
        row("💸 Расход", a.expense, b.expense),
        row("🟢 Итог" if b.balance >= 0 else "🔴 Итог", a.balance, b.balance, signed=True),
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
            mark = "🆕" if x == 0 else ("не было" if y == 0 else deviation(y, x))
            lines.append(f"{_label(icons, op_type, n)}: {fmt_money(x, cur)} → {fmt_money(y, cur)}  {mark}".rstrip())
    if not (a.count or b.count):
        lines += ["", "За эти месяцы записей нет."]
    return "\n".join(lines)


# ---------- тяжелые траты за год (/big) ----------

def format_big(
    ops: Sequence[Operation], bigs: Sequence[BigPurchase], today: date, cur: str = "", icons: Icons | None = None,
    limit: int = 30,
) -> str:
    start = add_months(month_start(today), -11)
    items = sorted(heavy_items(ops, bigs, start, today), key=lambda b: b.date, reverse=True)
    n, total, share = heavy_year(ops, bigs, today)
    head = f"🧾 <b>Тяжелые траты</b> с {MONTHS_SHORT[start.month - 1]} {start.year}"
    if not items:
        return f"{head}\nПока нет. Пометьте трату тегом: <code>283000 отпуск #тяжелое</code>"
    lines = [f"{head}: {n} на <b>{fmt_k(total, cur)}</b> · {fmt_percent(share)} всех расходов", ""]
    lines += format_heavy_list(items[:limit], cur, icons)
    if len(items) > limit:
        lines.append(f"… и еще {len(items) - limit}")
    if any(not b.category for b in items):
        lines += ["", NO_CATEGORY_HINT]
    return "\n".join(lines)


# ---------- мягкие сигналы ----------

SIGNAL_CATEGORY_FROM_DAY = 20
OTHER_SHARE = Decimal("0.10")
OTHER_FROM_DAY = 10


def find_signals(
    ops: Sequence[Operation],
    bigs: Sequence[BigPurchase],
    balances: Sequence[BalanceEntry],
    today: date,
    cur: str = "",
    icons: Icons | None = None,
) -> list[tuple[str, str]]:
    """Список (ключ, текст). Ключ нужен, чтобы один и тот же сигнал не повторялся. Всё без тяжелых трат."""
    out: list[tuple[str, str]] = []
    first = month_start(today)
    key_month = first.strftime("%Y-%m")
    s = summarize_clean(ops, bigs, first, today)

    if today.day >= SIGNAL_CATEGORY_FROM_DAY:
        avgs = usual(ops, bigs, today).by_category
        for name, amount in s.expense_by_category:
            avg = avgs.get(name)
            if avg and amount > avg:
                out.append((
                    f"cat:{key_month}:{name}",
                    f"{_label(icons, OpType.EXPENSE, name)}: уже {fmt_money(amount, cur)} к {today.day}-му — "
                    f"больше, чем обычно уходит за весь месяц ({fmt_money(avg, cur)})",
                ))

    other = dict(s.expense_by_category).get("Прочее", ZERO)
    if today.day >= OTHER_FROM_DAY and s.expense > 0 and other > s.expense * OTHER_SHARE:
        out.append((
            f"other:{key_month}",
            f"{_label(icons, OpType.EXPENSE, 'Прочее')} — {fmt_percent(other / s.expense * 100)} расходов месяца "
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
