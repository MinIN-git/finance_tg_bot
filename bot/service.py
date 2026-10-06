"""Бизнес-логика бота, не зависящая от Telegram."""
from __future__ import annotations

import html
import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from . import reports
from .categories import find_category, guess_category, learnable_keyword
from .models import Account, BalanceEntry, Category, Operation, OpType
from .parser import parse_message
from .state import State
from .storage import Storage
from .textutil import fmt_money, fmt_short_date, normalize

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
            tags=parsed.tags,
        )
        if op.type == OpType.TRANSFER:
            return Prepared(op, needs_category=False)
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
        return op

    async def undo(self) -> Operation | None:
        ops = await self.storage.list_operations()
        if not ops:
            return None
        return await self.storage.delete_operation(ops[-1].id)

    async def delete(self, op_id: str) -> Operation | None:
        return await self.storage.delete_operation(op_id)

    async def last(self, n: int = 10) -> list[Operation]:
        await self._categories()
        ops = await self.storage.list_operations()
        return list(reversed(ops[-n:]))

    def describe(self, op: Operation) -> str:
        if op.type == OpType.TRANSFER:
            head = f"🔁 Перевод {fmt_money(op.amount, self.cur)}: {html.escape(op.account)} → {html.escape(op.to_account)}"
        elif op.type == OpType.INCOME:
            head = f"🟢 Доход {fmt_money(op.amount, self.cur)} · {html.escape(self.cat_label(op.type, op.category) or '?')}"
        else:
            head = f"🔴 Расход {fmt_money(op.amount, self.cur)} · {html.escape(self.cat_label(op.type, op.category) or '?')}"
        parts = [head, fmt_short_date(op.date)]
        if op.comment:
            parts.append(html.escape(op.comment))
        if op.type != OpType.TRANSFER and op.account:
            parts.append(f"@{html.escape(op.account)}")
        tags = [t for t in op.tags if t not in reports.HIDDEN_TAGS]
        if tags:
            parts.append(" ".join(f"#{html.escape(t)}" for t in tags))
        return " · ".join(parts)

    def describe_short(self, op: Operation) -> str:
        sign = {OpType.INCOME: "+", OpType.EXPENSE: "−", OpType.TRANSFER: "⇄"}[op.type]
        what = self.cat_label(op.type, op.category) if op.type != OpType.TRANSFER else f"{op.account}→{op.to_account}"
        comment = f" {op.comment}" if op.comment else ""
        return html.escape(f"{fmt_short_date(op.date)} {sign}{fmt_money(op.amount, self.cur)} {what}{comment}")

    # ---------- отчеты ----------

    async def month_text(self) -> str:
        today = self.today()
        ops = await self.storage.list_operations()
        s = reports.summarize(ops, reports.month_start(today), today)
        await self._categories()
        spent, expected = reports.month_pace(ops, await self.storage.list_big_purchases(), today)
        pace = reports.format_pace(spent, expected, today, self.cur)
        return reports.format_month(s, today, self.cur, self._icons, pace=pace)

    async def week_text(self) -> str:
        today = self.today()
        ops = await self.storage.list_operations()
        s = reports.summarize(ops, reports.week_start(today), today)
        await self._categories()
        return reports.format_week(s, self.cur, self._icons)

    async def category_text(self, query: str) -> str:
        cats = find_category(query, await self._categories())
        names = list(dict.fromkeys(c.name for c in cats))
        if not names:
            return f"Категория «{html.escape(query)}» не найдена. Список: /categories"
        if len(names) > 1:
            return "Уточните категорию: " + ", ".join(html.escape(n) for n in names)
        ops = await self.storage.list_operations()
        icon = next((c.emoji for c in cats if c.name == names[0] and c.emoji), "")
        return reports.format_category_report(names[0], ops, self.today(), self.cur, icon=icon)

    async def month_close_text(self, month: date | None = None) -> str:
        """Итоги месяца (по умолчанию — прошлого): операции, разовые траты, капитал и сверка."""
        month = reports.month_start(month or reports.add_months(self.today(), -1))
        ops = await self.storage.list_operations()
        accounts = [a.name for a in await self.storage.list_accounts()]
        mc = reports.month_close(
            ops, await self.storage.list_big_purchases(), await self.storage.list_balances(), month, accounts,
        )
        await self._categories()
        return reports.format_month_close(mc, self.cur, self._icons)

    monthly_report_text = month_close_text

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
            safety = reports.safety_months(summary.total, ops, await self.storage.list_big_purchases(), self.today())
        return reports.format_capital(summary, self.cur, reports.snapshot_totals(balances), safety)


def parse_plain_amount(text: str) -> Decimal | None:
    """Сумма в ответе на вопрос снимка «Сколько на карте?»: «512000», «512 000», «512000,50», «512к»."""
    t = normalize(text).replace("\u00a0", "").replace(" ", "")
    m = PLAIN_AMOUNT_RE.match(t)
    if not m:
        return None
    value = Decimal(m.group(1).replace(",", "."))
    return value * 1000 if m.group(2) else value
