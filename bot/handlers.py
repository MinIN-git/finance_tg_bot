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

from .models import Category, Operation, OpType
from .parser import ParseError
from .service import FinanceService, SnapshotQuestion, parse_plain_amount
from .textutil import fmt_money, fmt_short_date

log = logging.getLogger(__name__)
router = Router(name="finance")

HELP = """<b>Как записывать</b>
<code>350 кофе</code> — расход
<code>+150000 зп</code> — доход
<code>вчера 1200 такси</code>, <code>5.10 800 аптека</code> — с датой
<code>4500 отель #командировка</code> — с тегом
<code>2000 продукты @наличные</code> — с другого счета
<code>5000 Карта -> Наличные</code> — перевод между счетами
Копейки — через запятую: <code>99,90 молоко</code>. Можно <code>1.5к</code> = 1500.

Без «+» сумма считается расходом, но если слово есть только у доходной категории
(например, «стипендия» в «Прочее»), запишется доход. Если слово подходит и туда, и туда, бот спросит.
Если категорию не удалось угадать, бот предложит кнопки (с переключателем «это доход») и запомнит слово.

<b>Команды</b>
/month — текущий месяц
/week — текущая неделя
/cat <i>категория</i> — расходы по категории
/report — отчет за прошлый месяц
/last — последние 10 операций (с удалением)
/undo — удалить последнюю добавленную
/capital — остатки по счетам и динамика за 1/3/6/12 мес.
/snapshot — внеплановый опрос остатков по счетам (/деньги)
/categories — категории и ключевые слова
/refresh — перечитать таблицу после ручных правок"""

class PickCat(CallbackData, prefix="pc"):
    pid: str
    idx: int  # -1 — отмена, -2 — переключить доход/расход


class PickType(CallbackData, prefix="pt"):
    pid: str
    income: bool


class ChangeCat(CallbackData, prefix="cc"):
    op_id: str
    idx: int  # -1 — показать список


class DelOp(CallbackData, prefix="del"):
    op_id: str
    from_list: bool = False


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

CANCEL_TEXT = "✖️ Не записывать"


def category_keyboard(
    categories: list[Category],
    make_cb: Callable[[int], CallbackData],
    cancel: CallbackData | None,
    toggle: tuple[str, CallbackData] | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(categories):
        kb.button(text=c.name, callback_data=make_cb(i))
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
    kb.button(text=f"🟢 Доход · {choices[OpType.INCOME]}", callback_data=PickType(pid=pid, income=True))
    kb.button(text=f"🔴 Расход · {choices[OpType.EXPENSE]}", callback_data=PickType(pid=pid, income=False))
    kb.button(text=CANCEL_TEXT, callback_data=PickCat(pid=pid, idx=-1))
    kb.adjust(1)
    what = html.escape(op.comment) if op.comment else "операция"
    text = f"«{what}» {fmt_money(op.amount, svc.cur)} — это доход или расход?"
    return text, kb.as_markup()


def saved_keyboard(op: Operation) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить", callback_data=DelOp(op_id=op.id))
    if op.type != OpType.TRANSFER:
        kb.button(text="🏷 Категория", callback_data=ChangeCat(op_id=op.id, idx=-1))
    return kb.as_markup()


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
    await message.answer(await svc.month_text())


@router.message(Command("week"))
async def cmd_week(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.week_text())


@router.message(Command("report"))
async def cmd_report(message: Message, svc: FinanceService) -> None:
    await message.answer(await svc.monthly_report_text())


@router.message(Command("cat"))
async def cmd_cat(message: Message, command: CommandObject, svc: FinanceService) -> None:
    if not command.args:
        await message.answer("Укажите категорию: <code>/cat продукты</code>. Список: /categories")
        return
    await message.answer(await svc.category_text(command.args))


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
    await message.answer(f"Удалено: {svc.describe(op)}" if op else "Удалять нечего.")


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


async def send_snapshot_step(message: Message, svc: FinanceService, nxt: SnapshotQuestion | None) -> None:
    if nxt is not None:
        text, kb = snapshot_question_view(nxt, svc.cur)
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(await svc.capital_text())


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


# ---------- ввод операций ----------

@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, svc: FinanceService) -> None:
    user_id = message.from_user.id
    if svc.snapshot_question(user_id) is not None:
        amount = parse_plain_amount(message.text)
        if amount is not None:
            _, nxt = await svc.answer_snapshot(user_id, amount)
            await send_snapshot_step(message, svc, nxt)
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
    await message.answer(f"✅ {svc.describe(op)}", reply_markup=saved_keyboard(op))


STALE = "Эта операция устарела (бот перезапускался) — отправьте ее заново"


@router.callback_query(PickType.filter())
async def on_pick_type(call: CallbackQuery, callback_data: PickType, svc: FinanceService) -> None:
    op = svc.get_pending(callback_data.pid)
    choices = svc.pending_choices(callback_data.pid)
    if op is None or not choices:
        await call.answer(STALE, show_alert=True)
        return
    op.type = OpType.INCOME if callback_data.income else OpType.EXPENSE
    op.category = choices[op.type]
    await svc.save(op)
    svc.pop_pending(callback_data.pid)
    await call.message.edit_text(f"✅ {svc.describe(op)}", reply_markup=saved_keyboard(op))
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
    op.category = cats[callback_data.idx].name
    await svc.save(op)
    svc.pop_pending(callback_data.pid)
    learned = await svc.learn(op)
    text = f"✅ {svc.describe(op)}"
    if learned:
        text += f"\n🧠 Запомнил: «{html.escape(learned)}» → {html.escape(op.category)}"
    await call.message.edit_text(text, reply_markup=saved_keyboard(op))
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
    await call.message.edit_text(text, reply_markup=saved_keyboard(updated))
    await call.answer("Категория изменена")


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
