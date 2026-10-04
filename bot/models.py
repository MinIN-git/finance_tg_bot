from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class OpType(StrEnum):
    EXPENSE = "расход"
    INCOME = "доход"
    TRANSFER = "перевод"


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


@dataclass
class Category:
    name: str
    type: OpType
    keywords: list[str] = field(default_factory=list)


@dataclass
class Account:
    name: str
    question: str = ""  # вопрос для ежемесячного снимка остатков

    @property
    def prompt(self) -> str:
        return self.question or f"Сколько на счете «{self.name}»?"


@dataclass
class BalanceEntry:
    """Остаток на счете на дату (ежемесячный снимок)."""
    date: date
    account: str
    amount: Decimal
