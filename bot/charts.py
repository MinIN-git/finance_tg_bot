"""Картинка для квартального отчета: капитал по месяцам и доход/расход столбиками.

Две панели с общей осью месяцев (без второй оси Y): сверху капитал (линия), снизу доход и
расход парными столбиками. Цвета — первые два слота проверенной категориальной палитры.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

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


def _thousands(v: float, _pos=None) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн".replace(".", ",")
    if abs(v) >= 1000:
        return f"{v / 1000:.0f} тыс"
    return f"{v:.0f}"


def quarter_chart(
    months: Sequence[date],
    income: Sequence[Decimal],
    expense: Sequence[Decimal],
    capital: Sequence[Decimal | None],
    title: str,
) -> bytes:
    labels = [f"{MONTHS_SHORT[m.month - 1]}\n{m.year}" if m.month == 1 or i == 0 else MONTHS_SHORT[m.month - 1]
              for i, m in enumerate(months)]
    x = list(range(len(months)))
    has_capital = any(c is not None for c in capital)
    fig, axes = plt.subplots(
        2 if has_capital else 1, 1, figsize=(9, 6.4 if has_capital else 3.8), dpi=150, sharex=True,
        gridspec_kw={"height_ratios": [1, 1.2]} if has_capital else None, squeeze=False,
    )
    fig.patch.set_facecolor(SURFACE)
    axes = [a[0] for a in axes]

    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(BASELINE)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)
        ax.yaxis.set_major_formatter(FuncFormatter(_thousands))

    if has_capital:
        ax = axes[0]
        pts = [(i, float(c)) for i, c in zip(x, capital) if c is not None]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=CAPITAL, linewidth=2,
                marker="o", markersize=5, markeredgecolor=SURFACE, markeredgewidth=2)
        last_i, last_v = pts[-1]
        ax.annotate(_thousands(last_v), (last_i, last_v), xytext=(0, 9), textcoords="offset points",
                    ha="center", color=INK, fontsize=9)
        ax.set_title("Капитал на конец месяца", loc="left", color=INK_SECONDARY, fontsize=10)
        lo = min(p[1] for p in pts)
        ax.set_ylim(bottom=min(0, lo))

    ax = axes[-1]
    w = 0.36
    ax.bar([i - w / 2 - 0.01 for i in x], [float(v) for v in income], width=w, color=INCOME, label="Доход")
    ax.bar([i + w / 2 + 0.01 for i in x], [float(v) for v in expense], width=w, color=EXPENSE, label="Расход")
    ax.set_title("Доход и расход по месяцам", loc="left", color=INK_SECONDARY, fontsize=10)
    top = max([float(v) for v in [*income, *expense]] + [1])
    ax.set_ylim(0, top * 1.08)
    leg = ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), frameon=False, fontsize=9, ncol=2,
                    borderaxespad=0.2, handlelength=1.2)
    for t in leg.get_texts():
        t.set_color(INK_SECONDARY)
    ax.set_xticks(x, labels)

    # последние три месяца — сам квартал
    for a in axes:
        a.axvspan(len(x) - 3.5, len(x) - 0.5, color=GRID, alpha=0.35, linewidth=0, zorder=0)
    fig.suptitle(title, x=0.02, ha="left", color=INK, fontsize=12, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
