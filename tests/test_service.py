import asyncio
from datetime import date
from decimal import Decimal

import pytest

from bot.models import BalanceEntry, Operation, OpType
from bot.service import FinanceService, parse_plain_amount
from bot.storage import MemoryStorage, op_to_row, row_to_op, cell_to_date

TODAY = date(2026, 10, 4)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def svc():
    return FinanceService(MemoryStorage(), today_fn=lambda: TODAY)


def test_prepare_guesses_category_and_default_account(svc):
    p = run(svc.prepare("350 кофе"))
    assert not p.needs_category
    assert p.op.category == "Еда вне дома"
    assert p.op.account == "Карта"


def test_unknown_category_and_learning(svc):
    p = run(svc.prepare("500 хинкали"))
    assert p.needs_category
    p.op.category = "Еда вне дома"
    run(svc.save(p.op))
    assert run(svc.learn(p.op)) == "хинкали"
    assert run(svc.prepare("600 хинкали")).op.category == "Еда вне дома"
    # уже угадывается — второй раз не учим
    assert run(svc.learn(run(svc.prepare("600 хинкали")).op)) is None


def test_relearning_moves_keyword(svc):
    op = run(svc.save(run(svc.prepare("500 хинкали")).op))
    op.category = "Досуг"
    run(svc.learn(op))
    updated = run(svc.set_category(op.id, "Еда вне дома"))
    assert run(svc.learn(updated)) == "хинкали"
    cats = {c.name: c for c in run(svc.storage.list_categories()) if c.type == OpType.EXPENSE}
    assert "хинкали" in cats["Еда вне дома"].keywords
    assert "хинкали" not in cats["Досуг"].keywords
    assert run(svc.prepare("хинкали 400")).op.category == "Еда вне дома"


def test_undo_last_and_delete(svc):
    for text in ("100 кофе", "200 такси", "300 аптека"):
        run(svc.save(run(svc.prepare(text)).op))
    last = run(svc.last(10))
    assert [o.amount for o in last] == [300, 200, 100]
    assert run(svc.undo()).amount == 300
    assert run(svc.delete(last[2].id)).amount == 100
    assert [o.amount for o in run(svc.last())] == [200]


def test_set_category(svc):
    op = run(svc.save(run(svc.prepare("350 кофе")).op))
    updated = run(svc.set_category(op.id, "Досуг"))
    assert updated.category == "Досуг"
    assert run(svc.get_operation(op.id)).category == "Досуг"


def test_pending(svc):
    op = run(svc.prepare("500 хинкали")).op
    pid = svc.stash(op)
    assert svc.pop_pending(pid) is op
    assert svc.pop_pending(pid) is None


def test_month_text(svc):
    run(svc.save(run(svc.prepare("+100000 зп")).op))
    run(svc.save(run(svc.prepare("вчера 350 кофе")).op))
    text = run(svc.month_text())
    assert "<b>Октябрь</b>, 4-й день" in text
    assert "Еда вне дома" in text


def test_category_text(svc):
    run(svc.save(run(svc.prepare("350 кофе")).op))
    assert "350" in run(svc.category_text("еда"))
    assert "не найдена" in run(svc.category_text("абырвалг"))


def test_snapshot_flow():
    storage = MemoryStorage(balances=[
        BalanceEntry(date(2026, 9, 1), "Карта", Decimal(3300)),
        BalanceEntry(date(2026, 9, 1), "Вклад", Decimal(8450)),
        BalanceEntry(date(2026, 9, 1), "Наличные", Decimal(500)),
    ])
    svc = FinanceService(storage, today_fn=lambda: date(2026, 10, 1))
    q = run(svc.start_snapshot(1))
    assert (q.account.name, q.account.prompt, q.position, q.total) == ("Карта", "Сколько на карте?", 0, 3)
    assert q.previous.amount == 3300
    active, q = run(svc.answer_snapshot(1, Decimal(3100)))
    assert active and q.account.name == "Вклад"
    active, q = run(svc.answer_snapshot(1, Decimal(9300)))
    assert q.account.prompt == "Сколько наличными?"
    active, q = run(svc.answer_snapshot(1, None))       # «как в прошлый раз»
    assert active and q is None
    assert svc.snapshot_question(1) is None
    oct_ = {e.account: e.amount for e in run(storage.list_balances()) if e.date == date(2026, 10, 1)}
    assert oct_ == {"Карта": 3100, "Вклад": 9300, "Наличные": 500}
    text = run(svc.capital_text())
    assert "к 1 сентября" in text
    assert run(svc.answer_snapshot(1, Decimal(1))) == (False, None)


