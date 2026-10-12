from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


def new_op_id() -> str:
    return uuid.uuid4().hex[:10]


class OpType(StrEnum):
    EXPENSE = "расход"
    INCOME = "доход"
    TRANSFER = "перевод"


class Kind(StrEnum):
    """Поток трат: в разбивке по категориям и в «обычно» участвуют только обычные."""
    REGULAR = "обычная"
    HEAVY = "тяжелая"
    TRIP = "командировка"


@dataclass
class Operation:
    date: date
    type: OpType
    amount: Decimal
    category: str = ""
    account: str = ""
    to_account: str = ""
    comment: str = ""
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=new_op_id)
    kind: Kind = Kind.REGULAR
    trip_item: str = ""    # статья поездки (только у командировочных): Жилье, Кафешки, Билеты, Транспорт, Прочее

    def __post_init__(self) -> None:
        # Вид и теги согласованы: #тяжелое или тег поездки задают вид, если он не указан явно.
        if self.type != OpType.EXPENSE:
            self.kind = Kind.REGULAR
        elif self.kind == Kind.REGULAR:
            self.kind = kind_from_tags(self.tags)
        if self.kind != Kind.TRIP:
            self.trip_item = ""
        elif self.trip_item not in TRIP_ITEMS:
            alias = TRIP_ITEM_ALIASES.get(self.trip_item.strip().lower().replace("ё", "е"))
            self.trip_item = alias or guess_trip_item(self.comment, self.category)


@dataclass
class Category:
    name: str
    type: OpType
    keywords: list[str] = field(default_factory=list)
    emoji: str = ""

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}" if self.emoji else self.name


@dataclass
class Account:
    name: str
    question: str = ""  # вопрос для ежемесячного снимка остатков

    @property
    def prompt(self) -> str:
        return self.question or f"Сколько на счете «{self.name}»?"


@dataclass
class BigPurchase:
    """Разовая крупная трата из листа «Большие покупки». Считается, что она также записана
    обычным расходом в «Операции» (входит в расходы месяца), а здесь лишь помечена как разовая."""
    date: date
    what: str
    amount: Decimal
    category: str = ""     # пусто у старых строк — тогда трата вычитается только из общего расхода
    op_id: str = ""        # id операции, если строку дописал бот по тегу #тяжелое


HEAVY_TAG = "тяжелое"
HEAVY_TAG_ALIASES = {"тяжелое", "тяжелая", "крупное", "крупная"}


def is_heavy(op: "Operation") -> bool:
    return op.type == OpType.EXPENSE and op.kind == Kind.HEAVY


def is_trip(op: "Operation") -> bool:
    return op.type == OpType.EXPENSE and op.kind == Kind.TRIP


def is_regular(op: "Operation") -> bool:
    return op.kind == Kind.REGULAR


def kind_from_tags(tags: list[str]) -> Kind:
    """Вид для строк без колонки «Вид» (записанных до ее появления): по тегам."""
    if HEAVY_TAG in tags:
        return Kind.HEAVY
    if any(t.startswith(TRIP_TAG_PREFIX) for t in tags):
        return Kind.TRIP
    return Kind.REGULAR


TRIP_TAG_PREFIX = "поездка-"

# Статьи командировок. Билеты и местный транспорт — одна статья «Транспорт/РЖД».
TRIP_ITEMS = ["Жилье", "Кафешки", "Транспорт/РЖД", "Прочее"]
TRIP_ITEM_ICONS = {"Жилье": "🏨", "Кафешки": "🍽", "Транспорт/РЖД": "🚆", "Прочее": "📌"}
TRIP_ITEM_WORDS = [   # по словам в комментарии; первое совпадение
    ("Жилье", ("отел", "гостиниц", "хостел", "квартир", "жиль", "airbnb", "суточн", "посуточн", "апартамент")),
    ("Транспорт/РЖД", ("ржд", "жд", "поезд", "самолет", "авиа", "билет", "аэроэкспресс", "ласточк", "сапсан",
                       "такси", "метро", "автобус", "каршеринг", "транспорт", "трансфер", "электричк")),
]
TRIP_ITEM_BY_CATEGORY = {"Еда вне дома": "Кафешки", "Транспорт/РЖД": "Транспорт/РЖД"}
# Статьи листа «Командировки» и прежние названия -> статья поездки.
TRIP_ITEM_ALIASES = {"жд билеты": "Транспорт/РЖД", "билеты": "Транспорт/РЖД", "транспорт": "Транспорт/РЖД",
                     "транспорт/ржд": "Транспорт/РЖД", "кафешки": "Кафешки", "жилье": "Жилье", "прочее": "Прочее"}
_MARKER_RE = re.compile(r"\[[^\]\s]+-\d{6}\s(.+?)\]")   # метка миграции: [командировка-130726 ЖД билеты]


def guess_trip_item(comment: str, category: str) -> str:
    """Статья поездки: из метки миграции, по словам в комментарии, затем по категории."""
    m = _MARKER_RE.search(comment)
    if m:
        item = TRIP_ITEM_ALIASES.get(m.group(1).strip().lower().replace("ё", "е"))
        if item:
            return item
    words = re.findall(r"\w+", comment.lower().replace("ё", "е"))
    for item, stems in TRIP_ITEM_WORDS:
        if any(w.startswith(stems) for w in words):
            return item
    return TRIP_ITEM_BY_CATEGORY.get(category, "Прочее")


@dataclass
class Trip:
    """Командировка из листа «Поездки». Траты помечаются тегом #поездка-<id>."""
    city: str
    start: date
    end: date
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            slug = re.sub(r"[^\w]+", "_", self.city.lower().replace("ё", "е")).strip("_")
            self.id = f"{slug}-{self.start:%d%m%y}"

    @property
    def tag(self) -> str:
        return f"{TRIP_TAG_PREFIX}{self.id}"

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end


def trip_tag(op: "Operation") -> str | None:
    return next((t for t in op.tags if t.startswith(TRIP_TAG_PREFIX)), None)


@dataclass
class BalanceEntry:
    """Остаток на счете на дату (ежемесячный снимок)."""
    date: date
    account: str
    amount: Decimal
