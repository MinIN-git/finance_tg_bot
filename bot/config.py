from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    storage: str
    spreadsheet_id: str
    credentials_file: str
    tz: ZoneInfo
    report_hour: int
    cache_ttl: int
    currency: str
    state_file: str

    @classmethod
    def from_env(cls) -> "Config":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass

        def req(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ConfigError(f"Не задана переменная окружения {name} (см. .env.example)")
            return value

        storage = os.getenv("STORAGE", "sheets").strip().lower()
        ids_raw = req("ALLOWED_USER_IDS")
        try:
            ids = frozenset(int(x) for x in ids_raw.replace(";", ",").split(",") if x.strip())
        except ValueError as e:
            raise ConfigError("ALLOWED_USER_IDS — числа через запятую") from e
        return cls(
            bot_token=req("BOT_TOKEN"),
            allowed_user_ids=ids,
            storage=storage,
            spreadsheet_id=req("SPREADSHEET_ID") if storage == "sheets" else os.getenv("SPREADSHEET_ID", ""),
            credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "secrets/service_account.json"),
            tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
            report_hour=int(os.getenv("REPORT_HOUR", "10")),
            cache_ttl=int(os.getenv("CACHE_TTL", "300")),
            currency=os.getenv("CURRENCY", "").strip(),
            state_file=os.getenv("STATE_FILE", "data/state.json"),
        )
