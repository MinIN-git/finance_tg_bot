"""Расчеты и тексты отчетов. Чистые функции без обращения к хранилищу.

Траты делятся на три потока: обычные, тяжелые, командировки; «потрачено» — сумма трех.
«Обычно» — медиана обычных трат категории за 6 прошлых полных месяцев; ▲/▼ — только если значение
вышло за min/max этих месяцев и отличается от медианы больше чем на 2 000. /report — сухая статистика
без сравнений; тенденции — только в /stats. Суммы без копеек.
"""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from .models import (
    TRIP_ITEM_ICONS, BalanceEntry, BigPurchase, Operation, OpType, Trip, is_heavy, is_regular, is_trip, trip_tag,
)
from .textutil import (
    MONTHS_GENITIVE, MONTHS_NOMINATIVE, MONTHS_SHORT, fmt_date, fmt_k, fmt_money, fmt_percent, fmt_short_date,
    fmt_trip_range, month_title,
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

def heavy_items(ops: Sequence[Operation], start: date, end: date) -> list[BigPurchase]:
    """Тяжелые траты периода — операции вида «тяжелая» (лист «Большие покупки» — их зеркало)."""
    items = [
        BigPurchase(op.date, op.comment or op.category, op.amount, op.category, op.id)
        for op in ops if is_heavy(op) and start <= op.date <= end
    ]
    return sorted(items, key=lambda b: (b.date, b.what))


def trip_spending(ops: Sequence[Operation], start: date, end: date) -> Decimal:
    return sum((op.amount for op in ops if is_trip(op) and start <= op.date <= end), ZERO)


def summarize_clean(ops: Sequence[Operation], start: date, end: date) -> Summary:
    """Сводка только по обычным тратам: тяжелые и командировки — отдельные потоки,
    в разбивку по категориям и в «обычно» они не входят. Доходы — все."""
    return summarize([op for op in ops if is_regular(op) or op.type != OpType.EXPENSE], start, end)


def summarize_clean_month(ops: Sequence[Operation], any_day: date) -> Summary:
    return summarize_clean(ops, month_start(any_day), month_end(any_day))


USUAL_WINDOW = 6                  # «обычно» — медиана за столько прошлых полных месяцев
MARK_MIN_DIFF = Decimal(2000)     # ▲/▼ — только если вышло за min/max окна и отличие от медианы больше
TREND_PCT = Decimal(20)           # «растут/снижаются»: среднее 3 последних vs 3 предыдущих месяцев
TREND_MIN_DIFF = Decimal(2000)
TREND_MONTHS = 6


def median(values: Sequence[Decimal]) -> Decimal:
    v = sorted(values)
    n = len(v)
    if not n:
        return ZERO
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


@dataclass
class Usual:
    """«Обычно» для месяца: обычные траты прошлых полных месяцев окна (только месяцы с операциями)."""
    months: list[date]
    history: dict[str, list[Decimal]]       # категория -> суммы по месяцам окна (0, если не было)
    totals: list[Decimal]                   # обычные траты по месяцам окна
    incomes: list[Decimal]

    @property
    def by_category(self) -> dict[str, Decimal]:
        return {k: median(v) for k, v in self.history.items()}

    @property
    def expense(self) -> Decimal | None:
        return median(self.totals) if self.totals else None

    @property
    def income(self) -> Decimal | None:
        return median(self.incomes) if self.incomes else None

    def band(self, name: str) -> tuple[Decimal, Decimal] | None:
        hist = self.history.get(name) or ([ZERO] * len(self.months) if self.months else None)
        return (min(hist), max(hist)) if hist else None

    def marker(self, name: str, value: Decimal) -> str:
        """▲/▼, только если значение вышло за min/max прошлых месяцев и отличается от медианы больше чем на 2 000."""
        band = self.band(name)
        if band is None:
            return ""
        med = median(self.history.get(name) or [ZERO] * len(self.months))
        if value > band[1] and value - med > MARK_MIN_DIFF:
            return "▲"
        if value < band[0] and med - value > MARK_MIN_DIFF:
            return "▼"
        return ""


def usual(ops: Sequence[Operation], month: date, window: int = USUAL_WINDOW) -> Usual:
    first = month_start(month)
    prev = [summarize_clean_month(ops, add_months(first, -i)) for i in range(window, 0, -1)]
    prev = [p for p in prev if p.count]
    names = {name for p in prev for name, _ in p.expense_by_category}
    return Usual(
        months=[p.start for p in prev],
        history={n: [dict(p.expense_by_category).get(n, ZERO) for p in prev] for n in names},
        totals=[p.expense for p in prev],
        incomes=[p.income for p in prev],
    )


@dataclass
class Trend:
    name: str
    before: Decimal     # среднее за 3 месяца до
    after: Decimal      # среднее за последние 3 месяца


def trends(ops: Sequence[Operation], last_month: date) -> tuple[list[Trend], list[Trend]] | None:
    """Растут/снижаются: среднее последних 3 месяцев против 3 предыдущих, отличие > 20% и > 2 000.
    None — истории меньше 6 месяцев (выводов не делаем)."""
    months = [add_months(month_start(last_month), -i) for i in range(TREND_MONTHS - 1, -1, -1)]
    sums = [summarize_clean_month(ops, m) for m in months]
    if not all(s.count for s in sums):
        return None
    half = TREND_MONTHS // 2
    names = {name for s in sums for name, _ in s.expense_by_category}
    rising, falling = [], []
    for name in names:
        vals = [dict(s.expense_by_category).get(name, ZERO) for s in sums]
        before, after = sum(vals[:half], ZERO) / half, sum(vals[half:], ZERO) / half
        diff = after - before
        if abs(diff) <= TREND_MIN_DIFF:
            continue
        if before == 0 or abs(diff) / before * 100 > TREND_PCT:
            (rising if diff > 0 else falling).append(Trend(name, before, after))
    rising.sort(key=lambda t: -(t.after - t.before))
    falling.sort(key=lambda t: t.after - t.before)
    return rising, falling


# ---------- общие элементы оформления ----------

HIDDEN_TAGS = {"импорт", "тяжелое"}   # служебные теги в отчетах не показываем
DEVIATION_PCT = 30                    # для командировок: меньше — «как обычно»
MULTIPLIER_FROM = 3                   # с ×3 вместо процентов — множитель

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


def _streams(regular: Decimal, heavy: Decimal, trips: Decimal, cur: str) -> list[str]:
    dash = lambda v: fmt_money(v, cur) if v else "—"  # noqa: E731
    return [
        f"   🛒 обычные {dash(regular)}",
        f"   🧾 тяжелые {dash(heavy)}",
        f"   ✈️ командировки {dash(trips)}",
    ]


def format_now(
    title: str, full: Summary, regular: Summary, heavy: Decimal, trips: Decimal, cur: str = "", icons: Icons | None = None,
) -> str:
    """Только текущий период, без сравнений с прошлым: потрачено по трем потокам, пришло, больше всего."""
    lines = [
        title,
        f"💸 <b>{fmt_money(full.expense, cur)}</b> потрачено",
        *_streams(regular.expense, heavy, trips, cur),
        f"💰 <b>{fmt_money(full.income, cur)}</b> пришло",
    ]
    top = category_lines(regular.expense_by_category, OpType.EXPENSE, cur, icons, TOP_NOW)
    if top:
        lines += ["", "<b>Больше всего</b> (обычные):", *top]
    elif full.count == 0:
        lines += ["", "Записей пока нет."]
    return "\n".join(lines)


def month_now_title(today: date) -> str:
    return f"📅 <b>{MONTHS_NOMINATIVE[today.month - 1].capitalize()}</b>, {today.day}-й день"


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
    ops_start: date | None = None        # операции между снимками: с даты прошлого снимка
    ops_end: date | None = None          # по день перед последним снимком (включительно)
    ops_balance: Decimal | None = None   # доход − траты за эти дни (без переводов)
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

    if prev_date is not None:
        # Изменение капитала между снимками сверяем с операциями ровно за эти дни:
        # день снимка относится к следующему периоду (остаток спрашивается на утро).
        between = summarize(ops, prev_date, last_date - timedelta(days=1))
        if between.count:
            s.ops_start, s.ops_end, s.ops_balance = between.start, between.end, between.balance

    for n in periods:
        target = shift_months(last_date, -n)
        base = [snap for d, snap in snapshots[:-1] if d <= target]
        if base:
            s.dynamics.append((n, total - sum(base[-1].values(), ZERO)))
    return s


def _on_date(d: date) -> str:
    return f"{d.day} {MONTHS_GENITIVE[d.month - 1]}"


def ops_period(start: date, end: date) -> str:
    """«за сентябрь», если период — ровно календарный месяц, иначе «с 5 октября по 31 октября»."""
    if start == month_start(start) and end == month_end(start):
        return f"за {MONTHS_NOMINATIVE[start.month - 1]}"
    return f"с {_on_date(start)} по {_on_date(end)}"


def safety_months(total: Decimal, ops: Sequence[Operation], today: date) -> Decimal | None:
    """Сколько месяцев обычных трат (без тяжелых) покрывает капитал."""
    u = usual(ops, today)
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
        lines.append(f"Доход − траты {ops_period(s.ops_start, s.ops_end)}: {m(s.ops_balance, True)}")
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


# ---------- итоги месяца (1 числа и /report): сухая статистика ----------

@dataclass
class MonthClose:
    month: date
    full: Summary                  # все траты
    regular: Summary               # только обычные траты (и все доходы)
    heavy: list[BigPurchase]
    trip_ops: list[Operation]
    capital: CapitalSummary | None

    @property
    def heavy_total(self) -> Decimal:
        return sum((b.amount for b in self.heavy), ZERO)

    @property
    def trip_total(self) -> Decimal:
        return sum((op.amount for op in self.trip_ops), ZERO)


def month_close(
    ops: Sequence[Operation],
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
        full=summarize(ops, first, last),
        regular=summarize_clean(ops, first, last),
        heavy=heavy_items(ops, first, last),
        trip_ops=sorted((op for op in ops if is_trip(op) and first <= op.date <= last), key=lambda o: o.date),
        capital=capital,
    )


def _capital_start_line(c: CapitalSummary, cur: str) -> str:
    line = f"💼 Капитал на {_on_date(c.date)} <b>{fmt_k(c.total, cur)}</b>"
    if c.prev_date is not None:
        line += f" (на {_on_date(c.prev_date)} {fmt_k(c.prev_total)}, {fmt_k(c.total_delta, signed=True)})"
    return line


def _shares(items: Sequence[tuple[str, Decimal]], total: Decimal, op_type: OpType, cur: str, icons: Icons | None) -> list[str]:
    return [f"{_label(icons, op_type, n)} — {fmt_money(a, cur)} · {fmt_percent(a / total * 100) if total > 0 else '—'}"
            for n, a in items if a > 0]


def format_month_close(mc: MonthClose, cur: str = "", icons: Icons | None = None) -> str:
    f, r = mc.full, mc.regular
    mark = "🟢" if f.balance >= 0 else "🔴"
    lines = [
        f"📊 <b>{month_title(mc.month).capitalize()}</b>",
        f"💰 Доход <b>{fmt_money(f.income, cur)}</b>",
        f"💸 Потрачено <b>{fmt_money(f.expense, cur)}</b>",
        *_streams(r.expense, mc.heavy_total, mc.trip_total, cur),
        f"{mark} Итог <b>{fmt_money(f.balance, cur, signed=True)}</b> · без тяжелых "
        f"{fmt_money(f.balance + mc.heavy_total, cur, signed=True)}",
    ]
    if mc.capital is not None:
        lines.append(_capital_start_line(mc.capital, cur))
    if r.expense_by_category:
        lines += ["", "<b>Обычные траты</b>", *_shares(r.expense_by_category, r.expense, OpType.EXPENSE, cur, icons)]
    if f.income_by_category:
        lines += ["", "<b>Доходы</b>", *_shares(f.income_by_category, f.income, OpType.INCOME, cur, icons)]
    if f.count == 0:
        lines += ["", "За этот месяц записей нет."]
    return "\n".join(lines)


def format_heavy_list(items: Sequence[BigPurchase], cur: str = "", icons: Icons | None = None) -> list[str]:
    lines = []
    for b in items:
        icon = _icon(icons, OpType.EXPENSE, b.category) if b.category else "❔ "
        lines.append(f"{fmt_short_date(b.date)} {icon}{html.escape(b.what) or 'покупка'} — {fmt_money(b.amount, cur)}")
    return lines


def format_heavy_view(title: str, items: Sequence[BigPurchase], cur: str = "", icons: Icons | None = None) -> str:
    lines = [f"🧾 <b>Тяжелые траты</b> · {title}"]
    if not items:
        return "\n".join(lines + ["Тяжелых трат нет."])
    lines += format_heavy_list(items, cur, icons)
    lines.append(f"Всего: <b>{fmt_money(sum((b.amount for b in items), ZERO), cur)}</b>")
    return "\n".join(lines)


def trip_groups(trip_ops: Sequence[Operation], trips: Sequence[Trip]) -> list[tuple[str, list[Operation]]]:
    """Командировочные операции по поездкам: («Казань 13–16.10», операции)."""
    by_tag: dict[str, list[Operation]] = defaultdict(list)
    for op in trip_ops:
        by_tag[trip_tag(op) or ""].append(op)
    known = {t.tag: t for t in trips}
    out = []
    for tag, items in sorted(by_tag.items(), key=lambda kv: min(o.date for o in kv[1])):
        t = known.get(tag)
        name = f"{html.escape(t.city)} {fmt_trip_range(t.start, t.end)}" if t else "Без поездки"
        out.append((name, items))
    return out


def trip_items_totals(items: Sequence[Operation]) -> list[tuple[str, Decimal]]:
    """Суммы по статьям поездки, по убыванию."""
    parts: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for o in items:
        parts[o.trip_item or "Прочее"] += o.amount
    return _sorted_totals(parts)


def format_trips_view(
    title: str, trip_ops: Sequence[Operation], trips: Sequence[Trip], cur: str = "", icons: Icons | None = None,
) -> str:
    lines = [f"✈️ <b>Командировки</b> · {title}"]
    if not trip_ops:
        return "\n".join(lines + ["Командировок нет."])
    for name, items in trip_groups(trip_ops, trips):
        total = sum((o.amount for o in items), ZERO)
        lines += ["", f"<b>{name}</b> — {fmt_money(total, cur)}"]
        lines += [f"   {TRIP_ITEM_ICONS[n]} {n} {fmt_money(a, cur)}" for n, a in trip_items_totals(items)]
    return "\n".join(lines)


def heavy_year(ops: Sequence[Operation], last: date) -> tuple[int, Decimal, Decimal]:
    """За 12 месяцев до конца `last`: (число тяжелых трат, их сумма, доля в расходах, %)."""
    start = add_months(month_start(last), -11)
    items = heavy_items(ops, start, last)
    total = sum((b.amount for b in items), ZERO)
    expense = summarize(ops, start, last).expense
    return len(items), total, (total / expense * 100 if expense > 0 else ZERO)


# ---------- статистика за несколько месяцев (/stats) ----------

STATS_PERIODS = (3, 6, 12)
QUARTER_START_MONTHS = (1, 4, 7, 10)
MAX_TILES = 12


@dataclass
class Tile:
    """Плитка категории обычных трат: значения по месяцам периода и отметка за последний месяц."""
    name: str
    values: list[Decimal]
    band: tuple[Decimal, Decimal] | None    # min–max «обычно» перед последним месяцем
    usual: Decimal | None                   # медиана перед последним месяцем
    marker: str                             # ▲ / ▼ / ""

    @property
    def last(self) -> Decimal:
        return self.values[-1]


@dataclass
class Stats:
    months: list[date]
    regular: list[Summary]                  # обычные траты (и все доходы) по месяцам
    heavy: list[Decimal]                    # тяжелые по месяцам
    trips: list[Decimal]                    # командировки по месяцам
    capital: list[Decimal | None]           # на конец каждого месяца
    tiles: list[Tile]
    trends: tuple[list[Trend], list[Trend]] | None
    heavy_items: list[BigPurchase]
    trip_ops: list[Operation]

    @property
    def income(self) -> list[Decimal]:
        return [s.income for s in self.regular]

    @property
    def has_data(self) -> bool:
        return any(s.count for s in self.regular)

    @property
    def trips_count(self) -> int:
        return len({trip_tag(op) or "" for op in self.trip_ops})


def stats(ops: Sequence[Operation], balances: Sequence[BalanceEntry], today: date, n: int = 3) -> Stats:
    """n полных месяцев перед текущим."""
    first = add_months(month_start(today), -n)
    months = [add_months(first, i) for i in range(n)]
    last = months[-1]
    end = month_end(last)
    regular = [summarize_clean_month(ops, m) for m in months]
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for s in regular:
        for name, amount in s.expense_by_category:
            totals[name] += amount
    u = usual(ops, last)
    tiles = []
    for name, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:MAX_TILES]:
        values = [dict(s.expense_by_category).get(name, ZERO) for s in regular]
        tiles.append(Tile(name, values, u.band(name), u.by_category.get(name), u.marker(name, values[-1])))
    by_month = lambda pred: [  # noqa: E731
        sum((op.amount for op in ops if pred(op) and m <= op.date <= month_end(m)), ZERO) for m in months
    ]
    return Stats(
        months=months,
        regular=regular,
        heavy=by_month(is_heavy),
        trips=by_month(is_trip),
        capital=[capital_at_month_end(balances, m) for m in months],
        tiles=tiles,
        trends=trends(ops, last),
        heavy_items=heavy_items(ops, first, end),
        trip_ops=sorted((op for op in ops if is_trip(op) and first <= op.date <= end), key=lambda o: o.date),
    )


