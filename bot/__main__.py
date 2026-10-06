from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import Config, ConfigError
from .handlers import WhitelistMiddleware, router
from .scheduler import setup_scheduler
from .service import FinanceService
from .state import State
from .storage import MemoryStorage, SheetsStorage, Storage

COMMANDS = [
    BotCommand(command="month", description="Текущий месяц"),
    BotCommand(command="report", description="Итоги прошлого месяца"),
    BotCommand(command="stats", description="Графики и тенденции: /stats 3 | 6 | 12"),
    BotCommand(command="capital", description="Капитал по счетам"),
    BotCommand(command="trip", description="Командировка: /trip Казань 13.10–16.10"),
    BotCommand(command="trips", description="Все командировки"),
    BotCommand(command="compare", description="Сравнить месяцы: /compare август сентябрь"),
    BotCommand(command="cat", description="Одна категория: /cat продукты"),
    BotCommand(command="big", description="Тяжелые траты за год"),
    BotCommand(command="week", description="Текущая неделя"),
    BotCommand(command="last", description="Последние записи"),
    BotCommand(command="undo", description="Отменить последнюю запись"),
    BotCommand(command="snapshot", description="Записать остатки по счетам"),
    BotCommand(command="categories", description="Категории"),
    BotCommand(command="signals", description="Напоминания: /signals on | off"),
    BotCommand(command="refresh", description="Перечитать таблицу"),
    BotCommand(command="help", description="Справка"),
]


def build_storage(config: Config) -> Storage:
    if config.storage == "memory":
        logging.warning("STORAGE=memory: данные хранятся только в памяти и пропадут при перезапуске")
        return MemoryStorage()
    if not os.path.isfile(config.credentials_file):
        raise ConfigError(
            f"Не найден JSON-ключ service account: {config.credentials_file} (см. README, шаг 3)"
        )
    storage = SheetsStorage(config.credentials_file, config.spreadsheet_id, config.cache_ttl)
    storage.ensure_sheets()
    return storage


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    storage = await asyncio.to_thread(build_storage, config)
    svc = FinanceService(storage, tz=config.tz, currency=config.currency, state=State(config.state_file))

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(svc=svc)
    dp.update.outer_middleware(WhitelistMiddleware(config.allowed_user_ids))
    dp.include_router(router)

    scheduler = setup_scheduler(bot, svc, config)
    scheduler.start()
    await bot.set_my_commands(COMMANDS)
    logging.info("Бот запущен, разрешенные пользователи: %s", sorted(config.allowed_user_ids))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
