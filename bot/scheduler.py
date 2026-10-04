from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config
from .handlers import snapshot_question_view
from .service import FinanceService

log = logging.getLogger(__name__)


async def monthly_job(bot: Bot, svc: FinanceService, user_ids: frozenset[int]) -> None:
    """1 числа: отчет за прошлый месяц и опрос остатков по каждому счету."""
    await svc.storage.refresh()
    report = await svc.monthly_report_text()
    for uid in user_ids:
        try:
            await bot.send_message(uid, report)
            q = await svc.start_snapshot(uid)
            if q is not None:
                text, kb = snapshot_question_view(q, svc.cur)
                await bot.send_message(uid, text, reply_markup=kb)
        except Exception:
            log.exception("Не удалось отправить ежемесячный отчет пользователю %s", uid)


def setup_scheduler(bot: Bot, svc: FinanceService, config: Config) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.tz)
    scheduler.add_job(
        monthly_job,
        CronTrigger(day=1, hour=config.report_hour, minute=0, timezone=config.tz),
        args=[bot, svc, config.allowed_user_ids],
        id="monthly_report",
        misfire_grace_time=6 * 3600,
        coalesce=True,
    )
    return scheduler