def stats_title(st: Stats) -> str:
    return period_title(st.months[0], st.months[-1])


def format_stats(st: Stats, cur: str = "", icons: Icons | None = None) -> str:
    lines = [f"📈 <b>{stats_title(st)}</b>"]
    if not st.has_data and not st.heavy_items and not st.trip_ops:
        return "\n".join(lines + ["За этот период записей нет."])
    trend = lambda t: f"{_label(icons, OpType.EXPENSE, t.name)} ({fmt_k(t.before)} → {fmt_k(t.after, cur)})"  # noqa: E731
    if st.trends is None:
        lines.append("Мало данных для тенденций — нужно 6 месяцев истории.")
    else:
        rising, falling = st.trends
        if rising:
            lines.append("• Расходы растут: " + ", ".join(trend(t) for t in rising))
        if falling:
            lines.append("• Расходы снижаются: " + ", ".join(trend(t) for t in falling))
    heavy_sum = sum((b.amount for b in st.heavy_items), ZERO)
    trip_sum = sum((op.amount for op in st.trip_ops), ZERO)
    parts = []
    parts.append(f"🧾 Тяжелые за период: {len(st.heavy_items)} на {fmt_k(heavy_sum, cur)}" if st.heavy_items
                 else "🧾 Тяжелых за период нет")
    parts.append(f"✈️ Командировки: {st.trips_count} на {fmt_k(trip_sum, cur)}" if st.trip_ops
                 else "✈️ Командировок нет")
    lines += ["", " · ".join(parts)]
    return "\n".join(lines)


