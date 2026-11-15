"""Gün içi rapor taraması, 18:30 günlük özet ve ortak hisse analiz iş akışları."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.ai.commentator import BorsaCommentator
from src.collectors.analyst_collector import AnalystCollector
from src.collectors.market_collector import MarketCollector
from src.collectors.sector_collector import check_portfolio_sharp_moves
from src.config import Settings, get_settings
from src.storage.db import DatabaseManager
from src.storage.models import (
    AnalystConsensus,
    AnalystReport,
    MarketSnapshot,
    PortfolioItem,
    PriceMoveAlert,
)

logger = logging.getLogger(__name__)


def format_for_telegram_html(text: str) -> str:
    """Düz/Markdown benzeri metni güvenli Telegram HTML biçimine dönüştürür."""
    escaped = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)


def split_telegram_message(text: str, max_length: int = 3900) -> list[str]:
    """Uzun Telegram mesajlarını satır sınırlarından böler."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in text.splitlines():
        line_len = len(line) + 1
        if current_len + line_len > max_length and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


async def send_telegram_text(
    bot: Any,
    chat_id: str | int,
    text: str,
    reply_markup: Any = None,
) -> None:
    """Uzun metinleri parçalayarak HTML modunda (hata olursa düz metinle) gönderir."""
    chunks = split_telegram_message(text)
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        html_chunk = format_for_telegram_html(chunk)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=html_chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.debug("HTML gönderimi başarısız, düz metne dönülüyor: %s", exc)
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=markup,
                disable_web_page_preview=True,
            )


def analyze_stock(
    symbol: str,
    db: DatabaseManager,
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
    commentator: BorsaCommentator,
    override_lot: Optional[float] = None,
    override_cost: Optional[float] = None,
) -> tuple[str, MarketSnapshot, AnalystConsensus, Optional[PortfolioItem]]:
    """Tek bir BIST hissesi için tüm verileri toplar ve halk dilinde AI yorumunu üretir."""
    clean_symbol = MarketCollector.normalize_bist_symbol(symbol)
    market = market_collector.get_snapshot(clean_symbol, include_news=True)
    consensus = analyst_collector.get_consensus(
        clean_symbol,
        current_price=market.current_price if market.current_price > 0 else None,
    )
    if market.current_price <= 0 and consensus.current_price > 0:
        market = market.model_copy(update={"current_price": consensus.current_price})

    portfolio_item = db.get_portfolio_item(clean_symbol)
    if override_lot is not None and override_cost is not None:
        portfolio_item = PortfolioItem(
            symbol=clean_symbol,
            lot=float(override_lot),
            avg_cost=float(override_cost),
        )

    analysis_text = commentator.comment_on_stock(
        symbol=clean_symbol,
        consensus=consensus,
        market=market,
        portfolio_item=portfolio_item,
    )
    return analysis_text, market, consensus, portfolio_item


def build_daily_bulletin_text(
    db: DatabaseManager,
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
    commentator: BorsaCommentator,
    settings: Settings,
) -> str:
    """Portföy/izleme listesi + BIST fırsat radarı + yeni raporlardan günlük bülten oluşturur."""
    portfolio_items = db.list_portfolio_items()
    portfolio_symbols = {item.symbol for item in portfolio_items}

    def _eval_portfolio_item(p_item: PortfolioItem) -> dict[str, Any]:
        market = market_collector.get_snapshot(p_item.symbol, include_news=False)
        consensus = analyst_collector.get_consensus(
            p_item.symbol,
            current_price=market.current_price if market.current_price > 0 else None,
        )
        return {
            "portfolio": p_item,
            "market": market,
            "consensus": consensus,
        }

    portfolio_evaluations: list[dict[str, Any]] = []
    if portfolio_items:
        with ThreadPoolExecutor(max_workers=min(6, len(portfolio_items))) as pool:
            portfolio_evaluations = list(pool.map(_eval_portfolio_item, portfolio_items))

    opportunities = analyst_collector.scan_opportunities(
        universe=settings.bist_universe,
        portfolio_symbols=portfolio_symbols,
        limit=5,
    )
    new_reports: list[AnalystReport] = []
    seen_ids: set[str] = set()
    for opp in opportunities:
        for rep in opp.recent_reports:
            if rep.report_id not in seen_ids:
                seen_ids.add(rep.report_id)
                new_reports.append(rep)
    if not new_reports:
        target_symbols = list(portfolio_symbols) + [
            s for s in settings.bist_universe if s not in portfolio_symbols
        ]
        new_reports = analyst_collector.fetch_latest_reports(symbols=target_symbols[:8])

    return commentator.generate_daily_bulletin(
        portfolio_evaluations=portfolio_evaluations,
        opportunities=opportunities,
        new_reports=new_reports,
    )