def test_snapshot_skip_without_previous_and_cancel(svc):
    q = run(svc.start_snapshot(1))
    assert q.previous is None
    _, q = run(svc.answer_snapshot(1, None))            # пропуск без прошлого значения — ничего не пишем
    assert run(svc.storage.list_balances()) == []
    assert svc.cancel_snapshot(1)
    assert svc.snapshot_question(1) is None


def test_snapshot_repeat_same_day_replaces(svc):
    run(svc.start_snapshot(1))
    run(svc.answer_snapshot(1, Decimal(100)))
    run(svc.start_snapshot(1))
    q = svc.snapshot_question(1)
    assert q.previous is None                           # сегодняшняя запись не считается «прошлой»
    run(svc.answer_snapshot(1, Decimal(200)))
    assert [(e.account, e.amount) for e in run(svc.storage.list_balances())] == [("Карта", 200)]


@pytest.mark.parametrize(
    "text,expected",
    [("512000", 512000), ("512 000", 512000), ("512000,50", Decimal("512000.50")), ("512к", 512000), ("100р", 100)],
)
def test_parse_plain_amount(text, expected):
    assert parse_plain_amount(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["кофе", "350 кофе", "inf", "-5"])
def test_parse_plain_amount_invalid(text):
    assert parse_plain_amount(text) is None


def test_row_roundtrip():
    op = Operation(date=TODAY, type=OpType.EXPENSE, amount=Decimal("99.9"), category="Продукты домой",
                   account="Карта", comment="молоко", tags=["командировка"])
    row = op_to_row(op)
    assert row[0] == "2026-10-04" and row[2] == 99.9 and row[7] == "#командировка"
    assert row[6] == "'молоко"                          # текст защищен апострофом
    sheet_row = [c[1:] if isinstance(c, str) and c.startswith("'") else c for c in row]  # так вернет Sheets
    assert row_to_op(sheet_row) == op


def test_row_from_sheet_serial_date():
    row = [46299, "Доход", 150000, "Зарплата", "Карта", "", "зп", "", "abc"]
    op = row_to_op(row)
    assert op.date == cell_to_date(46299)
    assert op.type == OpType.INCOME
    assert row_to_op(["", "расход", 1]) is None
    assert row_to_op(["2026-10-01", "непонятно", 1]) is None


@pytest.fixture
def svc_vuz():
    from bot.categories import DEFAULT_CATEGORIES
    import copy
    cats = copy.deepcopy(DEFAULT_CATEGORIES)
    for c in cats:
        if c.name == "Прочее" and c.type == OpType.INCOME:
            c.keywords += ["стипендия", "вуз"]
    return FinanceService(MemoryStorage(categories=cats), today_fn=lambda: TODAY)


def test_no_sign_income_keyword_becomes_income(svc_vuz):
    p = run(svc_vuz.prepare("02.10 вуз 3104"))
    assert p.op.type == OpType.INCOME and p.op.category == "Прочее"
    assert not p.needs_category and p.type_choices is None
    assert "Доход" in svc_vuz.describe(p.op)


def test_explicit_minus_keeps_expense(svc_vuz):
    p = run(svc_vuz.prepare("-3104 вуз"))
    assert p.op.type == OpType.EXPENSE and p.needs_category


def test_explicit_plus_income(svc_vuz):
    p = run(svc_vuz.prepare("+3104 вуз"))
    assert p.op.type == OpType.INCOME and p.op.category == "Прочее"


def test_ambiguous_word_asks_type(svc_vuz):
    p = run(svc_vuz.prepare("5000 подарок"))   # «Подарки» есть и в доходах, и в расходах
    assert p.type_choices == {OpType.INCOME: "Подарки", OpType.EXPENSE: "Подарки"}


def test_expense_keyword_stays_expense(svc_vuz):
    p = run(svc_vuz.prepare("350 кофе"))
    assert p.op.type == OpType.EXPENSE and p.op.category == "Еда вне дома"