# ---------- категория (/cat) ----------

def format_category_report(
    name: str,
    ops: Sequence[Operation],
    today: date,
    cur: str = "",
    max_items: int = 20,
    icon: str = "",
    op_type: OpType | None = None,
) -> str:
    first = month_start(today)
    in_cat = [op for op in ops if op.category == name and op.type != OpType.TRANSFER
              and (op_type is None or op.type == op_type)]
    current = sorted((op for op in in_cat if first <= op.date <= today), key=lambda o: o.date, reverse=True)
    total = sum((op.amount for op in current), ZERO)
    head = f"{icon} " if icon else "🏷 "
    lines = [f"{head}<b>{html.escape(name)}</b> — {month_title(first)}", f"Итого: <b>{fmt_money(total, cur)}</b>"]
    for op in current[:max_items]:
        comment = f" {html.escape(op.comment)}" if op.comment else ""
        stream = " 🧾" if is_heavy(op) else (" ✈️" if is_trip(op) else "")
        lines.append(f"{fmt_short_date(op.date)} · {fmt_money(op.amount, cur)}{comment}{stream}")
    if len(current) > max_items:
        lines.append(f"… и еще {len(current) - max_items}")
    u = usual(ops, first)
    if u.months:
        lines += ["", "<b>Прошлые месяцы</b> (обычные траты):"]
        for m, v in zip(u.months, u.history.get(name, [ZERO] * len(u.months))):
            lines.append(f"{MONTHS_SHORT[m.month - 1]} {m.year}: {fmt_money(v, cur)}")
        lines.append(f"Обычно (медиана): <b>{fmt_money(u.by_category.get(name, ZERO), cur)}</b> в месяц")
    return "\n".join(lines)


