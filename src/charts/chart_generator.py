"""Hisse ve portföy grafikleri için headless PNG üreticisi."""

from __future__ import annotations

import io
import math
from datetime import datetime, timedelta
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from src.storage.models import AnalystConsensus, MarketSnapshot, PortfolioItem

# Koyu modern tema renk paleti (Slate tabanlı)
BG_COLOR = "#0f172a"
CARD_BG = "#1e293b"
TEXT_COLOR = "#e2e8f0"
MUTED_TEXT = "#94a3b8"
GRID_COLOR = "#1e293b"
SPINE_COLOR = "#334155"

PRICE_COLOR = "#38bdf8"
SMA_COLOR = "#f59e0b"
AVG_TARGET_COLOR = "#22c55e"
MIN_TARGET_COLOR = "#86efac"
MAX_TARGET_COLOR = "#4ade80"
USER_COST_COLOR = "#f43f5e"
POSITIVE_COLOR = "#22c55e"
NEGATIVE_COLOR = "#ef4444"
UPSIDE_NEGATIVE_COLOR = "#f59e0b"

ALLOCATION_PALETTE = [
    "#38bdf8",
    "#22c55e",
    "#f59e0b",
    "#a855f7",
    "#ec4899",
    "#14b8a6",
    "#f97316",
    "#6366f1",
    "#84cc16",
    "#06b6d4",
]


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Değeri güvenli şekilde sonlu float'a dönüştürür."""
    if value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().replace(".IS", "")


def _style_axis(ax: plt.Axes, grid_axis: str = "both") -> None:
    """Eksenlere koyu modern tema stilini uygular."""
    ax.set_facecolor(BG_COLOR)
    ax.tick_params(colors=TEXT_COLOR, labelsize=9)
    for spine_name in ("top", "right"):
        ax.spines[spine_name].set_visible(False)
    for spine_name in ("bottom", "left"):
        ax.spines[spine_name].set_color(SPINE_COLOR)
        ax.spines[spine_name].set_linewidth(0.9)
    ax.grid(
        True,
        axis=grid_axis,
        color=GRID_COLOR,
        linestyle="--",
        linewidth=0.8,
        alpha=0.9,
    )
    ax.set_axisbelow(True)


def _fig_to_png_bytes(fig: plt.Figure, dpi: int = 150) -> bytes:
    """Matplotlib figürünü bellek sızıntısı olmadan PNG baytlarına çevirir."""
    buffer = io.BytesIO()
    try:
        fig.savefig(
            buffer,
            format="png",
            dpi=dpi,
            bbox_inches="tight",
            pad_inches=0.25,
            facecolor=fig.get_facecolor(),
            edgecolor="none",
        )
        buffer.seek(0)
        return buffer.getvalue()
    finally:
        plt.close(fig)


def _parse_price_history(
    price_history: Optional[list[tuple[str, float]]],
) -> tuple[list[datetime], list[float]]:
    """(tarih, kapanış) listesini geçerli sıralı datetime ve float listelerine ayırır."""
    if not price_history:
        return [], []

    parsed: list[tuple[datetime, float]] = []
    for entry in price_history:
        if not isinstance(entry, (tuple, list)) or len(entry) < 2:
            continue
        raw_date, raw_price = entry[0], entry[1]
        price = _safe_float(raw_price, default=0.0)
        if price <= 0:
            continue

        date_obj: Optional[datetime] = None
        if isinstance(raw_date, datetime):
            date_obj = raw_date
        else:
            date_str = str(raw_date or "").strip()
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
                try:
                    date_obj = datetime.strptime(date_str[:10], fmt)
                    break
                except ValueError:
                    continue
            if date_obj is None and date_str:
                try:
                    date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if date_obj.tzinfo is not None:
                        date_obj = date_obj.replace(tzinfo=None)
                except ValueError:
                    date_obj = None

        if date_obj is not None:
            parsed.append((date_obj, price))

    if not parsed:
        return [], []

    parsed.sort(key=lambda item: item[0])
    dates = [item[0] for item in parsed]
    closes = [item[1] for item in parsed]
    return dates, closes