def _format_alert_message(
    report: AnalystReport,
    consensus: AnalystConsensus,
    market: MarketSnapshot,
    portfolio_item: Optional[PortfolioItem],
    is_in_portfolio: bool,
) -> str:
    """Anlık aracı kurum raporu veya fırsat bildirimi için sade Telegram mesajı oluşturur."""
    tag = "💼 PORTFÖY / TAKİP HİSSESİ" if is_in_portfolio else "🚀 BIST FIRSAT RADARI"
    price = market.current_price or consensus.current_price

    lines = [
        f"🔔 {tag} — Yeni Analist Raporu",
        f"📌 Hisse: {report.symbol} ({market.company_name})",
        f"🏦 Kurum: {report.institution} | Karar: {report.recommendation}",
        f"🎯 Kurum Hedef Fiyatı: {report.target_price:.2f} TL (%{report.upside_percent:+.1f})",
    ]
    if price > 0:
        lines.append(f"💰 Güncel Fiyat: {price:.2f} TL (Günlük: %{market.daily_change_pct:+.2f})")
    if consensus.avg_target_price > 0:
        lines.append(
            f"📊 Ortalama Konsensüs: {consensus.avg_target_price:.2f} TL "
            f"(%{consensus.upside_percent:+.1f} potansiyel | "
            f"{consensus.buy_count} AL / {consensus.hold_count} TUT / {consensus.sell_count} SAT)"
        )
    if portfolio_item and portfolio_item.is_owned and price > 0:
        pnl = portfolio_item.calculate_pnl(price, consensus.avg_target_price or report.target_price)
        lines.append(
            f"💼 Senin Maliyetin: {portfolio_item.avg_cost:.2f} TL ({portfolio_item.lot:g} lot) | "
            f"Güncel K/Z: %{pnl['pnl_pct']:+.1f} ({pnl['pnl_tl']:+,.0f} TL)"
        )
    lines.append(f"🔍 Hazır Teknik Özet: {market.plain_technical_comment}")
    lines.append(f"\n💡 Detaylı AI yorumu için '{report.symbol}' yazabilirsin.")
    return "\n".join(lines)


_ALERT_TYPE_TITLES: dict[str, str] = {
    "SERT_YUKSELIS": "Sert Yükseliş Uyarısı",
    "SERT_DUSUS": "Sert Düşüş Uyarısı",
    "UCUZLUK_RSI": "Aşırı Satım / Ucuzluk Bölgesi",
    "ISINMA_RSI": "Aşırı Isınma Bölgesi",
    "HEDEF_YAKIN": "Analist Hedefine Yaklaşma",
}


def _format_price_move_alert_message(
    alert: PriceMoveAlert,
    portfolio_item: Optional[PortfolioItem] = None,
) -> str:
    """Portföy/izleme listesindeki sert fiyat, RSI veya hedef fiyat uyarısını biçimlendirir."""
    title = _ALERT_TYPE_TITLES.get(alert.alert_type, "Portföy Hareket Uyarısı")
    lines = [
        f"⚡ **PORTFÖY / TAKİP ALARMI — {title}**",
        f"📌 Hisse: **{alert.symbol}** | Güncel Fiyat: {alert.current_price:.2f} TL "
        f"(Günlük: %{alert.daily_change_pct:+.2f})",
        alert.message,
    ]
    if portfolio_item and portfolio_item.is_owned and alert.current_price > 0:
        pnl = portfolio_item.calculate_pnl(alert.current_price)
        if pnl["pnl_pct"] is not None and pnl["pnl_tl"] is not None:
            lines.append(
                f"💼 Senin Maliyetin: {portfolio_item.avg_cost:.2f} TL ({portfolio_item.lot:g} lot) | "
                f"Güncel K/Z: %{pnl['pnl_pct']:+.1f} ({pnl['pnl_tl']:+,.0f} TL)"
            )
    lines.append(f"\n💡 Detaylı AI yorumu ve grafik için sohbete `{alert.symbol}` yazabilirsin.")
    return "\n".join(lines)


def collect_portfolio_price_alerts(
    db: DatabaseManager,
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
    mark_as_notified: bool = True,
    move_threshold_pct: float = 4.5,
) -> list[str]:
    """Portföy/izleme hisselerindeki sert hareket (±4.5%), RSI ve hedef yakınlığı uyarılarını toplar."""
    portfolio_items = db.list_portfolio_items()
    if not portfolio_items:
        return []

    portfolio_map = {item.symbol: item for item in portfolio_items}
    price_alerts = check_portfolio_sharp_moves(
        portfolio_items=portfolio_items,
        market_collector=market_collector,
        analyst_collector=analyst_collector,
        db=db,
        move_threshold_pct=move_threshold_pct,
        mark=False,
    )

    formatted_alerts: list[str] = []
    for alert in price_alerts:
        formatted_alerts.append(
            _format_price_move_alert_message(
                alert=alert,
                portfolio_item=portfolio_map.get(alert.symbol),
            )
        )
        if mark_as_notified:
            db.mark_price_alert_notified(alert)

    return formatted_alerts


