"""Командировки: разбор /trip, траты поездки, тексты /trip и /trips. Чистые функции."""
from __future__ import annotations

import html
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from .models import Operation, OpType, Trip
from .models import TRIP_ITEM_ICONS
from .reports import deviation, trip_items_totals
from .textutil import fmt_money, fmt_trip_range

ZERO = Decimal(0)
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?$")
RANGE_SPLIT = re.compile(r"\s*(?:–|—|-|\.\.)\s*")
PAST_TOLERANCE = timedelta(days=60)   # дата без года раньше, чем 60 дней назад, — это уже следующий год


class TripError(ValueError):
    pass


def _parse_day(token: str, today: date, after: date | None = None) -> date | None:
    m = DATE_RE.match(token)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        d = date(today.year, month, day)
    except ValueError:
        return None
    base = after or today - PAST_TOLERANCE
    if d < base:
        try:
            d = date(d.year + 1, month, day)
        except ValueError:
            return None
    return d


def parse_trip_args(args: str, today: date) -> tuple[str, date, date]:
    """«Казань 13.10–16.10», «Нижний Новгород 13.10-16.10», «Казань 13.10 16.10», «Казань 13.10»."""
    text = RANGE_SPLIT.sub(" ", args.strip())
    tokens = text.split()
    dates = [t for t in tokens if DATE_RE.match(t)]
    city = " ".join(t for t in tokens if not DATE_RE.match(t)).strip()
    if not city or not dates or len(dates) > 2:
        raise TripError("Пример: <code>/trip Казань 13.10–16.10</code>")
    start = _parse_day(dates[0], today)
    end = _parse_day(dates[-1], today, after=start) if start else None
    if start is None or end is None:
        raise TripError("Не понял даты. Пример: <code>/trip Казань 13.10–16.10</code>")
    if (end - start).days > 90:
        raise TripError("Поездка длиннее 90 дней — проверьте даты.")
    return city[:1].upper() + city[1:], start, end


def fmt_range(t: Trip) -> str:
    return fmt_trip_range(t.start, t.end)


def trip_ops(trip: Trip, ops: Sequence[Operation]) -> list[Operation]:
    return [op for op in ops if op.type == OpType.EXPENSE and trip.tag in op.tags]


def trip_total(trip: Trip, ops: Sequence[Operation]) -> Decimal:
    return sum((op.amount for op in trip_ops(trip, ops)), ZERO)


def nearest_active(trips: Sequence[Trip], today: date) -> Trip | None:
    """Идущая сейчас поездка, иначе ближайшая будущая."""
    active = [t for t in trips if t.end >= today]
    return min(active, key=lambda t: t.start) if active else None


def covering(trips: Sequence[Trip], d: date) -> Trip | None:
    return next((t for t in sorted(trips, key=lambda t: t.start) if t.covers(d)), None)


def format_trip_status(trip: Trip | None, ops: Sequence[Operation], today: date, cur: str = "") -> str:
    if trip is None:
        return ("Поездок впереди нет. Добавить: <code>/trip Казань 13.10–16.10</code>\n"
                "Траты в эти даты пометятся сами, а билеты, купленные заранее, — кнопкой «✈️ В Казань» под тратой.")
    when = "идет сейчас" if trip.start <= today else f"через {(trip.start - today).days} дн"
    spent = trip_ops(trip, ops)
    lines = [f"✈️ <b>{html.escape(trip.city)}</b>, {fmt_range(trip)} · {trip.days} дн · {when}"]
    lines.append(f"Уже записано: {fmt_money(sum((o.amount for o in spent), ZERO), cur)} ({len(spent)} трат)" if spent
                 else "Трат пока нет.")
    lines.append("Траты в эти даты помечаются сами; заранее купленное — кнопкой «✈️ В …» под тратой. "
                 "Отменить поездку: /trip cancel")
    return "\n".join(lines)


def format_trips(trips: Sequence[Trip], ops: Sequence[Operation], today: date, cur: str = "", limit: int = 12) -> str:
    if not trips:
        return "🧳 Командировок пока нет. Добавить: <code>/trip Казань 13.10–16.10</code>"
    ordered = sorted(trips, key=lambda t: t.start, reverse=True)
    lines = ["🧳 <b>Командировки</b>"]
    for t in ordered[:limit]:
        tail = " · впереди" if t.start > today else (" · идет" if t.end >= today else "")
        lines.append(f"<b>{html.escape(t.city)}</b> {fmt_range(t)} · {t.days} дн · {fmt_money(trip_total(t, ops), cur)}{tail}")
        items = trip_items_totals(trip_ops(t, ops))
        if items:
            lines.append("   " + " · ".join(f"{TRIP_ITEM_ICONS[n]} {n} {fmt_money(a, cur)}" for n, a in items))
    done = [t for t in ordered if t.end < today and trip_total(t, ops) > 0]
    if done:
        totals = [trip_total(t, ops) for t in done]
        per_day = sum(totals, ZERO) / sum(t.days for t in done)
        avg = sum(totals, ZERO) / len(done)
        lines += ["", f"Обычно поездка {fmt_money(avg, cur)}, в день {fmt_money(per_day, cur)}"]
        last, others = done[0], done[1:]
        if others:
            usual_day = sum((trip_total(t, ops) for t in others), ZERO) / sum(t.days for t in others)
            last_day = trip_total(last, ops) / last.days
            mark = deviation(last_day, usual_day)
            verdict = f"{mark} к обычной" if mark else "как обычно"
            lines.append(f"Последняя ({html.escape(last.city)}): в день {fmt_money(last_day, cur)} — {verdict}")
    return "\n".join(lines)