def _compute_sma(values: list[float], window: int = 20) -> list[float]:
    """Basit hareketli ortalama (SMA) hesaplar."""
    if len(values) < window or window <= 0:
        return []
    sma: list[float] = []
    running_sum = sum(values[:window])
    sma.append(running_sum / window)
    for idx in range(window, len(values)):
        running_sum += values[idx] - values[idx - window]
        sma.append(running_sum / window)
    return sma


def _resolve_upside_pct(consensus: Optional[AnalystConsensus], current_price: float) -> float:
    """Analist hedef getiri potansiyelini (%) çözer."""
    if consensus is None:
        return 0.0
    explicit = _safe_float(consensus.upside_percent, default=0.0)
    if explicit != 0.0:
        return explicit
    avg_target = _safe_float(consensus.avg_target_price, default=0.0)
    if avg_target > 0 and current_price > 0:
        return round(((avg_target - current_price) / current_price) * 100.0, 2)
    return 0.0


def _apply_y_padding(ax: plt.Axes, values: list[float]) -> None:
    """Çizilen tüm fiyat/hedef seviyeleri için eksene nefes payı ekler."""
    valid = [v for v in values if v > 0 and math.isfinite(v)]
    if not valid:
        ax.set_ylim(0, 100)
        return
    y_min = min(valid)
    y_max = max(valid)
    span = y_max - y_min
    pad = span * 0.12 if span > 0 else max(y_max * 0.1, 1.0)
    lower = max(0.0, y_min - pad)
    upper = y_max + pad
    if upper <= lower:
        upper = lower + 1.0
    ax.set_ylim(lower, upper)


