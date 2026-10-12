from bot.categories import DEFAULT_CATEGORIES, find_category, guess_category, learnable_keyword
from bot.models import Category, OpType

E, I = OpType.EXPENSE, OpType.INCOME


def g(text, t=E):
    return guess_category(text, t, DEFAULT_CATEGORIES)


def test_guess_expense():
    assert g("кофе") == "Еда вне дома"
    assert g("Кофейня у дома") == "Еда вне дома"
    assert g("Пятёрочка") == "Продукты домой"
    assert g("такси до работы") == "Транспорт/РЖД"
    assert g("отпуск отель") == "Досуг"
    assert g("ипотека") == "Ипотека"
    assert g("яндекс маркет") == "Маркетплейсы"
    assert g("яндекс плюс") == "Подписки"
    assert g("мобильная связь") == "Подписки"
    assert g("интернет дома") == "ЖКУ"


def test_guess_income_uses_income_categories():
    assert g("зп", I) == "Зарплата"
    assert g("от папы", I) == "Папа"
    assert g("подарок", I) == "Подарки"
    assert g("подарок", E) == "Подарки"


def test_short_keyword_needs_exact_word():
    assert g("бар") == "Досуг"
    assert g("барбершоп") is None


def test_unknown():
    assert g("что-то странное") is None
    assert g("") is None


def test_longest_keyword_wins():
    cats = [Category("A", E, ["кофе"]), Category("B", E, ["кофе зерно"])]
    assert guess_category("кофе зерно 1кг", E, cats) == "B"


def test_learnable_keyword():
    assert learnable_keyword("Шаурма") == "шаурма"
    assert learnable_keyword("Шаурма, у метро") == "шаурма"
    assert learnable_keyword("Шаурма у метро") is None     # фраза без запятой — не учим
    assert learnable_keyword("2 шт") is None
    assert learnable_keyword("") is None


def test_find_category():
    assert [c.name for c in find_category("продукты", DEFAULT_CATEGORIES)] == ["Продукты домой"]
    assert {c.name for c in find_category("подарки", DEFAULT_CATEGORIES)} == {"Подарки"}
    assert find_category("нет такой", DEFAULT_CATEGORIES) == []
