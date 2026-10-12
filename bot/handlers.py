from __future__ import annotations

import html
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import reports
from .models import Category, Operation, OpType
from .parser import ParseError
from .service import FinanceService, SnapshotQuestion, parse_plain_amount
from .textutil import fmt_money, fmt_short_date

log = logging.getLogger(__name__)
router = Router(name="finance")

HELP = """<b>✍️ Записать</b> — просто сообщением
<code>350 кофе</code> — расход · <code>+150000 зп</code> — доход
<code>вчера 1200 такси</code>, <code>5.10 800 аптека</code> — с датой
<code>2000 продукты @наличные</code> — с другого счета
<code>5000 Карта -> Вклад</code> — перевод между счетами
<code>283000 отпуск #тяжелое</code> — тяжелая трата: в статистике считается отдельно
<code>1000 здоровье, сходил к врачу</code> — после запятой просто комментарий
Без «+» — расход; слова доходных категорий («зп», «стипендия») пишутся доходом сами.
Под записью: ↩️ отменить · 🏷 категория · ✏️ сумма

<b>📅 Сейчас</b>
/month — текущий месяц · /week — неделя
/last — последние записи · /undo — отменить последнюю

<b>📊 Отчеты</b> — траты делятся на обычные, 🧾 тяжелые и ✈️ командировки
/report — статистика прошлого месяца (приходит сама 1 числа)
/stats 3, 6 или 12 — графики и тенденции по обычным тратам
/compare август сентябрь — два месяца рядом
/cat продукты — одна категория · /big — тяжелые траты за год
«Обычно» — медиана за 6 месяцев; ▲/▼ — только если вышло за привычные пределы

<b>💼 Деньги</b>
/capital — капитал по счетам · /snapshot — записать остатки

<b>✈️ Командировки</b>
/trip Казань 13.10–16.10 — завести поездку: траты в эти даты пометятся сами,
билеты заранее — кнопкой «✈️ В Казань» под тратой
/trip — ближайшая поездка · /trip cancel — отменить · /trips — все поездки
Статью поездки (🏨 Жилье, 🍽 Кафешки, 🚆 Транспорт/РЖД, 📌 Прочее) бот определит сам; сменить — кнопкой «🧳 Статья»

<b>⚙️ Настройка</b>
/categories — категории · /signals on|off — напоминания · /refresh — перечитать таблицу"""

class PickCat(CallbackData, prefix="pc"):
    pid: str
    idx: int  # -1 — отмена, -2 — переключить доход/расход


class PickType(CallbackData, prefix="pt"):
    pid: str
    income: bool


class ChangeCat(CallbackData, prefix="cc"):
    op_id: str
    idx: int  # -1 — показать список


class EditAmount(CallbackData, prefix="ea"):
    op_id: str


class DelOp(CallbackData, prefix="del"):
    op_id: str
    from_list: bool = False


class MonthView(CallbackData, prefix="mv"):
    view: str  # main | all | last


class ReportView(CallbackData, prefix="rv"):
    month: str  # ГГГГ-ММ
    view: str   # main | heavy | trips


class StatsView(CallbackData, prefix="sv"):
    n: int
    view: str   # main | heavy | trips


class TripItemCb(CallbackData, prefix="ti"):
    op_id: str
    idx: int  # -1 — показать список статей


class TripMark(CallbackData, prefix="tm"):
    op_id: str
    on: bool


class Snap(CallbackData, prefix="snap"):
    pos: int  # номер вопроса; -1 — прервать опрос


class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]) -> None:
        self.allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed:
            if user is not None:
                log.warning("Игнорирую пользователя user_id=%s (@%s)", user.id, user.username)
            return None
        return await handler(event, data)


# ---------- клавиатуры ----------

BACK = "← Назад"


