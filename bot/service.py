"""Бизнес-логика бота, не зависящая от Telegram."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from . import reports
from .categories import find_category, guess_category, learnable_keyword
from .models import (
    HEAVY_TAG, HEAVY_TAG_ALIASES, TRIP_ITEM_ICONS, TRIP_ITEMS, TRIP_TAG_PREFIX, Account, Kind, guess_trip_item, BalanceEntry, BigPurchase, Category, Operation, OpType, Trip, is_heavy,
    trip_tag,
)
from . import trips as trips_mod
from .parser import parse_message
from .state import State
from .storage import Storage
from .textutil import fmt_money, fmt_short_date, normalize

log = logging.getLogger(__name__)

PENDING_LIMIT = 50
PLAIN_AMOUNT_RE = re.compile(r"^(\d+(?:[.,]\d{1,2})?)(к|k)?(?:р|руб|₽|€|\$|eur|евро)?\.?$")


@dataclass
class Prepared:
    op: Operation
    needs_category: bool
    # Знака нет, а слово подходит и к доходной, и к расходной категории: спросить тип.
    type_choices: dict[OpType, str] | None = None


@dataclass
class SnapshotQuestion:
    account: Account
    position: int                    # 0-based номер вопроса
    total: int
    previous: BalanceEntry | None    # последний известный остаток по счету


@dataclass
class SnapshotSession:
    date: date
    accounts: list[Account]
    previous: dict[str, BalanceEntry]
    position: int = 0
    purpose: str = "manual"           # "monthly" — опрос 1 числа, после него приходят итоги месяца

    def question(self) -> SnapshotQuestion | None:
        if self.position >= len(self.accounts):
            return None
        acc = self.accounts[self.position]
        return SnapshotQuestion(acc, self.position, len(self.accounts), self.previous.get(acc.name))


class FinanceService:
    def __init__(
        self,
        storage: Storage,
        tz: ZoneInfo | None = None,
        currency: str = "",
        today_fn: Callable[[], date] | None = None,
        state: State | None = None,
    ) -> None:
        self.storage = storage
        self.state = state or State()
        self.tz = tz or ZoneInfo("Europe/Moscow")
        self.cur = currency
        self._today_fn = today_fn
        self._pending: OrderedDict[str, Operation] = OrderedDict()
        self._pending_choices: dict[str, dict[OpType, str]] = {}
        self._snapshots: dict[int, SnapshotSession] = {}
        self._finished_purpose: dict[int, str] = {}
        self._icons: dict[tuple[OpType, str], str] = {}
        self._trip_names: dict[str, str] = {}   # тег поездки -> город, для describe()

    async def _categories(self) -> list[Category]:
        """Категории из хранилища; заодно обновляет кэш эмодзи для describe()."""
        cats = await self.storage.list_categories()
        self._icons = {(c.type, c.name): c.emoji for c in cats if c.emoji}
        return cats

    def cat_label(self, op_type: OpType, name: str) -> str:
        emoji = self._icons.get((op_type, name))
        return f"{emoji} {name}" if emoji else name

    def today(self) -> date:
        return self._today_fn() if self._today_fn else datetime.now(self.tz).date()

    # ---------- ввод операций ----------

    async def prepare(self, text: str) -> Prepared:
        """Разбирает сообщение и пытается угадать категорию. Бросает ParseError."""
        accounts = [a.name for a in await self.storage.list_accounts()]
        parsed = parse_message(text, self.today(), accounts)
        op = Operation(
            date=parsed.date,
            type=parsed.type,
            amount=parsed.amount,
            account=parsed.account or (accounts[0] if accounts else ""),
            to_account=parsed.to_account or "",
            comment=parsed.comment,
            tags=list(dict.fromkeys(HEAVY_TAG if t in HEAVY_TAG_ALIASES else t for t in parsed.tags)),
        )
        if op.type == OpType.TRANSFER:
            return Prepared(op, needs_category=False)
        trip = trips_mod.covering(await self._trips(), op.date)
        if trip is not None and op.type == OpType.EXPENSE and trip.tag not in op.tags and not is_heavy(op):
            op.tags.append(trip.tag)          # трата в даты поездки — сразу командировочная
            op.kind = Kind.TRIP
            op.trip_item = guess_trip_item(op.comment, op.category)
        categories = await self._categories()
        if parsed.explicit_sign:
            op.category = guess_category(op.comment, op.type, categories) or ""
            return Prepared(op, needs_category=not op.category)
        # Без знака: по умолчанию расход, но если слово знакомо только доходной категории — доход.
        expense = guess_category(op.comment, OpType.EXPENSE, categories)
        income = guess_category(op.comment, OpType.INCOME, categories)
        if expense and income:
            return Prepared(op, needs_category=False, type_choices={OpType.INCOME: income, OpType.EXPENSE: expense})
        if income:
            op.type, op.category = OpType.INCOME, income
        else:
            op.category = expense or ""
        return Prepared(op, needs_category=not op.category)

    async def save(self, op: Operation) -> Operation:
        await self.storage.add_operation(op)
        if is_heavy(op):
            try:
                await self.storage.add_big_purchase(
                    BigPurchase(op.date, op.comment or op.category, op.amount, op.category, op.id)
                )
            except Exception:
                log.exception("Не удалось дописать тяжелую трату в «Большие покупки»")
        return op

    def stash(self, op: Operation, type_choices: dict[OpType, str] | None = None) -> str:
        self._pending[op.id] = op
        if type_choices:
            self._pending_choices[op.id] = type_choices
        while len(self._pending) > PENDING_LIMIT:
            old, _ = self._pending.popitem(last=False)
            self._pending_choices.pop(old, None)
        return op.id

    def pop_pending(self, pending_id: str) -> Operation | None:
        self._pending_choices.pop(pending_id, None)
        return self._pending.pop(pending_id, None)

    def pending_choices(self, pending_id: str) -> dict[OpType, str] | None:
        return self._pending_choices.get(pending_id)

    def get_pending(self, pending_id: str) -> Operation | None:
        return self._pending.get(pending_id)

    async def categories_for(self, op_type: OpType) -> list[Category]:
        return [c for c in await self._categories() if c.type == op_type]

    async def learn(self, op: Operation) -> str | None:
        """Запоминает слово из комментария как ключевое для категории операции."""
        if not op.category or op.type == OpType.TRANSFER:
            return None
        kw = learnable_keyword(op.comment)
        if kw is None:
            return None
        categories = await self._categories()
        if guess_category(op.comment, op.type, categories) == op.category:
            return None
        await self.storage.add_keyword(op.category, op.type, kw)
        return kw

    async def get_operation(self, op_id: str) -> Operation | None:
        for op in await self.storage.list_operations():
            if op.id == op_id:
                return op
        return None

    async def set_category(self, op_id: str, category: str) -> Operation | None:
        current = await self.get_operation(op_id)
        if current is None:
            return None
        op = replace(current, category=category, tags=list(current.tags))
        if not await self.storage.update_operation(op):
            return None
        if is_heavy(op):
            await self.storage.set_big_purchase_category(op.id, category)
        return op

    async def undo(self) -> Operation | None:
        ops = await self.storage.list_operations()
        if not ops:
            return None
        return await self.delete(ops[-1].id)

    async def delete(self, op_id: str) -> Operation | None:
        op = await self.storage.delete_operation(op_id)
        if op is not None and is_heavy(op):
            await self.storage.delete_big_purchase(op.id)
        return op

    async def last(self, n: int = 10) -> list[Operation]:
        await self._categories()
        ops = await self.storage.list_operations()
        return list(reversed(ops[-n:]))

    def describe(self, op: Operation) -> str:
        if op.type == OpType.TRANSFER:
            head = f"🔁 Перевод {fmt_money(op.amount, self.cur, exact=True)}: {html.escape(op.account)} → {html.escape(op.to_account)}"
        elif op.type == OpType.INCOME:
            head = f"🟢 Доход {fmt_money(op.amount, self.cur, exact=True)} · {html.escape(self.cat_label(op.type, op.category) or '?')}"
        else:
            head = f"🔴 Расход {fmt_money(op.amount, self.cur, exact=True)} · {html.escape(self.cat_label(op.type, op.category) or '?')}"
        parts = [head, fmt_short_date(op.date)]
        if op.comment:
            parts.append(html.escape(op.comment))
        if op.type != OpType.TRANSFER and op.account:
            parts.append(f"@{html.escape(op.account)}")
        if is_heavy(op):
            parts.append("🧾 тяжелая трата")
        if trip_tag(op):
            city = self._trip_names.get(trip_tag(op))
            parts.append(f"✈️ {html.escape(city)}" if city else "✈️ командировка")
            if op.trip_item:
                parts.append(f"{TRIP_ITEM_ICONS[op.trip_item]} {op.trip_item}")
        tags = [t for t in op.tags if t not in reports.HIDDEN_TAGS]
        if tags:
            parts.append(" ".join(f"#{html.escape(t)}" for t in tags))
        return " · ".join(parts)

    def describe_short(self, op: Operation) -> str:
        sign = {OpType.INCOME: "+", OpType.EXPENSE: "−", OpType.TRANSFER: "⇄"}[op.type]
        what = self.cat_label(op.type, op.category) if op.type != OpType.TRANSFER else f"{op.account}→{op.to_account}"
        comment = f" {op.comment}" if op.comment else ""
        return html.escape(f"{fmt_short_date(op.date)} {sign}{fmt_money(op.amount, self.cur, exact=True)} {what}{comment}")

    # ---------- отчеты ----------

    async def _data(self) -> list[Operation]:
        """Операции (и заодно иконки категорий) — то, что нужно почти каждому отчету."""
        await self._categories()
        return await self.storage.list_operations()

    def _now_text(self, title: str, ops: list[Operation], start: date, end: date) -> str:
        full = reports.summarize(ops, start, end)
        regular = reports.summarize_clean(ops, start, end)
        heavy = sum((b.amount for b in reports.heavy_items(ops, start, end)), Decimal(0))
        trips = reports.trip_spending(ops, start, end)
        return reports.format_now(title, full, regular, heavy, trips, self.cur, self._icons)

    async def month_text(self, view: str = "main") -> str:
        """Текущий месяц: "main" — сводка, "all" — все категории, "last" — последние траты."""
        today = self.today()
        ops = await self._data()
        first = reports.month_start(today)
        if view == "all":
            s = reports.summarize(ops, first, today)
            return reports.format_all_categories(f"📋 {reports.month_now_title(today)}: все категории", s, self.cur, self._icons)
        if view == "last":
            await self._trips()
            recent = [op for op in ops if first <= op.date <= today and op.type == OpType.EXPENSE][-15:]
            lines = [f"🕑 {reports.month_now_title(today)}: последние траты"]
            lines += [self.describe_short(op) for op in reversed(recent)] or ["Трат пока нет."]
            return "\n".join(lines)
        return self._now_text(reports.month_now_title(today), ops, first, today)

    async def week_text(self) -> str:
        today = self.today()
        start = reports.week_start(today)
        return self._now_text(reports.week_title(start, today), await self._data(), start, today)

    async def category_text(self, query: str) -> str:
        cats = find_category(query, await self._categories())
        names = list(dict.fromkeys(c.name for c in cats))
        if not names:
            return f"Категория «{html.escape(query)}» не найдена. Список: /categories"
        if len(names) > 1:
            return "Уточните категорию: " + ", ".join(html.escape(n) for n in names)
        ops = await self._data()
        icon = next((c.emoji for c in cats if c.name == names[0] and c.emoji), "")
        return reports.format_category_report(names[0], ops, self.today(), self.cur, icon=icon)

    async def month_close(self, month: date | None = None) -> reports.MonthClose:
        month = reports.month_start(month or reports.add_months(self.today(), -1))
        ops = await self._data()
        accounts = [a.name for a in await self.storage.list_accounts()]
        return reports.month_close(ops, await self.storage.list_balances(), month, accounts)

    async def month_close_view(self, month: date | None = None, view: str = "main") -> tuple[str, bool, bool]:
        """Итоги месяца (по умолчанию прошлого): "main" — сухая статистика, "heavy" — тяжелые,
        "trips" — командировки. Возвращает (текст, есть тяжелые, есть командировки) — для кнопок."""
        mc = await self.month_close(month)
        title = reports.month_title(mc.month)
        if view == "heavy":
            text = reports.format_heavy_view(title, mc.heavy, self.cur, self._icons)
        elif view == "trips":
            text = reports.format_trips_view(title, mc.trip_ops, await self._trips(), self.cur, self._icons)
        else:
            text = reports.format_month_close(mc, self.cur, self._icons)
        return text, bool(mc.heavy), bool(mc.trip_ops)

    async def month_close_text(self, month: date | None = None, view: str = "main") -> str:
        return (await self.month_close_view(month, view))[0]

    monthly_report_text = month_close_text

    async def stats(self, n: int = 3) -> reports.Stats:
        ops = await self._data()
        return reports.stats(ops, await self.storage.list_balances(), self.today(), n)

    async def stats_view(self, n: int = 3, view: str = "main", chart: bool = False) -> tuple[str, bytes | None, bool, bool]:
        """/stats: (текст, картинка — если chart и есть данные, есть тяжелые, есть командировки)."""
        st = await self.stats(n)
        title = reports.stats_title(st)
        if view == "heavy":
            text = reports.format_heavy_view(title, st.heavy_items, self.cur, self._icons)
        elif view == "trips":
            text = reports.format_trips_view(title, st.trip_ops, await self._trips(), self.cur, self._icons)
        else:
            text = reports.format_stats(st, self.cur, self._icons)
        png = None
        if chart and st.has_data:
            try:
                from .charts import stats_chart

                png = await asyncio.to_thread(stats_chart, st, f"{title} · обычные траты по категориям")
            except Exception:
                log.exception("Не удалось нарисовать графики")
        return text, png, bool(st.heavy_items), bool(st.trip_ops)

    async def stats_report(self, n: int = 3) -> tuple[str, bytes | None]:
        text, png, _, _ = await self.stats_view(n, chart=True)
        return text, png

    async def signals(self) -> list[tuple[str, str]]:
        ops = await self._data()
        return reports.find_signals(ops, await self.storage.list_balances(), self.today(), self.cur, self._icons)

    async def compare_text(self, args: str | None) -> str:
        from .import_budget import parse_month_label

        today = self.today()
        if not args:
            m2 = reports.add_months(today, -1)
            m1 = reports.add_months(today, -2)
        else:
            labels: list[str] = []
            for t in args.split():   # «сентябрь 2025» — год приклеивается к месяцу
                if labels and t.isdigit() and len(t) == 4:
                    labels[-1] += f" {t}"
                else:
                    labels.append(t)
            months = [parse_month_label(lbl, today) for lbl in labels]
            if len(months) != 2 or None in months:
                return ("Укажите два месяца: <code>/compare август сентябрь</code> "
                        "или <code>/compare 08.2026 09.2026</code>")
            m1, m2 = months
        ops = await self._data()
        return reports.format_compare(ops, m1, m2, self.cur, self._icons)

    async def big_text(self) -> str:
        ops = await self._data()
        return reports.format_big(ops, self.today(), self.cur, self._icons)

    async def categories_text(self) -> str:
        cats = await self._categories()
        lines = []
        for t, title in ((OpType.EXPENSE, "Расходы"), (OpType.INCOME, "Доходы")):
            lines.append(f"<b>{title}:</b>")
            for c in cats:
                if c.type == t:
                    kws = ", ".join(c.keywords[:8]) + ("…" if len(c.keywords) > 8 else "")
                    lines.append(f"{c.emoji or '·'} {html.escape(c.name)}" + (f" — <i>{html.escape(kws)}</i>" if kws else ""))
            lines.append("")
        lines.append("Ключевые слова редактируются на листе «Категории», затем /refresh.")
        return "\n".join(lines)

    # ---------- командировки ----------

    async def _trips(self) -> list[Trip]:
        trips = await self.storage.list_trips()
        self._trip_names = {t.tag: t.city for t in trips}
        return trips

    async def trip_for_button(self, op: Operation) -> tuple[Trip | None, bool]:
        """Поездка для кнопки под записанной тратой: (поездка, уже помечена)."""
        if op.type != OpType.EXPENSE:
            return None, False
        trips = await self._trips()
        tag = trip_tag(op)
        if tag:
            return next((t for t in trips if t.tag == tag), None), True
        return trips_mod.nearest_active(trips, self.today()), False

    async def toggle_trip(self, op_id: str, on: bool) -> Operation | None:
        """Пометить трату ближайшей поездкой (on) или снять пометку поездки."""
        current = await self.get_operation(op_id)
        if current is None:
            return None
        tags = [t for t in current.tags if not t.startswith(TRIP_TAG_PREFIX)]
        if on:
            trip = trips_mod.nearest_active(await self._trips(), self.today())
            if trip is None:
                return None
            tags.append(trip.tag)
        kind = Kind.HEAVY if is_heavy(current) else (Kind.TRIP if on else Kind.REGULAR)
        op = replace(current, tags=tags, kind=kind)
        return op if await self.storage.update_operation(op) else None

    async def set_trip_item(self, op_id: str, item: str) -> Operation | None:
        current = await self.get_operation(op_id)
        if current is None or current.kind != Kind.TRIP or item not in TRIP_ITEMS:
            return None
        op = replace(current, trip_item=item, tags=list(current.tags))
        return op if await self.storage.update_operation(op) else None

    async def trip_command(self, args: str | None) -> str:
        today = self.today()
        trips = await self._trips()
        ops = await self.storage.list_operations()
        arg = (args or "").strip()
        if not arg:
            return trips_mod.format_trip_status(trips_mod.nearest_active(trips, today), ops, today, self.cur)
        if arg.lower() in ("cancel", "отмена", "end", "стоп"):
            trip = trips_mod.nearest_active(trips, today)
            if trip is None:
                return "Отменять нечего — впереди поездок нет."
            if arg.lower() in ("end", "стоп"):
                return (f"Поездка в {html.escape(trip.city)} закончится сама {trip.end:%d.%m}. "
                        "Чтобы удалить ее совсем: /trip cancel")
            for op in trips_mod.trip_ops(trip, ops):
                await self.storage.update_operation(replace(
                    op, tags=[t for t in op.tags if t != trip.tag],
                    kind=Kind.HEAVY if is_heavy(op) else Kind.REGULAR,
                ))
            await self.storage.delete_trip(trip.id)
            return f"✖️ Поездка {html.escape(trip.city)} {trips_mod.fmt_range(trip)} отменена, пометки с трат сняты."
        try:
            city, start, end = trips_mod.parse_trip_args(arg, today)
        except trips_mod.TripError as e:
            return str(e)
        trip = Trip(city, start, end)
        if any(t.id == trip.id for t in trips):
            return f"Такая поездка уже есть: {html.escape(city)} {trips_mod.fmt_range(trip)}"
        await self.storage.add_trip(trip)
        tagged = 0
        for op in ops:
            if op.type == OpType.EXPENSE and trip.covers(op.date) and not trip_tag(op) and not is_heavy(op):
                await self.storage.update_operation(replace(op, tags=[*op.tags, trip.tag], kind=Kind.TRIP))
                tagged += 1
        await self._trips()
        text = f"✈️ Поездка: <b>{html.escape(city)}</b>, {trips_mod.fmt_range(trip)} · {trip.days} дн."
        if tagged:
            text += f"\nУже записанные траты в эти даты помечены: {tagged}."
        text += f"\nБилеты и прочее, купленное заранее, отмечайте кнопкой «✈️ В {html.escape(city)}» под тратой."
        return text

    async def trips_text(self) -> str:
        return trips_mod.format_trips(await self._trips(), await self.storage.list_operations(), self.today(), self.cur)

    # ---------- ежемесячный снимок остатков ----------

    async def start_snapshot(self, user_id: int, purpose: str = "manual") -> SnapshotQuestion | None:
        """Начинает опрос «сколько на каждом счете». None — в листе «Счета» нет счетов."""
        accounts = await self.storage.list_accounts()
        if not accounts:
            return None
        today = self.today()
        previous: dict[str, BalanceEntry] = {}
        for e in sorted(await self.storage.list_balances(), key=lambda e: e.date):
            if e.date < today:
                previous[e.account] = e
        session = SnapshotSession(today, accounts, previous, purpose=purpose)
        self._snapshots[user_id] = session
        return session.question()

    def snapshot_question(self, user_id: int) -> SnapshotQuestion | None:
        session = self._snapshots.get(user_id)
        return session.question() if session else None

    def cancel_snapshot(self, user_id: int) -> bool:
        return self._snapshots.pop(user_id, None) is not None

    async def answer_snapshot(self, user_id: int, amount: Decimal | None) -> tuple[bool, SnapshotQuestion | None]:
        """Записывает ответ на текущий вопрос. amount=None — «пропустить», т.е. взять прошлое значение.

        Возвращает (опрос был активен, следующий вопрос или None, если опрос завершен).
        """
        session = self._snapshots.get(user_id)
        q = session.question() if session else None
        if q is None:
            self._snapshots.pop(user_id, None)
            return False, None
        if amount is None and q.previous is not None:
            amount = q.previous.amount
        if amount is not None:
            await self.storage.add_balance(BalanceEntry(session.date, q.account.name, amount))
        session.position += 1
        nxt = session.question()
        if nxt is None:
            self._snapshots.pop(user_id, None)
            self._finished_purpose[user_id] = session.purpose
        return True, nxt

    def pop_finished_purpose(self, user_id: int) -> str:
        """Для чего был только что завершенный опрос: "monthly" или "manual"."""
        return self._finished_purpose.pop(user_id, "manual")

    async def capital_text(self) -> str:
        accounts = [a.name for a in await self.storage.list_accounts()]
        balances = await self.storage.list_balances()
        ops = await self.storage.list_operations()
        summary = reports.capital_summary(balances, ops, accounts)
        safety = None
        if summary is not None:
            safety = reports.safety_months(summary.total, ops, self.today())
        return reports.format_capital(summary, self.cur, reports.snapshot_totals(balances), safety)


def parse_plain_amount(text: str) -> Decimal | None:
    """Сумма в ответе на вопрос снимка «Сколько на карте?»: «512000», «512 000», «512000,50», «512к»."""
    t = normalize(text).replace("\u00a0", "").replace(" ", "")
    m = PLAIN_AMOUNT_RE.match(t)
    if not m:
        return None
    value = Decimal(m.group(1).replace(",", "."))
    return value * 1000 if m.group(2) else value