def generate_stock_chart_png(
    symbol: str,
    market: MarketSnapshot,
    consensus: AnalystConsensus,
    portfolio_item: Optional[PortfolioItem] = None,
    price_history: Optional[list[tuple[str, float]]] = None,
) -> bytes:
    """Tek bir hisse için 6 aylık fiyat, SMA20 ve hedef/maliyet çizgili koyu tema PNG üretir."""
    symbol_clean = _normalize_symbol(symbol) or market.symbol or consensus.symbol or "HİSSE"
    dates, closes = _parse_price_history(price_history)

    current_price = _safe_float(market.current_price, default=0.0)
    if current_price <= 0:
        current_price = _safe_float(consensus.current_price, default=0.0)
    if current_price <= 0 and closes:
        current_price = closes[-1]

    upside_pct = _resolve_upside_pct(consensus, current_price)
    avg_target = _safe_float(consensus.avg_target_price, default=0.0)
    min_target = _safe_float(consensus.min_target_price, default=0.0)
    max_target = _safe_float(consensus.max_target_price, default=0.0)

    fig, ax = plt.subplots(figsize=(10, 5.8))
    fig.patch.set_facecolor(BG_COLOR)
    _style_axis(ax, grid_axis="both")

    y_reference_values: list[float] = list(closes)
    if current_price > 0:
        y_reference_values.append(current_price)

    if len(dates) >= 2:
        ax.plot(
            dates,
            closes,
            color=PRICE_COLOR,
            linewidth=2.2,
            label="Kapanış Fiyatı",
            zorder=4,
        )
        ax.fill_between(
            dates,
            closes,
            min(closes),
            color=PRICE_COLOR,
            alpha=0.08,
            zorder=2,
        )
        ax.scatter([dates[-1]], [closes[-1]], color=PRICE_COLOR, s=42, zorder=5)

        if len(closes) >= 20:
            sma20 = _compute_sma(closes, window=20)
            ax.plot(
                dates[19:],
                sma20,
                color=SMA_COLOR,
                linewidth=1.7,
                label="20 Günlük Ort. (SMA20)",
                zorder=4,
            )
            y_reference_values.extend(sma20)

        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%y"))
        fig.autofmt_xdate(rotation=20, ha="right")
        ax.set_xlabel("Tarih", color=TEXT_COLOR, fontsize=10, labelpad=6)
    elif len(dates) == 1:
        ax.scatter(
            dates,
            closes,
            color=PRICE_COLOR,
            s=85,
            label=f"Son Fiyat ({closes[0]:.2f} TL)",
            zorder=5,
        )
        ax.set_xlim(dates[0] - timedelta(days=7), dates[0] + timedelta(days=7))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%y"))
        ax.set_xlabel("Tarih", color=TEXT_COLOR, fontsize=10, labelpad=6)
    else:
        # Tarihsel fiyat yoksa güncel nokta ve hedef bandını temizce çiz
        ax.set_xlim(0.0, 1.0)
        ax.set_xticks([0.5])
        ax.set_xticklabels(["Güncel Görünüm (Tarihsel veri yok)"], color=MUTED_TEXT)
        if current_price > 0:
            ax.scatter(
                [0.5],
                [current_price],
                color=PRICE_COLOR,
                s=100,
                label=f"Güncel Fiyat: {current_price:.2f} TL",
                zorder=5,
            )

    # Analist hedef bandı (Min / Max)
    if min_target > 0 and max_target > 0 and max_target >= min_target:
        ax.axhspan(
            min_target,
            max_target,
            color=AVG_TARGET_COLOR,
            alpha=0.10,
            label=f"Hedef Aralığı: {min_target:.2f} - {max_target:.2f} TL",
            zorder=1,
        )
        ax.axhline(
            max_target,
            color=MAX_TARGET_COLOR,
            linestyle=":",
            linewidth=1.2,
            alpha=0.85,
            label=f"En Yüksek Hedef: {max_target:.2f} TL",
            zorder=3,
        )
        ax.axhline(
            min_target,
            color=MIN_TARGET_COLOR,
            linestyle=":",
            linewidth=1.2,
            alpha=0.85,
            label=f"En Düşük Hedef: {min_target:.2f} TL",
            zorder=3,
        )
        y_reference_values.extend([min_target, max_target])
    else:
        if max_target > 0:
            ax.axhline(
                max_target,
                color=MAX_TARGET_COLOR,
                linestyle=":",
                linewidth=1.2,
                alpha=0.85,
                label=f"En Yüksek Hedef: {max_target:.2f} TL",
                zorder=3,
            )
            y_reference_values.append(max_target)
        if min_target > 0:
            ax.axhline(
                min_target,
                color=MIN_TARGET_COLOR,
                linestyle=":",
                linewidth=1.2,
                alpha=0.85,
                label=f"En Düşük Hedef: {min_target:.2f} TL",
                zorder=3,
            )
            y_reference_values.append(min_target)

    # Ortalama analist hedefi
    if avg_target > 0:
        ax.axhline(
            avg_target,
            color=AVG_TARGET_COLOR,
            linestyle="--",
            linewidth=1.8,
            label=f"Ortalama Hedef: {avg_target:.2f} TL",
            zorder=4,
        )
        y_reference_values.append(avg_target)

    # Kullanıcının ortalama maliyeti
    if portfolio_item is not None and portfolio_item.is_owned:
        user_cost = _safe_float(portfolio_item.avg_cost, default=0.0)
        if user_cost > 0:
            ax.axhline(
                user_cost,
                color=USER_COST_COLOR,
                linestyle="--",
                linewidth=1.8,
                label=f"Senin Maliyetin: {user_cost:.2f} TL",
                zorder=4,
            )
            y_reference_values.append(user_cost)

    _apply_y_padding(ax, y_reference_values)

    if not y_reference_values:
        ax.text(
            0.5,
            0.5,
            "Bu hisse için fiyat ve hedef verisi bulunamadı.",
            transform=ax.transAxes,
            ha="center",
            va="center",
            color=MUTED_TEXT,
            fontsize=11,
        )

    ax.set_ylabel("Fiyat (TL)", color=TEXT_COLOR, fontsize=10, labelpad=6)

    price_text = f"{current_price:,.2f} TL" if current_price > 0 else "Fiyat Yok"
    if avg_target > 0 and current_price > 0:
        title = (
            f"{symbol_clean} — Güncel: {price_text} | "
            f"Hedef Potansiyel: {upside_pct:+.1f}%"
        )
    else:
        title = f"{symbol_clean} — Güncel: {price_text} | Potansiyel: {upside_pct:+.1f}%"

    ax.set_title(title, color=TEXT_COLOR, fontsize=12.5, fontweight="bold", pad=14)

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(
            handles,
            labels,
            loc="best",
            frameon=True,
            framealpha=0.92,
            facecolor=CARD_BG,
            edgecolor=SPINE_COLOR,
            labelcolor=TEXT_COLOR,
            fontsize=8.5,
            ncol=1 if len(handles) <= 5 else 2,
        )

    fig.tight_layout()
    return _fig_to_png_bytes(fig)


