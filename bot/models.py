from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


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
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    kind: Kind = Kind.REGULAR

    def __post_init__(self) -> None:
        # Вид и теги согласованы: #тяжелое или тег поездки задают вид, если он не указан явно.
        if self.type != OpType.EXPENSE:
            self.kind = Kind.REGULAR
        elif self.kind == Kind.REGULAR:
            self.kind = kind_from_tags(self.tags)


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
