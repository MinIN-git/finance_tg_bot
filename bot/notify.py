"""Отправка итогов месяца, квартального отчета и сигналов (общая для команд и планировщика)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import BufferedInputFile

from . import reports
from .service import FinanceService

log = logging.getLogger(__name__)

SIGNALS_MIN_INTERVAL = timedelta(days=7)


async def send_quarter(bot: Bot, chat_id: int, svc: FinanceService) -> None:
    text, png = await svc.quarter_report()
    if png:
        await bot.send_photo(chat_id, BufferedInputFile(png, "quarter.png"))
    await bot.send_message(chat_id, text)


async def send_month_close(bot: Bot, chat_id: int, svc: FinanceService) -> None:
    """Итоги прошлого месяца; 1 января/апреля/июля/октября — еще и квартальный отчет с картинкой."""
    await bot.send_message(chat_id, await svc.month_close_text())
    svc.state.mark_month_closed(svc.today().strftime("%Y-%m"))
    if svc.today().month in reports.QUARTER_START_MONTHS:
        try:
            await send_quarter(bot, chat_id, svc)
        except Exception:
            log.exception("Не удалось отправить квартальный отчет")


async def send_signals(bot: Bot, chat_ids, svc: FinanceService, now: datetime | None = None) -> bool:
    """Новые сигналы, не чаще раза в неделю и только если они включены. True — что-то отправлено."""
    if not svc.state.get("signals_enabled", True):
        return False
    now = now or datetime.now(svc.tz)
    last = svc.state.get("signals_last_sent")
    if last and now - datetime.fromisoformat(last) < SIGNALS_MIN_INTERVAL:
        return False
    sent_keys = set(svc.state.get("signal_keys", []))
    fresh = [(k, t) for k, t in await svc.signals() if k not in sent_keys]
    if not fresh:
        return False
    text = "🔔 <b>На что обратить внимание</b>\n" + "\n".join(f"• {t}" for _, t in fresh) + "\n\nОтключить: /signals off"
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.exception("Не удалось отправить сигналы пользователю %s", chat_id)
    svc.state.set("signal_keys", [*sent_keys, *(k for k, _ in fresh)][-200:])
    svc.state.set("signals_last_sent", now.isoformat())
    return True
