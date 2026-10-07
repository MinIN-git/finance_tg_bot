"""Картинка для /stats.

Сверху — сетка плиток по категориям обычных трат: название, сумма за последний месяц, спарклайн по
месяцам периода, светлая полоса «обычно» (min–max прошлых месяцев) и ▲/▼ по правилу «обычно».
Ниже — потрачено по месяцам стопкой из трех потоков (обычные, тяжелые, командировки) и доход пунктиром,
затем капитал по снимкам. Одна ось Y на панель, без второй оси.
"""
from __future__ import annotations

import io
from datetime import date
from typing import TYPE_CHECKING, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .textutil import MONTHS_SHORT  # noqa: E402

if TYPE_CHECKING:
    from .reports import Stats

SURFACE = "#fcfcfb"
TILE = "#f3f2ee"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
BAND = "#e1e0d9"
REGULAR = "#2a78d6"    # слот 1 (синий)
HEAVY = "#eb6834"      # слот 2 (оранжевый)
TRIPS = "#1baf7a"      # слот 3 (бирюзовый)
UP = "#c2410c"
DOWN = "#008300"
COLS = 3


def _short(v: float, _pos=None) -> str:
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн".replace(".", ",")
    if abs(v) >= 1000:
        return f"{v / 1000:.0f} тыс"
    return f"{v:.0f}"


def _labels(months: Sequence[date]) -> list[str]:
    return [MONTHS_SHORT[m.month - 1] for m in months]


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


def _tile(ax, tile, x: list[int]) -> None:
    ax.set_facecolor(TILE)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])
    vals = [float(v) for v in tile.values]
    top = max(vals + [float(tile.band[1]) if tile.band else 0, 1.0])
    if tile.band is not None:
        ax.axhspan(float(tile.band[0]), float(tile.band[1]), color=BAND, alpha=0.9, lw=0, zorder=0)
    ax.plot(x, vals, color=REGULAR, linewidth=2, zorder=2)
    ax.plot([x[-1]], [vals[-1]], "o", color=REGULAR, markersize=5, markeredgecolor=TILE, markeredgewidth=1.5, zorder=3)
    ax.set_ylim(-top * 0.08, top * 1.9)
    ax.set_xlim(-0.3, len(x) - 0.7 if len(x) > 1 else 0.3)
    ax.text(0.05, 0.9, tile.name, transform=ax.transAxes, fontsize=8.5, color=INK_SECONDARY, va="top")
    ax.text(0.05, 0.66, _short(vals[-1]), transform=ax.transAxes, fontsize=11, color=INK, fontweight="bold", va="top")
    if tile.marker:
        ax.text(0.95, 0.66, tile.marker, transform=ax.transAxes, fontsize=11, ha="right", va="top",
                color=UP if tile.marker == "▲" else DOWN)


def stats_chart(st: "Stats", title: str) -> bytes:
    x = list(range(len(st.months)))
    labels = _labels(st.months)
    tile_rows = (len(st.tiles) + COLS - 1) // COLS
    has_capital = any(c is not None for c in st.capital)
    heights = [0.62] * tile_rows + [1.25] + ([0.95] if has_capital else [])
    fig = plt.figure(figsize=(9, 0.9 + 2.0 * sum(heights)), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    gs = GridSpec(len(heights), COLS, figure=fig, height_ratios=heights, hspace=0.55, wspace=0.12)

    for k, tile in enumerate(st.tiles):
        _tile(fig.add_subplot(gs[k // COLS, k % COLS]), tile, x)

    row = tile_rows
    ax = fig.add_subplot(gs[row, :])
    _style(ax)
    regular = [float(s.expense) for s in st.regular]
    heavy = [float(v) for v in st.heavy]
    trips = [float(v) for v in st.trips]
    ax.bar(x, regular, width=0.6, color=REGULAR, label="Обычные")
    ax.bar(x, heavy, width=0.6, bottom=regular, color=HEAVY, label="Тяжелые", edgecolor=SURFACE, linewidth=1)
    ax.bar(x, trips, width=0.6, bottom=[a + b for a, b in zip(regular, heavy)], color=TRIPS, label="Командировки",
           edgecolor=SURFACE, linewidth=1)
    income = [float(v) for v in st.income]
    ax.plot(x, income, color=INK_SECONDARY, linewidth=1.6, linestyle=(0, (4, 3)), marker="o", markersize=4,
            label="Доход")
    totals = [a + b + c for a, b, c in zip(regular, heavy, trips)]
    ax.set_ylim(0, max(totals + income + [1]) * 1.15)
    ax.set_xlim(-0.5, len(x) - 0.5)
    ax.set_xticks(x, labels)
    ax.set_title("Потрачено по месяцам", loc="left", color=INK_SECONDARY, fontsize=10)
    leg = ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), frameon=False, fontsize=8, ncol=4,
                    borderaxespad=0.2, handlelength=1.4)
    for t in leg.get_texts():
        t.set_color(INK_SECONDARY)

    if has_capital:
        ax = fig.add_subplot(gs[row + 1, :])
        _style(ax)
        pts = [(i, float(c)) for i, c in zip(x, st.capital) if c is not None]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=REGULAR, linewidth=2, marker="o", markersize=6,
                markeredgecolor=SURFACE, markeredgewidth=2)
        ax.annotate(_short(pts[-1][1]), pts[-1], xytext=(0, 8), textcoords="offset points", ha="center",
                    color=INK, fontsize=9)
        ax.set_ylim(0, max(p[1] for p in pts) * 1.25)
        ax.set_xlim(-0.5, len(x) - 0.5)
        ax.set_xticks(x, labels)
        ax.set_title("Капитал на конец месяца", loc="left", color=INK_SECONDARY, fontsize=10)

    fig.suptitle(title, x=0.02, ha="left", color=INK, fontsize=12, fontweight="bold")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    return buf.getvalue()