def _extract_portfolio_metrics(
    portfolio_evaluations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Portföy değerlendirme listesinden güvenli grafik metrikleri çıkarır."""
    rows: list[dict[str, Any]] = []
    for entry in portfolio_evaluations:
        if not isinstance(entry, dict):
            continue
        item = entry.get("portfolio")
        if not isinstance(item, PortfolioItem):
            continue
        market = entry.get("market")
        consensus = entry.get("consensus")

        current_price = _safe_float(
            market.current_price if isinstance(market, MarketSnapshot) else 0.0
        )
        if current_price <= 0 and isinstance(consensus, AnalystConsensus):
            current_price = _safe_float(consensus.current_price)

        is_owned = bool(item.is_owned)
        current_value = (
            round(item.lot * current_price, 2) if (is_owned and current_price > 0) else 0.0
        )
        total_cost = round(item.lot * item.avg_cost, 2) if is_owned else 0.0
        pnl_pct: Optional[float] = None
        if is_owned and current_price > 0 and item.avg_cost > 0:
            pnl_pct = round(((current_price - item.avg_cost) / item.avg_cost) * 100.0, 2)

        upside_pct = _resolve_upside_pct(
            consensus if isinstance(consensus, AnalystConsensus) else None,
            current_price,
        )

        rows.append(
            {
                "symbol": _normalize_symbol(item.symbol) or "HİSSE",
                "is_owned": is_owned,
                "current_value": current_value,
                "total_cost": total_cost,
                "pnl_pct": pnl_pct,
                "upside_pct": upside_pct,
            }
        )
    return rows


def _draw_allocation_panel(ax: plt.Axes, rows: list[dict[str, Any]]) -> None:
    """Sol panel: Güncel değere göre portföy veya eşit izleme dağılımı."""
    ax.set_facecolor(BG_COLOR)
    owned_with_value = [r for r in rows if r["current_value"] > 0]

    if owned_with_value:
        labels = [r["symbol"] for r in owned_with_value]
        values = [r["current_value"] for r in owned_with_value]
        panel_title = "Portföy Dağılımı (Güncel Değer)"
        is_value_weighted = True
    else:
        labels = [r["symbol"] for r in rows]
        values = [1.0 for _ in rows]
        panel_title = "İzleme Listesi (Eşit Ağırlık)"
        is_value_weighted = False

    total = sum(values)
    colors = [ALLOCATION_PALETTE[idx % len(ALLOCATION_PALETTE)] for idx in range(len(values))]

    def _autopct(pct: float) -> str:
        return f"%{pct:.1f}" if pct >= 5.0 else ""

    wedges, _, autotexts = ax.pie(
        values,
        labels=None,
        colors=colors,
        autopct=_autopct,
        pctdistance=0.76,
        startangle=140,
        wedgeprops={"width": 0.46, "edgecolor": BG_COLOR, "linewidth": 2},
        textprops={"color": TEXT_COLOR, "fontsize": 8.5, "fontweight": "bold"},
    )
    for txt in autotexts:
        txt.set_color(TEXT_COLOR)

    legend_labels: list[str] = []
    for lbl, val in zip(labels, values):
        share = (val / total) * 100.0 if total > 0 else 0.0
        if is_value_weighted:
            legend_labels.append(f"{lbl}: %{share:.1f} ({val:,.0f} TL)")
        else:
            legend_labels.append(f"{lbl}: %{share:.1f}")

    ax.legend(
        wedges,
        legend_labels,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        frameon=True,
        facecolor=CARD_BG,
        edgecolor=SPINE_COLOR,
        labelcolor=TEXT_COLOR,
        fontsize=8.2,
        ncol=1 if len(labels) <= 5 else 2,
    )
    ax.set_title(panel_title, color=TEXT_COLOR, fontsize=11, fontweight="bold", pad=10)
    ax.set_aspect("equal")


def _draw_comparison_panel(ax: plt.Axes, rows: list[dict[str, Any]]) -> None:
    """Sağ panel: Mevcut Kâr/Zarar % ve Analist Hedef Potansiyeli % yatay çubukları."""
    _style_axis(ax, grid_axis="x")
    ordered = list(reversed(rows))
    symbols = [r["symbol"] for r in ordered]
    y_positions = list(range(len(ordered)))

    has_any_pnl = any(r["pnl_pct"] is not None for r in ordered)
    all_values: list[float] = [r["upside_pct"] for r in ordered]

    if has_any_pnl:
        bar_height = 0.36
        for idx, row in enumerate(ordered):
            pnl = row["pnl_pct"]
            upside = row["upside_pct"]

            if pnl is not None:
                pnl_color = POSITIVE_COLOR if pnl >= 0 else NEGATIVE_COLOR
                ax.barh(
                    idx + bar_height / 2,
                    pnl,
                    height=bar_height,
                    color=pnl_color,
                    alpha=0.92,
                    zorder=3,
                )
                all_values.append(pnl)
                _annotate_hbar(ax, idx + bar_height / 2, pnl)
                upside_y = idx - bar_height / 2
                upside_h = bar_height
            else:
                upside_y = idx
                upside_h = 0.48

            upside_color = PRICE_COLOR if upside >= 0 else UPSIDE_NEGATIVE_COLOR
            ax.barh(
                upside_y,
                upside,
                height=upside_h,
                color=upside_color,
                alpha=0.92,
                zorder=3,
            )
            _annotate_hbar(ax, upside_y, upside)

        legend_handles = [
            Patch(facecolor=POSITIVE_COLOR, label="Kâr % (Mevcut)"),
            Patch(facecolor=NEGATIVE_COLOR, label="Zarar % (Mevcut)"),
            Patch(facecolor=PRICE_COLOR, label="Analist Hedef Potansiyeli %"),
        ]
        ax.legend(
            handles=legend_handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            frameon=True,
            facecolor=CARD_BG,
            edgecolor=SPINE_COLOR,
            labelcolor=TEXT_COLOR,
            fontsize=8.2,
            ncol=2,
        )
    else:
        bar_height = 0.52
        for idx, row in enumerate(ordered):
            upside = row["upside_pct"]
            upside_color = PRICE_COLOR if upside >= 0 else UPSIDE_NEGATIVE_COLOR
            ax.barh(
                idx,
                upside,
                height=bar_height,
                color=upside_color,
                alpha=0.92,
                zorder=3,
            )
            _annotate_hbar(ax, idx, upside)

        ax.legend(
            handles=[Patch(facecolor=PRICE_COLOR, label="Analist Hedef Potansiyeli %")],
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            frameon=True,
            facecolor=CARD_BG,
            edgecolor=SPINE_COLOR,
            labelcolor=TEXT_COLOR,
            fontsize=8.5,
        )

    ax.axvline(0.0, color="#64748b", linewidth=1.1, zorder=2)
    ax.set_yticks(y_positions)
    ax.set_yticklabels(symbols, color=TEXT_COLOR, fontsize=9.5, fontweight="bold")
    ax.set_xlabel("Oran (%)", color=TEXT_COLOR, fontsize=9.5, labelpad=6)
    ax.set_title(
        "Mevcut Kâr/Zarar ve Hedef Potansiyel (%)",
        color=TEXT_COLOR,
        fontsize=11,
        fontweight="bold",
        pad=10,
    )

    min_v = min(all_values) if all_values else 0.0
    max_v = max(all_values) if all_values else 0.0
    spread = max(abs(min_v), abs(max_v), 10.0)
    left_lim = min(min_v - spread * 0.25, -spread * 0.20)
    right_lim = max(max_v + spread * 0.28, spread * 0.25)
    ax.set_xlim(left_lim, right_lim)


def _annotate_hbar(ax: plt.Axes, y_pos: float, value: float) -> None:
    """Yatay çubuk ucuna yüzde değerini okunaklı biçimde yazar."""
    offset_pts = 4 if value >= 0 else -4
    ha = "left" if value >= 0 else "right"
    ax.annotate(
        f"{value:+.1f}%",
        xy=(value, y_pos),
        xytext=(offset_pts, 0),
        textcoords="offset points",
        ha=ha,
        va="center",
        color=TEXT_COLOR,
        fontsize=8.2,
    )


def generate_portfolio_chart_png(
    portfolio_evaluations: list[dict[str, Any]],
) -> bytes:
    """Portföy dağılımı ve Kâr/Zarar vs Analist Hedef Potansiyeli için 2 panelli PNG üretir."""
    rows = _extract_portfolio_metrics(portfolio_evaluations)

    if not rows:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        fig.patch.set_facecolor(BG_COLOR)
        ax.set_facecolor(BG_COLOR)
        ax.axis("off")
        ax.set_title("Portföy Özeti", color=TEXT_COLOR, fontsize=13, fontweight="bold", pad=14)
        ax.text(
            0.5,
            0.5,
            "Portföy veya izleme listesinde görüntülenecek hisse bulunamadı.",
            transform=ax.transAxes,
            ha="center",
            va="center",
            color=MUTED_TEXT,
            fontsize=11,
        )
        fig.tight_layout()
        return _fig_to_png_bytes(fig)

    fig_height = max(5.6, 0.65 * len(rows) + 2.6)
    fig, (ax_left, ax_right) = plt.subplots(
        1,
        2,
        figsize=(12.5, fig_height),
        gridspec_kw={"width_ratios": [1.0, 1.35], "wspace": 0.28},
    )
    fig.patch.set_facecolor(BG_COLOR)

    _draw_allocation_panel(ax_left, rows)
    _draw_comparison_panel(ax_right, rows)

    total_value = sum(r["current_value"] for r in rows)
    total_cost = sum(r["total_cost"] for r in rows if r["current_value"] > 0)
    if total_value > 0 and total_cost > 0:
        total_pnl_pct = ((total_value - total_cost) / total_cost) * 100.0
        suptitle = (
            f"Portföy Özeti — Toplam Değer: {total_value:,.0f} TL | "
            f"Toplam Kâr/Zarar: {total_pnl_pct:+.1f}%"
        )
    else:
        suptitle = f"Portföy ve İzleme Listesi Özeti ({len(rows)} Hisse)"

    fig.suptitle(suptitle, color=TEXT_COLOR, fontsize=13, fontweight="bold", y=0.98)
    fig.subplots_adjust(top=0.86, bottom=0.18, left=0.05, right=0.96)
    return _fig_to_png_bytes(fig)
