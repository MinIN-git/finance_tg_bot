"""Небольшое состояние бота в JSON-файле (переживает перезапуск контейнера):
включены ли сигналы, когда отправлялись, какие итоги месяца уже присланы."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                log.warning("Не удалось прочитать %s, начинаю с пустого состояния", path, exc_info=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("Не удалось сохранить %s", self.path, exc_info=True)

    # ----- итоги месяца -----

    def month_closed(self, month_key: str) -> bool:
        return month_key in self.get("month_close_sent", [])

    def mark_month_closed(self, month_key: str) -> None:
        sent = [k for k in self.get("month_close_sent", []) if k != month_key][-23:]
        self.set("month_close_sent", [*sent, month_key])