def collect_pending_report_alerts(
    db: DatabaseManager,
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
    settings: Settings,
    mark_as_notified: bool = True,
) -> list[str]:
    """Yeni aracı kurum raporlarını ve portföy sert hareket/RSI/hedef uyarılarını tarar."""
    portfolio_items = db.list_portfolio_items()
    portfolio_map = {item.symbol: item for item in portfolio_items}
    portfolio_symbols = set(portfolio_map.keys())

    scan_symbols = list(portfolio_symbols) + [
        s for s in settings.bist_universe if s not in portfolio_symbols
    ]
    latest_reports = analyst_collector.fetch_latest_reports(symbols=scan_symbols[:20])

    alerts: list[str] = []
    consensus_cache: dict[str, AnalystConsensus] = {}
    market_cache: dict[str, MarketSnapshot] = {}

    for report in latest_reports:
        if db.is_report_notified(report.report_id):
            continue

        sym = report.symbol
        if sym not in market_cache:
            market_cache[sym] = market_collector.get_snapshot(sym, include_news=False)
        market = market_cache[sym]

        if sym not in consensus_cache:
            consensus_cache[sym] = analyst_collector.get_consensus(
                sym,
                current_price=market.current_price if market.current_price > 0 else None,
            )
        consensus = consensus_cache[sym]

        is_in_portfolio = sym in portfolio_symbols
        if not analyst_collector.should_notify_report(consensus, is_in_portfolio=is_in_portfolio):
            continue

        price = market.current_price or consensus.current_price
        if price > 0 and report.upside_percent == 0.0:
            report = report.model_copy(
                update={
                    "current_price": price,
                    "upside_percent": round(((report.target_price - price) / price) * 100, 2),
                }
            )

        alert_text = _format_alert_message(
            report=report,
            consensus=consensus,
            market=market,
            portfolio_item=portfolio_map.get(sym),
            is_in_portfolio=is_in_portfolio,
        )
        alerts.append(alert_text)

        if mark_as_notified:
            db.mark_report_notified(report)

    alerts.extend(
        collect_portfolio_price_alerts(
            db=db,
            market_collector=market_collector,
            analyst_collector=analyst_collector,
            mark_as_notified=mark_as_notified,
        )
    )
    return alerts


async def intraday_report_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hafta içi gün içinde yeni analist raporlarını ve portföy fiyat/RSI alarmlarını gönderir."""
    settings: Settings = context.bot_data.get("settings") or get_settings()
    if not settings.has_valid_chat_id():
        return

    now_local = datetime.now(ZoneInfo(settings.timezone))
    if now_local.weekday() >= 5 or not (9 <= now_local.hour <= 19):
        return

    db: DatabaseManager = context.bot_data["db"]
    market_collector: MarketCollector = context.bot_data["market_collector"]
    analyst_collector: AnalystCollector = context.bot_data["analyst_collector"]

    try:
        alerts = await asyncio.to_thread(
            collect_pending_report_alerts,
            db,
            market_collector,
            analyst_collector,
            settings,
            True,
        )
        for alert_text in alerts:
            await send_telegram_text(context.bot, settings.telegram_chat_id, alert_text)
    except Exception as exc:
        logger.exception("Gün içi tarama görevi sırasında hata: %s", exc)


async def daily_evening_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hafta içi akşam 18:30'da günlük BIST kapanış özet bültenini gönderir."""
    settings: Settings = context.bot_data.get("settings") or get_settings()
    if not settings.has_valid_chat_id():
        return

    now_local = datetime.now(ZoneInfo(settings.timezone))
    if now_local.weekday() >= 5:
        return

    db: DatabaseManager = context.bot_data["db"]
    market_collector: MarketCollector = context.bot_data["market_collector"]
    analyst_collector: AnalystCollector = context.bot_data["analyst_collector"]
    commentator: BorsaCommentator = context.bot_data["commentator"]

    try:
        bulletin = await asyncio.to_thread(
            build_daily_bulletin_text,
            db,
            market_collector,
            analyst_collector,
            commentator,
            settings,
        )
        await send_telegram_text(context.bot, settings.telegram_chat_id, bulletin)
    except Exception as exc:
        logger.exception("Günlük akşam özeti gönderilirken hata: %s", exc)