# ---------- тяжелые траты за год (/big) ----------

def format_big(ops: Sequence[Operation], today: date, cur: str = "", icons: Icons | None = None, limit: int = 30) -> str:
    start = add_months(month_start(today), -11)
    items = sorted(heavy_items(ops, start, today), key=lambda b: b.date, reverse=True)
    n, total, share = heavy_year(ops, today)
    head = f"🧾 <b>Тяжелые траты</b> с {MONTHS_SHORT[start.month - 1]} {start.year}"
    if not items:
        return f"{head}\nПока нет. Пометьте трату тегом: <code>283000 отпуск #тяжелое</code>"
    lines = [f"{head}: {n} на <b>{fmt_k(total, cur)}</b> · {fmt_percent(share)} всех расходов", ""]
    lines += format_heavy_list(items[:limit], cur, icons)
    if len(items) > limit:
        lines.append(f"… и еще {len(items) - limit}")
    return "\n".join(lines)


# ---------- мягкие сигналы ----------

SIGNAL_CATEGORY_FROM_DAY = 20
OTHER_SHARE = Decimal("0.10")
OTHER_FROM_DAY = 10


def find_signals(
    ops: Sequence[Operation],
    balances: Sequence[BalanceEntry],
    today: date,
    cur: str = "",
    icons: Icons | None = None,
) -> list[tuple[str, str]]:
    """Список (ключ, текст). Ключ нужен, чтобы один и тот же сигнал не повторялся. Всё без тяжелых трат."""
    out: list[tuple[str, str]] = []
    first = month_start(today)
    key_month = first.strftime("%Y-%m")
    s = summarize_clean(ops, first, today)

    if today.day >= SIGNAL_CATEGORY_FROM_DAY:
        avgs = usual(ops, today).by_category
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
    if c is not None and c.unaccounted is not None and c.ops_start is not None:
        income = summarize(ops, c.ops_start, c.ops_end).income
        if income > 0 and abs(c.unaccounted) > income * RECONCILE_WARN_SHARE:
            out.append((
                f"reconcile:{c.date.isoformat()}",
                f"💼 Сверка {ops_period(c.ops_start, c.ops_end)} не сходится на "
                f"{fmt_money(c.unaccounted, cur, signed=True)} — похоже, часть трат или доходов не записана.",
            ))
    return out
