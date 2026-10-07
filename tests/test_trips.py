import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import asyncio
import datetime
from datetime import date
from decimal import Decimal

import pytest

from bot import trips
from bot.models import Operation, OpType, Trip, trip_tag
from bot.service import FinanceService
from bot.storage import MemoryStorage

E = OpType.EXPENSE
TODAY = date(2026, 10, 7)


def run(c):
    return asyncio.run(c)


@pytest.mark.parametrize("args,expected", [
    ("Казань 13.10–16.10", ("Казань", date(2026, 10, 13), date(2026, 10, 16))),
    ("нижний новгород 13.10-16.10", ("Нижний новгород", date(2026, 10, 13), date(2026, 10, 16))),
    ("Казань 13.10 16.10", ("Казань", date(2026, 10, 13), date(2026, 10, 16))),
    ("Казань 13.10", ("Казань", date(2026, 10, 13), date(2026, 10, 13))),
    ("Сочи 30.12–02.01", ("Сочи", date(2026, 12, 30), date(2027, 1, 2))),
    ("Пермь 3.06.2026–5.06.2026", ("Пермь", date(2026, 6, 3), date(2026, 6, 5))),
    ("Пермь 20.09–22.09", ("Пермь", date(2026, 9, 20), date(2026, 9, 22))),   # недавно прошедшая
])
def test_parse_trip_args(args, expected):
    assert trips.parse_trip_args(args, TODAY) == expected


@pytest.mark.parametrize("args", ["Казань", "13.10–16.10", "Казань 31.02", "Казань 1.10 2.10 3.10"])
def test_parse_trip_args_errors(args):
    with pytest.raises(trips.TripError):
        trips.parse_trip_args(args, TODAY)


def test_trip_flow():
    svc = FinanceService(MemoryStorage(operations=[Operation(date(2026, 10, 14), E, Decimal(500), "Еда вне дома")]),
                         today_fn=lambda: TODAY)
    text = run(svc.trip_command("Казань 13.10–16.10"))
    assert "Казань</b>, 13–16.10 · 4 дн" in text and "помечены: 1" in text
    # билеты заранее: трата сегодня, вне дат поездки — кнопка «В Казань»
    tickets = run(svc.prepare("12000 билеты ржд")).op
    assert not trip_tag(tickets)
    run(svc.save(tickets))
    trip, marked = run(svc.trip_for_button(tickets))
    assert trip.city == "Казань" and not marked
    tickets = run(svc.toggle_trip(tickets.id, True))
    assert trip_tag(tickets) == "поездка-казань-131026" and "✈️ Казань" in svc.describe(tickets)
    assert tickets.kind.value == "командировка"
    # трата в даты поездки помечается сама, кнопкой можно снять
    svc._today_fn = lambda: date(2026, 10, 14)      # уже в поездке
    hotel = run(svc.prepare("9000 отель")).op
    assert trip_tag(hotel)
    run(svc.save(hotel))
    assert run(svc.trip_for_button(hotel))[1] is True
    assert hotel.kind.value == "командировка"
    untagged = run(svc.toggle_trip(hotel.id, False))
    assert trip_tag(untagged) is None and untagged.kind.value == "обычная"
    status = run(svc.trip_command(None))
    assert "идет сейчас" in status and "12" in status and "(2 трат)" in status
    assert "Такая поездка уже есть" in run(svc.trip_command("казань 13.10-16.10"))
    assert "отменена" in run(svc.trip_command("cancel"))
    assert all(not trip_tag(o) and o.kind.value == "обычная" for o in run(svc.storage.list_operations()))
    assert "Поездок впереди нет" in run(svc.trip_command(None))


