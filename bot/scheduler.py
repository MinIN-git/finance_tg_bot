from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config
from .handlers import snapshot_question_view
from .service import FinanceService
from .textutil import MONTHS_NOMINATIVE

log = logging.getLogger(__name__)

FALLBACK_HOUR = 21  # если до этого часа 1 числа опрос не пройден — итоги приходят без свежего снимка


async def monthly_job(bot: Bot, svc: FinanceService, user_ids: frozenset[int]) -> None:
    """1 числа: опрос остатков по счетам, после последнего ответа — итоги прошлого месяца."""
    await svc.storage.refresh()
    prev_month = MONTHS_NOMINATIVE[(svc.today().month - 2) % 12]
    for uid in user_ids:
        try:
            q = await svc.start_snapshot(uid, purpose="monthly")
            if q is None:  # счетов нет — сразу итоги
                await bot.send_message(uid, await svc.month_close_text())
                svc.state.mark_month_closed(svc.today().strftime("%Y-%m"))
                continue
            await bot.send_message(uid, f"📅 Подводим итоги: {prev_month}. Сначала остатки по счетам — итоги придут сразу после.")
            text, kb = snapshot_question_view(q, svc.cur)
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception:
            log.exception("Не удалось начать ежемесячный опрос для пользователя %s", uid)


async def monthly_fallback_job(bot: Bot, svc: FinanceService, user_ids: frozenset[int]) -> None:
    """Вечером 1 числа: если опрос так и не пройден — прислать итоги без свежего снимка."""
    key = svc.today().strftime("%Y-%m")
    if svc.state.month_closed(key):
        return
    report = await svc.month_close_text()
    for uid in user_ids:
        try:
            await bot.send_message(uid, report)
            if svc.snapshot_question(uid) is not None:
                await bot.send_message(uid, "Опрос остатков еще открыт — после ответов пришлю итоги со сверкой капитала.")
        except Exception:
            log.exception("Не удалось отправить итоги месяца пользователю %s", uid)
    svc.state.mark_month_closed(key)


def setup_scheduler(bot: Bot, svc: FinanceService, config: Config) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.tz)
    common = dict(args=[bot, svc, config.allowed_user_ids], misfire_grace_time=6 * 3600, coalesce=True)
    scheduler.add_job(
        monthly_job, CronTrigger(day=1, hour=config.report_hour, minute=0, timezone=config.tz),
        id="monthly_report", **common,
    )
    scheduler.add_job(
        monthly_fallback_job, CronTrigger(day=1, hour=FALLBACK_HOUR, minute=0, timezone=config.tz),
        id="monthly_fallback", **common,
    )
    return scheduler