def month_keyboard(view: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if view == "main":
        kb.button(text="📋 Все категории", callback_data=MonthView(view="all"))
        kb.button(text="🕑 Последние траты", callback_data=MonthView(view="last"))
    else:
        kb.button(text=BACK, callback_data=MonthView(view="main"))
    return kb.as_markup()


def report_keyboard(month, view: str, has_heavy: bool, has_trips: bool) -> InlineKeyboardMarkup | None:
    key = month.strftime("%Y-%m")
    kb = InlineKeyboardBuilder()
    if view == "main":
        if has_heavy:
            kb.button(text="🧾 Тяжелые", callback_data=ReportView(month=key, view="heavy"))
        if has_trips:
            kb.button(text="✈️ Командировка", callback_data=ReportView(month=key, view="trips"))
    else:
        kb.button(text=BACK, callback_data=ReportView(month=key, view="main"))
    markup = kb.as_markup()
    return markup if markup.inline_keyboard else None


def stats_keyboard(n: int, view: str, has_heavy: bool, has_trips: bool) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    if view == "main":
        if has_heavy:
            kb.button(text="🧾 Тяжелые", callback_data=StatsView(n=n, view="heavy"))
        if has_trips:
            kb.button(text="✈️ Командировки", callback_data=StatsView(n=n, view="trips"))
    else:
        kb.button(text=BACK, callback_data=StatsView(n=n, view="main"))
    markup = kb.as_markup()
    return markup if markup.inline_keyboard else None


async def _edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:  # «message is not modified» при повторном нажатии
        pass
    await call.answer()

CANCEL_TEXT = "✖️ Не записывать"


def category_keyboard(
    categories: list[Category],
    make_cb: Callable[[int], CallbackData],
    cancel: CallbackData | None,
    toggle: tuple[str, CallbackData] | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(categories):
        kb.button(text=c.label, callback_data=make_cb(i))
    kb.adjust(2)
    extra = []
    if toggle is not None:
        extra.append(InlineKeyboardButton(text=toggle[0], callback_data=toggle[1].pack()))
    if cancel is not None:
        extra.append(InlineKeyboardButton(text=CANCEL_TEXT, callback_data=cancel.pack()))
    if extra:
        kb.row(*extra)
    return kb.as_markup()


async def pick_category_view(svc: FinanceService, pid: str, op: Operation) -> tuple[str, InlineKeyboardMarkup]:
    """Выбор категории для новой операции + переключатель «это доход/расход»."""
    cats = await svc.categories_for(op.type)
    toggle_text = "🔄 Это доход" if op.type == OpType.EXPENSE else "🔄 Это расход"
    kb = category_keyboard(
        cats, lambda i: PickCat(pid=pid, idx=i), PickCat(pid=pid, idx=-1),
        toggle=(toggle_text, PickCat(pid=pid, idx=-2)),
    )
    return f"Выберите категорию для: {svc.describe(op)}", kb


def pick_type_view(svc: FinanceService, pid: str, op: Operation, choices: dict[OpType, str]) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🟢 Доход · {svc.cat_label(OpType.INCOME, choices[OpType.INCOME])}", callback_data=PickType(pid=pid, income=True))
    kb.button(text=f"🔴 Расход · {svc.cat_label(OpType.EXPENSE, choices[OpType.EXPENSE])}", callback_data=PickType(pid=pid, income=False))
    kb.button(text=CANCEL_TEXT, callback_data=PickCat(pid=pid, idx=-1))
    kb.adjust(1)
    what = html.escape(op.comment) if op.comment else "операция"
    text = f"«{what}» {fmt_money(op.amount, svc.cur)} — это доход или расход?"
    return text, kb.as_markup()


def saved_keyboard(op: Operation, trip=None, in_trip: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить", callback_data=DelOp(op_id=op.id))
    if op.type != OpType.TRANSFER:
        kb.button(text="🏷 Категория", callback_data=ChangeCat(op_id=op.id, idx=-1))
    kb.button(text="✏️ Сумма", callback_data=EditAmount(op_id=op.id))
    if trip is not None:
        if in_trip:
            kb.button(text=f"🧳 Статья: {op.trip_item}" if op.trip_item else "🧳 Статья",
                      callback_data=TripItemCb(op_id=op.id, idx=-1))
            kb.button(text="✖️ Не командировка", callback_data=TripMark(op_id=op.id, on=False))
        else:
            kb.button(text=f"✈️ В {trip.city}"[:40], callback_data=TripMark(op_id=op.id, on=True))
    kb.adjust(2)
    return kb.as_markup()


async def saved_kb(svc: FinanceService, op: Operation) -> InlineKeyboardMarkup:
    trip, in_trip = await svc.trip_for_button(op)
    return saved_keyboard(op, trip, in_trip)


async def last_view(svc: FinanceService) -> tuple[str, InlineKeyboardMarkup | None]:
    ops = await svc.last(10)
    if not ops:
        return "Операций пока нет.", None
    lines = ["<b>Последние операции</b> (сверху — самая новая):"]
    kb = InlineKeyboardBuilder()
    for i, op in enumerate(ops, 1):
        lines.append(f"{i}. {svc.describe_short(op)}")
        kb.button(text=f"❌ {i}", callback_data=DelOp(op_id=op.id, from_list=True))
    kb.adjust(5)
    lines.append("\nНажмите ❌ с номером, чтобы удалить.")
    return "\n".join(lines), kb.as_markup()


# ---------- команды ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("month"))
async def cmd_month(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.month_text(), reply_markup=month_keyboard("main"))


@router.message(Command("week"))
async def cmd_week(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.week_text())


@router.message(Command("report"))
async def cmd_report(message: Message, svc: FinanceService) -> None:
    month = reports.add_months(svc.today(), -1)
    text, heavy, trips = await svc.month_close_view(month)
    await message.answer(text, reply_markup=report_keyboard(month, "main", heavy, trips))


@router.message(Command("cat"))
async def cmd_cat(message: Message, command: CommandObject, svc: FinanceService) -> None:
    if not command.args:
        await message.answer("Укажите категорию: <code>/cat продукты</code>. Список: /categories")
        return
    await message.answer(await svc.category_text(command.args))


@router.message(Command("compare", "сравнить"))
async def cmd_compare(message: Message, command: CommandObject, svc: FinanceService) -> None:
    await message.answer(await svc.compare_text(command.args))


@router.message(Command("big", "крупные"))
async def cmd_big(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.big_text())


@router.message(Command("stats", "статистика", "quarter", "квартал"))
async def cmd_stats(message: Message, command: CommandObject, svc: FinanceService) -> None:
    from .notify import send_stats

    arg = (command.args or "").strip()
    n = int(arg) if arg.isdigit() and int(arg) in reports.STATS_PERIODS else 3
    if arg and not (arg.isdigit() and int(arg) in reports.STATS_PERIODS):
        await message.answer("Период — 3, 6 или 12 месяцев: <code>/stats 6</code>. Показываю за 3.")
    await send_stats(message.bot, message.chat.id, svc, n)


@router.message(Command("signals", "сигналы"))
async def cmd_signals(message: Message, command: CommandObject, svc: FinanceService) -> None:
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл"):
        svc.state.set("signals_enabled", True)
    elif arg in ("off", "выкл"):
        svc.state.set("signals_enabled", False)
    enabled = svc.state.get("signals_enabled", True)
    text = ("🔔 Сигналы включены: не чаще раза в неделю бот напомнит, если категория к 20-му числу "
            "превысила свое обычное за месяц, «Прочее» больше 10% расходов или не сошлась сверка капитала. "
            "Выключить: /signals off") if enabled else "🔕 Сигналы выключены. Включить: /signals on"
    if enabled and arg not in ("on", "вкл"):
        current = await svc.signals()
        if current:
            text += "\n\nСейчас:\n" + "\n".join(f"• {t}" for _, t in current)
    await message.answer(text)


@router.message(Command("trip", "поездка"))
async def cmd_trip(message: Message, command: CommandObject, svc: FinanceService) -> None:
    await message.answer(await svc.trip_command(command.args))


@router.message(Command("trips", "командировки", "поездки"))
async def cmd_trips(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.trips_text())


@router.callback_query(TripItemCb.filter())
async def on_trip_item(call: CallbackQuery, callback_data: TripItemCb, svc: FinanceService) -> None:
    from .models import TRIP_ITEM_ICONS, TRIP_ITEMS

    if callback_data.idx < 0:
        kb = InlineKeyboardBuilder()
        for i, item in enumerate(TRIP_ITEMS):
            kb.button(text=f"{TRIP_ITEM_ICONS[item]} {item}", callback_data=TripItemCb(op_id=callback_data.op_id, idx=i))
        kb.adjust(3)
        await call.message.edit_reply_markup(reply_markup=kb.as_markup())
        await call.answer()
        return
    op = await svc.set_trip_item(callback_data.op_id, TRIP_ITEMS[callback_data.idx])
    if op is None:
        await call.answer("Не получилось: это не командировочная трата", show_alert=True)
        return
    try:
        await call.message.edit_text(f"✅ {svc.describe(op)}", reply_markup=await saved_kb(svc, op))
    except TelegramBadRequest:
        pass
    await call.answer(f"Статья: {op.trip_item}")


@router.callback_query(TripMark.filter())
async def on_trip_mark(call: CallbackQuery, callback_data: TripMark, svc: FinanceService) -> None:
    op = await svc.toggle_trip(callback_data.op_id, callback_data.on)
    if op is None:
        await call.answer("Не получилось: операция или поездка не найдена", show_alert=True)
        return
    try:
        await call.message.edit_text(f"✅ {svc.describe(op)}", reply_markup=await saved_kb(svc, op))
    except TelegramBadRequest:
        pass
    await call.answer("Помечено как командировка" if callback_data.on else "Пометка снята")


@router.message(Command("categories"))
async def cmd_categories(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.categories_text())


@router.message(Command("last"))
async def cmd_last(message: Message, svc: FinanceService) -> None:
    text, kb = await last_view(svc)
    await message.answer(text, reply_markup=kb)


@router.message(Command("undo"))
async def cmd_undo(message: Message, svc: FinanceService) -> None:
    op = await svc.undo()
    await message.answer(f"Удалено: {svc.describe(op)}" if op else
                         "Отменять нечего: /undo отменяет записи, сделанные ботом. Удалить любую — в /last.")


@router.message(Command("refresh"))
async def cmd_refresh(message: Message, svc: FinanceService) -> None:
    await svc.storage.refresh()
    await message.answer("🔄 Данные перечитаны из таблицы.")


@router.message(Command("capital"))
async def cmd_capital(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.capital_text())


@router.message(Command("snapshot", "деньги"))
async def cmd_snapshot(message: Message, svc: FinanceService) -> None:
    q = await svc.start_snapshot(message.from_user.id)
    if q is None:
        await message.answer("В листе «Счета» нет ни одного счета.")
        return
    text, kb = snapshot_question_view(q, svc.cur)
    await message.answer(text, reply_markup=kb)


# ---------- снимок остатков ----------

def snapshot_question_view(q: SnapshotQuestion, cur: str) -> tuple[str, InlineKeyboardMarkup]:
    text = f"💼 {html.escape(q.account.prompt)} ({q.position + 1}/{q.total})"
    kb = InlineKeyboardBuilder()
    if q.previous is not None:
        text += f"\n<i>В прошлый раз ({fmt_short_date(q.previous.date)}): {fmt_money(q.previous.amount, cur)}</i>"
        kb.button(text="↪️ Как в прошлый раз", callback_data=Snap(pos=q.position))
    text += "\nПришлите сумму числом, например <code>12400</code>."
    kb.button(text="✖️ Прервать", callback_data=Snap(pos=-1))
    kb.adjust(1)
    return text, kb.as_markup()


async def send_snapshot_step(message: Message, svc: FinanceService, nxt: SnapshotQuestion | None, done: str = "") -> None:
    """Следующий вопрос опроса (или итог); done — подтверждение только что записанного ответа."""
    prefix = f"{done}\n\n" if done else ""
    if nxt is not None:
        text, kb = snapshot_question_view(nxt, svc.cur)
        await message.answer(prefix + text, reply_markup=kb)
    elif svc.pop_finished_purpose(message.chat.id) == "monthly":
        from .notify import send_month_close

        if done:
            await message.answer(done)
        await send_month_close(message.bot, message.chat.id, svc)
    else:
        await message.answer(prefix + await svc.capital_text())


@router.callback_query(Snap.filter())
async def on_snapshot_button(call: CallbackQuery, callback_data: Snap, svc: FinanceService) -> None:
    user_id = call.from_user.id
    current = svc.snapshot_question(user_id)
    if callback_data.pos < 0:
        svc.cancel_snapshot(user_id)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("Опрос прерван. Записанные ответы сохранены. Начать заново: /snapshot")
        await call.answer()
        return
    if current is None or current.position != callback_data.pos:
        await call.answer("Этот вопрос уже не актуален", show_alert=True)
        return
    _, nxt = await svc.answer_snapshot(user_id, None)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Взято прошлое значение")
    await send_snapshot_step(call.message, svc, nxt)


# ---------- переключение видов отчетов ----------

@router.callback_query(MonthView.filter())
async def on_month_view(call: CallbackQuery, callback_data: MonthView, svc: FinanceService) -> None:
    await _edit(call, await svc.month_text(callback_data.view), month_keyboard(callback_data.view))


@router.callback_query(ReportView.filter())
async def on_report_view(call: CallbackQuery, callback_data: ReportView, svc: FinanceService) -> None:
    from datetime import date

    year, month = map(int, callback_data.month.split("-"))
    first = date(year, month, 1)
    text, heavy, trips = await svc.month_close_view(first, callback_data.view)
    await _edit(call, text, report_keyboard(first, callback_data.view, heavy, trips))


@router.callback_query(StatsView.filter())
async def on_stats_view(call: CallbackQuery, callback_data: StatsView, svc: FinanceService) -> None:
    text, _, heavy, trips = await svc.stats_view(callback_data.n, callback_data.view)
    await _edit(call, text, stats_keyboard(callback_data.n, callback_data.view, heavy, trips))


# ---------- ввод операций ----------

@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, svc: FinanceService) -> None:
    user_id = message.from_user.id
    op_id = svc.editing_op_id(user_id)
    if op_id is not None:
        svc.cancel_edit_amount(user_id)
        amount = parse_plain_amount(message.text)
        if amount is not None and amount > 0:
            op = await svc.set_amount(op_id, amount)
            if op is None:
                await message.answer("Операция не найдена — возможно, ее уже удалили.")
            else:
                await message.answer(f"✏️ Сумма исправлена: {svc.describe(op)}", reply_markup=await saved_kb(svc, op))
            return
        # не число — значит, передумали: сообщение обрабатывается как обычно
    q = svc.snapshot_question(user_id)
    if q is not None:
        amount = parse_plain_amount(message.text)
        if amount is not None:
            _, nxt = await svc.answer_snapshot(user_id, amount)
            done = f"💼 Остаток «{html.escape(q.account.name)}»: {fmt_money(amount, svc.cur)}"
            await send_snapshot_step(message, svc, nxt, done)
            return

    try:
        prepared = await svc.prepare(message.text)
    except ParseError as e:
        await message.answer(f"🤔 {html.escape(str(e))}.\nПример: <code>350 кофе</code>. Справка: /help")
        return

    op = prepared.op
    if prepared.type_choices:
        pid = svc.stash(op, prepared.type_choices)
        text, kb = pick_type_view(svc, pid, op, prepared.type_choices)
        await message.answer(text, reply_markup=kb)
        return
    if prepared.needs_category:
        pid = svc.stash(op)
        text, kb = await pick_category_view(svc, pid, op)
        await message.answer(text, reply_markup=kb)
        return

    await svc.save(op)
    await message.answer(f"✅ {svc.describe(op)}", reply_markup=await saved_kb(svc, op))


STALE = "Эта операция устарела (бот перезапускался) — отправьте ее заново"


async def save_or_restash(svc: FinanceService, op: Operation, choices: dict[OpType, str] | None = None) -> None:
    """Запись операции, уже снятой из ожидающих: если таблица не ответила — вернуть ее, чтобы нажать еще раз."""
    try:
        await svc.save(op)
    except Exception:
        svc.stash(op, choices)
        raise


@router.callback_query(PickType.filter())
async def on_pick_type(call: CallbackQuery, callback_data: PickType, svc: FinanceService) -> None:
    op = svc.get_pending(callback_data.pid)
    choices = svc.pending_choices(callback_data.pid)
    if op is None or not choices:
        await call.answer(STALE, show_alert=True)
        return
    svc.pop_pending(callback_data.pid)   # до записи: повторный тап не запишет операцию второй раз
    op.type = OpType.INCOME if callback_data.income else OpType.EXPENSE
    op.category = choices[op.type]
    await save_or_restash(svc, op, choices)
    await call.message.edit_text(f"✅ {svc.describe(op)}", reply_markup=await saved_kb(svc, op))
    await call.answer()


@router.callback_query(PickCat.filter())
async def on_pick_category(call: CallbackQuery, callback_data: PickCat, svc: FinanceService) -> None:
    op = svc.get_pending(callback_data.pid)
    if op is None:
        await call.answer(STALE, show_alert=True)
        return
    if callback_data.idx == -1:
        svc.pop_pending(callback_data.pid)
        await call.message.edit_text(f"✖️ Отменено, не записано: {svc.describe(op)}")
        await call.answer()
        return
    if callback_data.idx == -2:
        op.type = OpType.INCOME if op.type == OpType.EXPENSE else OpType.EXPENSE
        text, kb = await pick_category_view(svc, callback_data.pid, op)
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return
    cats = await svc.categories_for(op.type)
    if not 0 <= callback_data.idx < len(cats):
        await call.answer("Список категорий изменился — отправьте заново", show_alert=True)
        return
    if svc.pop_pending(callback_data.pid) is None:   # до записи: повторный тап уже ее забрал
        await call.answer()
        return
    op.category = cats[callback_data.idx].name
    await save_or_restash(svc, op)
    learned = await svc.learn(op)
    text = f"✅ {svc.describe(op)}"
    if learned:
        text += f"\n🧠 Запомнил: «{html.escape(learned)}» → {html.escape(op.category)}"
    await call.message.edit_text(text, reply_markup=await saved_kb(svc, op))
    await call.answer()


@router.callback_query(ChangeCat.filter())
async def on_change_category(call: CallbackQuery, callback_data: ChangeCat, svc: FinanceService) -> None:
    op = await svc.get_operation(callback_data.op_id)
    if op is None:
        await call.answer("Операция не найдена", show_alert=True)
        return
    cats = await svc.categories_for(op.type)
    if callback_data.idx < 0:
        kb = category_keyboard(cats, lambda i: ChangeCat(op_id=op.id, idx=i), None)
        await call.message.edit_reply_markup(reply_markup=kb)
        await call.answer()
        return
    if not 0 <= callback_data.idx < len(cats):
        await call.answer("Список категорий изменился", show_alert=True)
        return
    updated = await svc.set_category(op.id, cats[callback_data.idx].name)
    if updated is None:
        await call.answer("Не удалось обновить", show_alert=True)
        return
    learned = await svc.learn(updated)
    text = f"✅ {svc.describe(updated)}"
    if learned:
        text += f"\n🧠 Запомнил: «{html.escape(learned)}» → {html.escape(updated.category)}"
    await call.message.edit_text(text, reply_markup=await saved_kb(svc, updated))
    await call.answer("Категория изменена")


@router.callback_query(EditAmount.filter())
async def on_edit_amount(call: CallbackQuery, callback_data: EditAmount, svc: FinanceService) -> None:
    op = await svc.get_operation(callback_data.op_id)
    if op is None:
        await call.answer("Операция не найдена", show_alert=True)
        return
    svc.start_edit_amount(call.from_user.id, op.id)
    await call.message.answer(
        f"✏️ Новая сумма для: {svc.describe(op)}\nПришлите число, например <code>1250</code>. "
        "Передумали — просто напишите что-нибудь другое."
    )
    await call.answer()


@router.callback_query(DelOp.filter())
async def on_delete(call: CallbackQuery, callback_data: DelOp, svc: FinanceService) -> None:
    op = await svc.delete(callback_data.op_id)
    if op is None:
        await call.answer("Уже удалено", show_alert=True)
    else:
        await call.answer("Удалено")
    if callback_data.from_list:
        text, kb = await last_view(svc)
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    elif op is not None:
        await call.message.edit_text(f"🗑 Удалено: {svc.describe(op)}")


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message) -> None:
    await message.answer("Не знаю такой команды. Справка: /help")


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    """Любая ошибка (например, Google Sheets не ответил) — сообщить, а не молчать."""
    log.error("Ошибка при обработке update %s", event.update.update_id, exc_info=event.exception)
    text = "⚠️ Не получилось — скорее всего, Google Таблица не ответила. Попробуйте еще раз через минуту."
    try:
        if event.update.message:
            await event.update.message.answer(text)
        elif event.update.callback_query:
            await event.update.callback_query.answer(text, show_alert=True)
    except Exception:
        log.exception("Не удалось сообщить об ошибке")
    return True