def test_format_trips_usual_and_last():
    t1 = Trip("Пермь", date(2026, 6, 3), date(2026, 6, 5))
    t2 = Trip("Самара", date(2026, 6, 20), date(2026, 6, 21))
    t3 = Trip("Казань", date(2026, 9, 13), date(2026, 9, 16))
    t4 = Trip("Сочи", date(2026, 11, 1), date(2026, 11, 3))
    ops = [Operation(date(2026, 6, 4), E, Decimal(15000), "Еда вне дома", tags=[t1.tag]),
           Operation(date(2026, 6, 20), E, Decimal(10000), "Транспорт/РЖД", tags=[t2.tag]),
           Operation(date(2026, 9, 14), E, Decimal(33643), "Еда вне дома", tags=[t3.tag])]
    text = trips.format_trips([t1, t2, t3, t4], ops, TODAY)
    lines = text.splitlines()
    assert lines[1] == "<b>Сочи</b> 1–3.11 · 3 дн · 0 · впереди"
    assert lines[2] == "<b>Казань</b> 13–16.09 · 4 дн · 33 643"
    assert lines[3] == "   🍽 Кафешки 33 643"
    assert "Обычно поездка 19 548, в день 6 516" in text
    assert "Последняя (Казань): в день 8 411 — ⬆️ +68% к обычной" in text
    assert "Командировок пока нет" in trips.format_trips([], [], TODAY)


def test_trip_buttons_in_chat():
    from test_handlers import Harness, buttons, texts
    h = Harness(MemoryStorage())
    h.svc._today_fn = lambda: TODAY
    assert "13–16.10" in texts(h.text("/trip Казань 13.10–16.10"))[0]
    out = h.text("12000 билеты ржд")
    kb = buttons(out)
    assert "✈️ В Казань" in kb
    out = h.click(kb["✈️ В Казань"])
    assert "✈️ Казань" in texts(out)[0] and "✖️ Не командировка" in buttons(out)
    trips_text = texts(h.text("/командировки"))[0]
    assert "<b>Казань</b> 13–16.10" in trips_text and "🚆 Билеты 12" in trips_text
    # статью можно сменить кнопкой
    kb = buttons(h.text("13000 билеты ржд"))
    out = h.click(kb["✈️ В Казань"])
    kb = buttons(out)
    assert "🧳 Статья: Билеты" in kb
    out = h.click(kb["🧳 Статья: Билеты"])
    out = h.click(buttons(out)["🏨 Жилье"])
    assert "🏨 Жилье" in texts(out)[0]



@pytest.mark.parametrize("comment,category,expected", [
    ("отель Казань", "Досуг", "Жилье"),
    ("посуточно квартира", "Прочее", "Жилье"),
    ("билеты ржд", "Транспорт/РЖД", "Билеты"),
    ("самолет", "Транспорт/РЖД", "Билеты"),
    ("такси до вокзала", "Транспорт/РЖД", "Транспорт"),
    ("обед", "Еда вне дома", "Кафешки"),
    ("сувенир", "Подарки", "Прочее"),
    ("метро", "Прочее", "Транспорт"),
    ("ЖД билеты [командировка-130726 ЖД билеты]", "Транспорт/РЖД", "Билеты"),
    ("Жилье [командировка-060926 Жилье]", "Прочее", "Жилье"),
])
def test_guess_trip_item(comment, category, expected):
    from bot.models import guess_trip_item
    assert guess_trip_item(comment, category) == expected


def test_trip_item_on_input_and_kind_changes():
    svc = FinanceService(MemoryStorage(), today_fn=lambda: date(2026, 10, 14))
    run(svc.trip_command("Казань 13.10–16.10"))
    hotel = run(svc.prepare("9000 отель"))
    assert hotel.op.trip_item == "Жилье" and "🏨 Жилье" in svc.describe(hotel.op)
    run(svc.save(hotel.op))
    changed = run(svc.set_trip_item(hotel.op.id, "Прочее"))
    assert changed.trip_item == "Прочее"
    regular = run(svc.toggle_trip(hotel.op.id, False))
    assert regular.trip_item == "" and regular.kind.value == "обычная"
    back = run(svc.toggle_trip(hotel.op.id, True))
    assert back.trip_item == "Жилье"                   # заново по словам
