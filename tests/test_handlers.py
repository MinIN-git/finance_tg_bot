"""Обработчики aiogram с поддельной сессией Telegram (без сети и токена)."""
import asyncio
import copy
import datetime
import itertools

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.categories import DEFAULT_CATEGORIES
from bot.handlers import WhitelistMiddleware, router
from bot.models import OpType
from bot.service import FinanceService
from bot.storage import MemoryStorage

ME = User(id=1, is_bot=False, first_name="I")
_ids = itertools.count(100)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return Message(message_id=next(_ids), date=datetime.datetime.now(), chat=Chat(id=1, type="private"), text="x")

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""


class Harness:
    def __init__(self, storage):
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.svc = FinanceService(storage, today_fn=lambda: datetime.date(2026, 10, 4))
        self.dp = Dispatcher(svc=self.svc)
        self.dp.update.outer_middleware(WhitelistMiddleware(frozenset({1})))
        router._parent_router = None  # модульный router можно подключить к новому Dispatcher в каждом тесте
        self.dp.include_router(router)

    def _feed(self, update):
        n = len(self.session.sent)
        asyncio.run(self.dp.feed_update(self.bot, update))
        return self.session.sent[n:]

    def text(self, text):
        msg = Message(message_id=next(_ids), date=datetime.datetime.now(), chat=Chat(id=1, type="private"), from_user=ME, text=text)
        return self._feed(Update(update_id=next(_ids), message=msg))

    def click(self, data):
        msg = Message(message_id=5, date=datetime.datetime.now(), chat=Chat(id=1, type="private"), from_user=ME, text="x")
        cq = CallbackQuery(id="c", from_user=ME, chat_instance="i", message=msg, data=data)
        return self._feed(Update(update_id=next(_ids), callback_query=cq))


def buttons(methods):
    for m in reversed(methods):
        rm = getattr(m, "reply_markup", None)
        if rm:
            return {b.text: b.callback_data for row in rm.inline_keyboard for b in row}
    return {}


def texts(methods):
    return [getattr(m, "text", None) or "" for m in methods]


@pytest.fixture
def h():
    cats = copy.deepcopy(DEFAULT_CATEGORIES)
    for c in cats:
        if c.name == "Прочее" and c.type == OpType.INCOME:
            c.keywords += ["стипендия", "вуз"]
    return Harness(MemoryStorage(categories=cats))


def ops(h):
    return asyncio.run(h.svc.storage.list_operations())


def test_vuz_without_plus_is_income(h):
    out = h.text("02.10 вуз 3104")
    assert "🟢 Доход" in texts(out)[0] and "Прочее" in texts(out)[0]
    [op] = ops(h)
    assert op.type == OpType.INCOME and op.date == datetime.date(2026, 10, 2)


def test_unknown_word_toggle_to_income(h):
    out = h.text("500 абырвалг")
    kb = buttons(out)
    assert "🔄 Это доход" in kb and "🏦 Ипотека" in kb
    out = h.click(kb["🔄 Это доход"])
    kb = buttons(out)
    assert "💼 Зарплата" in kb and "🏦 Ипотека" not in kb and "🔄 Это расход" in kb
    out = h.click(kb["💼 Зарплата"])
    assert "🟢 Доход" in texts(out)[0]
    assert ops(h)[0].type == OpType.INCOME
    # слово запомнилось за доходной категорией
    out = h.text("700 абырвалг")
    assert "🟢 Доход" in texts(out)[0]


def test_ambiguous_word_asks_income_or_expense(h):
    kb = buttons(h.text("5000 подарок"))
    assert set(kb) == {"🟢 Доход · 🎁 Подарки", "🔴 Расход · 🎁 Подарки", "✖️ Не записывать"}
    out = h.click(kb["🟢 Доход · 🎁 Подарки"])
    assert "🟢 Доход" in texts(out)[0]
    assert ops(h)[0].type == OpType.INCOME


def test_cancel_says_what_was_not_saved(h):
    kb = buttons(h.text("500 абырвалг"))
    out = h.click(kb["✖️ Не записывать"])
    assert "Отменено, не записано" in texts(out)[0] and "500" in texts(out)[0]
    assert ops(h) == []


def test_new_input_after_refresh_and_pending(h):
    h.text("500 абырвалг")          # незавершенный выбор категории не мешает следующему вводу
    h.text("/refresh")
    out = h.text("02.10 вуз 3104")
    assert "🟢 Доход" in texts(out)[0]


def test_storage_error_reports_and_keeps_pending(h):
    kb = buttons(h.text("500 абырвалг"))
    storage = h.svc.storage
    real_add = storage.add_operation

    async def broken(op):
        raise RuntimeError("Sheets API 503")

    storage.add_operation = broken
    out = h.click(kb["🎭 Досуг"])
    assert any("Не получилось" in (getattr(m, "text", "") or "") for m in out)
    storage.add_operation = real_add
    out = h.click(kb["🎭 Досуг"])      # повторное нажатие срабатывает: операция не потерялась
    assert "✅" in texts(out)[0]
    assert len(ops(h)) == 1

    storage.add_operation = broken
    out = h.text("350 кофе")
    assert any("Не получилось" in t for t in texts(out))


def test_emoji_in_saved_reply_and_last(h):
    out = h.text("350 кофе")
    assert "🍽 Еда вне дома" in texts(out)[0]
    out = h.text("/last")
    assert "🍽 Еда вне дома" in texts(out)[0]


def test_monthly_snapshot_ends_with_month_close(h):
    q = asyncio.run(h.svc.start_snapshot(1, purpose="monthly"))
    out = []
    for _ in range(q.total):
        out = h.text("1000")
    assert "итоги месяца" in texts(out)[0]
    assert h.svc.state.month_closed("2026-10")
    # обычный /snapshot заканчивается сводкой /capital
    h.text("/snapshot")
    for _ in range(q.total):
        out = h.text("1000")
    assert "итоги месяца" not in texts(out)[0] and "💼" in texts(out)[0]


def test_fallback_job_sends_once(h):
    from bot.scheduler import monthly_fallback_job
    n = len(h.session.sent)
    asyncio.run(monthly_fallback_job(h.bot, h.svc, frozenset({1})))
    asyncio.run(monthly_fallback_job(h.bot, h.svc, frozenset({1})))
    sent = [m for m in h.session.sent[n:] if "итоги месяца" in (getattr(m, "text", "") or "")]
    assert len(sent) == 1


def test_russian_aliases(h):
    assert "⚖️" in texts(h.text("/сравнить август сентябрь"))[0]
    assert "🧾" in texts(h.text("/крупные"))[0]
    assert "💼" in texts(h.text("/деньги"))[0]
