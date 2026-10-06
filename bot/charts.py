"""Картинка для /stats: отдельная панель на каждый показатель, общая ось месяцев, без второй оси Y.

Сверху капитал на конец месяца (линия), ниже доход и расход парными столбиками, затем по маленькой
панели на каждую крупную категорию — все суммы без тяжелых трат. Цвета — первые слоты проверенной
категориальной палитры, текст и сетка — нейтральные.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .textutil import MONTHS_SHORT  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
INCOME = "#2a78d6"     # слот 1 (синий)
EXPENSE = "#eb6834"    # слот 2 (оранжевый)
CAPITAL = "#2a78d6"
CATEGORY = "#eb6834"


def _short(v: float, _pos=None) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн".replace(".", ",")
    if abs(v) >= 1000:
        return f"{v / 1000:.0f} тыс"
    return f"{v:.0f}"


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=0)
    ax.yaxis.set_major_formatter(FuncFormatter(_short))
    ax.yaxis.set_major_locator(MaxNLocator(4))


def _labels(months: Sequence[date]) -> list[str]:
    return [MONTHS_SHORT[m.month - 1] for m in months]


def stats_chart(
    months: Sequence[date],
    income: Sequence[Decimal],
    expense: Sequence[Decimal],
    capital: Sequence[Decimal | None],
    categories: Mapping[str, Sequence[Decimal]],
    title: str,
) -> bytes:
    x = list(range(len(months)))
    labels = _labels(months)
    has_capital = any(c is not None for c in capital)
    cats = list(categories.items())
    cat_rows = (len(cats) + 2) // 3
    rows = (1 if has_capital else 0) + 1 + cat_rows
    heights = ([1.0] if has_capital else []) + [1.2] + [0.8] * cat_rows
    fig = plt.figure(figsize=(9, 1.0 + 2.1 * sum(heights)), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    gs = GridSpec(rows, 3, figure=fig, height_ratios=heights, hspace=0.75, wspace=0.28)
    row = 0

    if has_capital:
        ax = fig.add_subplot(gs[row, :])
        _style(ax)
        pts = [(i, float(c)) for i, c in zip(x, capital) if c is not None]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=CAPITAL, linewidth=2,
                marker="o", markersize=6, markeredgecolor=SURFACE, markeredgewidth=2)
        last_i, last_v = pts[-1]
        ax.annotate(_short(last_v), (last_i, last_v), xytext=(0, 9), textcoords="offset points",
                    ha="center", color=INK, fontsize=9)
        ax.set_ylim(bottom=0, top=max(p[1] for p in pts) * 1.2)
        ax.set_xlim(-0.5, len(x) - 0.5)
        ax.set_xticks(x, labels)
        ax.set_title("Капитал на конец месяца", loc="left", color=INK_SECONDARY, fontsize=10)
        row += 1

    ax = fig.add_subplot(gs[row, :])
    _style(ax)
    w = 0.36
    ax.bar([i - w / 2 - 0.01 for i in x], [float(v) for v in income], width=w, color=INCOME, label="Доход")
    ax.bar([i + w / 2 + 0.01 for i in x], [float(v) for v in expense], width=w, color=EXPENSE, label="Расход")
    ax.set_ylim(0, max([float(v) for v in [*income, *expense]] + [1]) * 1.1)
    ax.set_xlim(-0.5, len(x) - 0.5)
    ax.set_xticks(x, labels)
    ax.set_title("Доход и расход", loc="left", color=INK_SECONDARY, fontsize=10)
    leg = ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), frameon=False, fontsize=8.5, ncol=2,
                    borderaxespad=0.2, handlelength=1.2)
    for t in leg.get_texts():
        t.set_color(INK_SECONDARY)
    row += 1

    for k, (name, values) in enumerate(cats):
        ax = fig.add_subplot(gs[row + k // 3, k % 3])
        _style(ax)
        vals = [float(v) for v in values]
        ax.bar(x, vals, width=0.6, color=CATEGORY)
        ax.set_ylim(0, max(vals + [1]) * 1.25)
        ax.set_xlim(-0.5, len(x) - 0.5)
        ax.set_xticks(x, labels if len(x) <= 6 else [lb if i % 2 == len(x) % 2 else "" for i, lb in enumerate(labels)])
        ax.set_title(name, loc="left", color=INK_SECONDARY, fontsize=9)

    fig.suptitle(title, x=0.02, ha="left", color=INK, fontsize=12, fontweight="bold")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    return buf.getvalue()
