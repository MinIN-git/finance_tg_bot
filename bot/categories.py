"""Категории по умолчанию и угадывание категории по ключевым словам."""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from .models import Category, OpType
from .textutil import normalize

E, I = OpType.EXPENSE, OpType.INCOME

DEFAULT_CATEGORIES: list[Category] = [
    Category("Зарплата", I, ["зп", "зарплата", "аванс", "премия", "оклад", "отпускные"], "💼"),
    Category("Папа", I, ["папа", "отец"], "👨"),
    Category("Подарки", I, ["подарок", "подарили"], "🎁"),
    Category("Прочее", I, ["кэшбэк", "кешбэк", "кешбек", "возврат"], "💰"),
    Category("ЖКУ", E, ["жку", "коммуналка", "квартплата", "электричество", "отопление", "вода", "интернет"], "🏠"),
    Category("Ипотека", E, ["ипотека"], "🏦"),
    Category("Продукты домой", E, [
        "продукты", "пятерочка", "перекресток", "магнит", "лента", "ашан", "вкусвилл",
        "азбука", "дикси", "окей", "супермаркет", "самокат", "лавка",
    ], "🛒"),
    Category("Транспорт/РЖД", E, [
        "метро", "такси", "автобус", "электричка", "ржд", "поезд", "транспорт", "тройка",
        "самолет", "авиабилет", "аэроэкспресс", "каршеринг",
    ], "🚆"),
    Category("Машина", E, ["бензин", "азс", "заправка", "мойка", "парковка", "шиномонтаж", "осаго", "каско", "машина", "штраф"], "🚗"),
    Category("Здоровье", E, ["аптека", "врач", "лекарства", "анализы", "стоматолог", "клиника", "массаж", "здоровье"], "💊"),
    Category("Семейные расходы", E, ["семья", "дети", "детск", "садик", "школа"], "👨‍👩‍👧"),
    Category("Подарки", E, ["подарок", "подарки", "цветы"], "🎁"),
    Category("Досуг", E, [
        "кино", "театр", "концерт", "бар", "боулинг", "музей", "отпуск", "отель", "гостиница",
        "экскурсия", "досуг", "хобби", "steam",
    ], "🎭"),
    Category("Еда вне дома", E, [
        "кофе", "кафе", "ресторан", "обед", "ужин", "завтрак", "бургер", "пицца", "шаурма",
        "макдональдс", "kfc", "столовая", "ланч", "суши",
    ], "🍽"),
    Category("Маркетплейсы", E, ["озон", "ozon", "вб", "wb", "wildberries", "вайлдберриз", "яндекс маркет", "алиэкспресс", "мегамаркет"], "📦"),
    Category("Одежда / украшения", E, ["одежда", "обувь", "кроссовки", "куртка", "джинсы", "украшения", "футболка"], "👕"),
    Category("Вредные привычки", E, ["сигареты", "сиги", "табак", "вейп", "пиво", "алкоголь", "вино", "кальян"], "🚬"),
    Category("Подписки", E, [
        "подписка", "яндекс плюс", "netflix", "spotify", "youtube", "icloud", "кинопоиск", "chatgpt",
        "связь", "телефон", "мобильн", "сотовая", "тариф", "мтс", "билайн", "мегафон", "теле2", "tele2",
    ], "📱"),
    Category("Прочее", E, [], "📌"),
]

WORD_RE = re.compile(r"[\w]+", re.UNICODE)
# Короткие ключевые слова сравниваются целиком, длинные — как начало слова
# («кофе» поймает «кофейня», а «бар» не поймает «барбершоп»). У длинных слов
# отбрасываются конечные гласные, чтобы ловить падежи: «папа» -> «папы», «аптека» -> «аптеке».
PREFIX_MIN_LEN = 4
VOWELS = "аеиоуыэюяьй"


def _stem(keyword: str) -> str:
    stem = keyword.rstrip(VOWELS)
    return stem if len(stem) >= 3 else keyword


def words(text: str) -> list[str]:
    return WORD_RE.findall(normalize(text))


def _keyword_matches(keyword: str, text_norm: str, text_words: Sequence[str]) -> bool:
    if " " in keyword:
        return keyword in text_norm
    if len(keyword) >= PREFIX_MIN_LEN:
        stem = _stem(keyword)
        return any(w.startswith(stem) for w in text_words)
    return keyword in text_words


def guess_category(comment: str, op_type: OpType, categories: Iterable[Category]) -> str | None:
    """Возвращает название категории с самым длинным совпавшим ключевым словом."""
    text_norm = normalize(comment)
    text_words = words(comment)
    if not text_words:
        return None
    best: tuple[int, str] | None = None
    for cat in categories:
        if cat.type != op_type:
            continue
        for kw in [cat.name, *cat.keywords]:
            kw = normalize(kw.strip())
            if kw and _keyword_matches(kw, text_norm, text_words):
                if best is None or len(kw) > best[0]:
                    best = (len(kw), cat.name)
    return best[1] if best else None


def subject(comment: str) -> str:
    """Что за трата — часть до первой запятой. После запятой — свободный комментарий
    («здоровье, сходил к врачу по поводу спины»): он не угадывает категорию и не запоминается."""
    return comment.split(",", 1)[0]


def learnable_keyword(comment: str) -> str | None:
    """Слово, которое стоит запомнить как ключевое для выбранной категории: только если до запятой
    ровно одно слово («хинкали», «хинкали, в грузинском»), иначе в ключевые слова попадет мусор."""
    ws = words(subject(comment))
    if len(ws) == 1 and len(ws[0]) >= 3 and not ws[0].isdigit():
        return ws[0]
    return None


def find_category(query: str, categories: Iterable[Category]) -> list[Category]:
    q = normalize(query.strip())
    cats = list(categories)
    exact = [c for c in cats if normalize(c.name) == q]
    if exact:
        return exact
    return [c for c in cats if q and q in normalize(c.name)]
