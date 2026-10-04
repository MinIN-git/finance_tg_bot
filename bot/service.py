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
from .storage import Storage
from .textutil import fmt_money, fmt_short_date, normalize

PENDING_LIMIT = 50
PLAIN_AMOUNT_RE = re.compile(r"^(\d+(?:[.,]\d{1,2})?)(к|k)?(?:р|руб|₽|€|\$|eur|евро)?\.?$")


@dataclass
class Prepared:
    op: Operation
    needs_category: bool


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
    ) -> None:
        self.storage = storage
        self.tz = tz or ZoneInfo("Europe/Moscow")
        self.cur = currency
        self._today_fn = today_fn
        self._pending: OrderedDict[str, Operation] = OrderedDict()
        self._snapshots: dict[int, SnapshotSession] = {}

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
        categories = await self.storage.list_categories()
        op.category = guess_category(op.comment, op.type, categories) or ""
        return Prepared(op, needs_category=not op.category)

    async def save(self, op: Operation) -> Operation:
        await self.storage.add_operation(op)
        return op

    def stash(self, op: Operation) -> str:
        self._pending[op.id] = op
        while len(self._pending) > PENDING_LIMIT:
            self._pending.popitem(last=False)
        return op.id

    def pop_pending(self, pending_id: str) -> Operation | None:
        return self._pending.pop(pending_id, None)

    async def categories_for(self, op_type: OpType) -> list[Category]:
        return [c for c in await self.storage.list_categories() if c.type == op_type]

    async def learn(self, op: Operation) -> str | None:
        """Запоминает слово из комментария как ключевое для категории операции."""
        if not op.category or op.type == OpType.TRANSFER:
            return None
        kw = learnable_keyword(op.comment)
        if kw is None:
            return None
        categories = await self.storage.list_categories()
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
        ops = await self.storage.list_operations()
        return list(reversed(ops[-n:]))

    def describe(self, op: Operation) -> str:
        if op.type == OpType.TRANSFER:
            head = f"🔁 Перевод {fmt_money(op.amount, self.cur)}: {html.escape(op.account)} → {html.escape(op.to_account)}"
        elif op.type == OpType.INCOME:
            head = f"🟢 Доход {fmt_money(op.amount, self.cur)} · {html.escape(op.category or '?')}"
        else:
            head = f"🔴 Расход {fmt_money(op.amount, self.cur)} · {html.escape(op.category or '?')}"
        parts = [head, fmt_short_date(op.date)]
        if op.comment:
            parts.append(html.escape(op.comment))
        if op.type != OpType.TRANSFER and op.account:
            parts.append(f"@{html.escape(op.account)}")
        if op.tags:
            parts.append(" ".join(f"#{html.escape(t)}" for t in op.tags))
        return " · ".join(parts)

    def describe_short(self, op: Operation) -> str:
        sign = {OpType.INCOME: "+", OpType.EXPENSE: "−", OpType.TRANSFER: "⇄"}[op.type]
        what = op.category if op.type != OpType.TRANSFER else f"{op.account}→{op.to_account}"
        comment = f" {op.comment}" if op.comment else ""
        return html.escape(f"{fmt_short_date(op.date)} {sign}{fmt_money(op.amount, self.cur)} {what}{comment}")

    # ---------- отчеты ----------

    async def month_text(self) -> str:
        today = self.today()
        ops = await self.storage.list_operations()
        s = reports.summarize(ops, reports.month_start(today), today)
        return reports.format_month(s, today, self.cur)

    async def week_text(self) -> str:
        today = self.today()
        ops = await self.storage.list_operations()
        s = reports.summarize(ops, reports.week_start(today), today)
        return reports.format_week(s, self.cur)

    async def category_text(self, query: str) -> str:
        cats = find_category(query, await self.storage.list_categories())
        names = list(dict.fromkeys(c.name for c in cats))
        if not names:
            return f"Категория «{html.escape(query)}» не найдена. Список: /categories"
        if len(names) > 1:
            return "Уточните категорию: " + ", ".join(html.escape(n) for n in names)
        ops = await self.storage.list_operations()
        return reports.format_category_report(names[0], ops, self.today(), self.cur)

    async def monthly_report_text(self, month: date | None = None) -> str:
        month = month or reports.add_months(self.today(), -1)
        ops = await self.storage.list_operations()
        return reports.format_monthly_report(reports.monthly_report(ops, month), self.cur)

    async def categories_text(self) -> str:
        cats = await self.storage.list_categories()
        lines = []
        for t, title in ((OpType.EXPENSE, "Расходы"), (OpType.INCOME, "Доходы")):
            lines.append(f"<b>{title}:</b>")
            for c in cats:
                if c.type == t:
                    kws = ", ".join(c.keywords[:8]) + ("…" if len(c.keywords) > 8 else "")
                    lines.append(f"· {html.escape(c.name)}" + (f" — <i>{html.escape(kws)}</i>" if kws else ""))
            lines.append("")
        lines.append("Ключевые слова редактируются на листе «Категории», затем /refresh.")
        return "\n".join(lines)

    # ---------- ежемесячный снимок остатков ----------

    async def start_snapshot(self, user_id: int) -> SnapshotQuestion | None:
        """Начинает опрос «сколько на каждом счете». None — в листе «Счета» нет счетов."""
        accounts = await self.storage.list_accounts()
        if not accounts:
            return None
        today = self.today()
        previous: dict[str, BalanceEntry] = {}
        for e in sorted(await self.storage.list_balances(), key=lambda e: e.date):
            if e.date < today:
                previous[e.account] = e
        session = SnapshotSession(today, accounts, previous)
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
        return True, nxt

    async def capital_text(self) -> str:
        accounts = [a.name for a in await self.storage.list_accounts()]
        summary = reports.capital_summary(
            await self.storage.list_balances(), await self.storage.list_operations(), accounts
        )
        return reports.format_capital(summary, self.cur)


def parse_plain_amount(text: str) -> Decimal | None:
    """Сумма в ответе на вопрос снимка «Сколько на карте?»: «512000», «512 000», «512000,50», «512к»."""
    t = normalize(text).replace("\u00a0", "").replace(" ", "")
    m = PLAIN_AMOUNT_RE.match(t)
    if not m:
        return None
    value = Decimal(m.group(1).replace(",", "."))
    return value * 1000 if m.group(2) else value
