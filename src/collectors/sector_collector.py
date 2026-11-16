"""BIST sektör eşleştirmesi, emsal karşılaştırma, tarama ve portföy alarm modülü."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from zoneinfo import ZoneInfo

from src.config import DEFAULT_BIST_UNIVERSE
from src.storage.models import (
    AnalystConsensus,
    MarketSnapshot,
    PortfolioItem,
    PriceMoveAlert,
)

if TYPE_CHECKING:
    from src.collectors.analyst_collector import AnalystCollector
    from src.collectors.market_collector import MarketCollector
    from src.storage.db import DatabaseManager

logger = logging.getLogger(__name__)

BIST_SECTORS: dict[str, list[str]] = {
    "Havacılık": ["THYAO", "PGSUS", "TAVHL", "CLEBI"],
    "Bankacılık": ["AKBNK", "GARAN", "ISCTR", "YKBNK", "VAKBN", "HALKB", "TSKB"],
    "Holding": ["KCHOL", "SAHOL", "AGHOL", "DOHOL", "ALARK"],
    "Perakende": ["BIMAS", "MGROS", "SOKM", "MAVI"],
    "Otomotiv": ["FROTO", "TOASO", "DOAS", "TTRAK", "OTKAR"],
    "Enerji & Kimya": ["TUPRS", "PETKM", "ASTOR", "ENJSA", "AKSEN", "KONTR"],
    "Savunma & Teknoloji": ["ASELS", "KONTR", "SDTTR", "MIATK"],
    "Telekomünikasyon": ["TCELL", "TTKOM"],
    "Demir-Çelik & Maden": ["EREGL", "KRDMD", "KOZAL", "ISDMR"],
    "Sanayi & Dayanıklı Tüketim": ["ARCLK", "VESTL", "VESBE", "SISE"],
    "Gıda & İçecek": ["ULKER", "CCOLA", "AEFES"],
    "İnşaat & Çimento & GYO": ["ENKAI", "OYAKC", "CIMSA", "EKGYO"],
}

_FALLBACK_PEERS: tuple[str, ...] = ("THYAO", "KCHOL", "BIMAS")


def _normalize_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().replace(".IS", "")


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for sym in symbols:
        norm = _normalize_symbol(sym)
        if norm and norm not in seen:
            seen.add(norm)
            cleaned.append(norm)
    return cleaned


def _resolve_universe(
    universe: Optional[list[str]], analyst_collector: AnalystCollector
) -> list[str]:
    if universe is not None:
        return _dedupe_symbols(list(universe))
    settings_obj = getattr(analyst_collector, "settings", None)
    settings_universe = getattr(settings_obj, "bist_universe", None)
    if isinstance(settings_universe, (list, tuple)) and settings_universe:
        return _dedupe_symbols(list(settings_universe))
    return _dedupe_symbols(list(DEFAULT_BIST_UNIVERSE))


def _today_istanbul_date() -> str:
    try:
        return datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _fetch_market_and_consensus(
    symbol: str,
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
) -> tuple[MarketSnapshot, AnalystConsensus]:
    market = market_collector.get_snapshot(symbol, include_news=False)
    consensus = analyst_collector.get_consensus(
        symbol,
        current_price=market.current_price if market.current_price > 0 else None,
    )
    if market.current_price <= 0 and consensus.current_price > 0:
        market = market.model_copy(update={"current_price": consensus.current_price})
    return market, consensus


def _technical_strength_score(market: MarketSnapshot) -> tuple[int, int, int, float]:
    rec = (market.tv_recommendation or "").strip().upper()
    if rec == "GÜÇLÜ AL":
        tier = 5
    elif rec == "AL" or rec.startswith("AL ("):
        tier = 4
    elif rec == "NÖTR":
        tier = 3
    elif rec == "BİLİNMİYOR" or not rec:
        tier = 2
    elif rec == "SAT" or rec.startswith("SAT ("):
        tier = 1
    elif rec == "GÜÇLÜ SAT":
        tier = 0
    else:
        tier = 2
    net_signals = int(market.tv_buy_count) - int(market.tv_sell_count)
    return (tier, net_signals, int(market.tv_buy_count), float(market.daily_change_pct))


def get_sector_and_peers(symbol: str, max_peers: int = 3) -> tuple[str, list[str]]:
    """Verilen BIST hissesi için Türkçe sektör adını ve aynı sektördeki emsal hisseleri döndürür."""
    clean_symbol = _normalize_symbol(symbol)
    peer_limit = max(0, max_peers)

    for sector_name, members in BIST_SECTORS.items():
        if clean_symbol in members:
            peers = [m for m in members if m != clean_symbol][:peer_limit]
            return sector_name, peers

    fallback = [m for m in _FALLBACK_PEERS if m != clean_symbol][:peer_limit]
    return "Genel BIST", fallback


def _prefetch_fundamentals(
    market_collector: MarketCollector, symbols: list[str]
) -> None:
    """Toplu tarama öncesinde MarketCollector önbelleğini tek TV Scanner isteğiyle ısıtır."""
    if not symbols:
        return
    prefetch = getattr(market_collector, "prefetch_fundamentals", None)
    if callable(prefetch):
        try:
            prefetch(symbols)
        except Exception as exc:
            logger.debug("Toplu temel veri önyüklemesi atlandı: %s", exc)


def compare_stocks(
    symbols: list[str],
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
) -> dict[str, Any]:
    """Verilen BIST hisselerini fiyat, çarpan, konsensüs ve teknik güç açısından karşılaştırır."""
    clean_symbols = _dedupe_symbols(symbols)
    _prefetch_fundamentals(market_collector, clean_symbols)
    items: list[dict[str, Any]] = []

    def _fetch_one(sym: str) -> Optional[dict[str, Any]]:
        try:
            market, consensus = _fetch_market_and_consensus(
                sym, market_collector=market_collector, analyst_collector=analyst_collector
            )
            return {"symbol": sym, "market": market, "consensus": consensus}
        except Exception as exc:
            logger.debug("Karşılaştırma için hisse verisi alınamadı (%s): %s", sym, exc)
            return None

    if clean_symbols:
        with ThreadPoolExecutor(max_workers=min(4, len(clean_symbols))) as pool:
            for item in pool.map(_fetch_one, clean_symbols):
                if item is not None:
                    items.append(item)

    if clean_symbols:
        sectors = [get_sector_and_peers(s)[0] for s in clean_symbols]
        unique_sectors = set(sectors)
        if len(unique_sectors) == 1:
            sector = sectors[0]
        else:
            known_sectors = {s for s in sectors if s != "Genel BIST"}
            sector = next(iter(known_sectors)) if len(known_sectors) == 1 else "Genel BIST"
    else:
        sector = "Genel BIST"

    upside_candidates = [
        it
        for it in items
        if it["consensus"].avg_target_price > 0 or it["consensus"].upside_percent != 0
    ]
    best_upside_symbol = (
        max(upside_candidates, key=lambda it: it["consensus"].upside_percent)["symbol"]
        if upside_candidates
        else None
    )

    pe_candidates = [
        it
        for it in items
        if it["market"].pe_ratio is not None and it["market"].pe_ratio > 0
    ]
    lowest_pe_symbol = (
        min(pe_candidates, key=lambda it: it["market"].pe_ratio)["symbol"]
        if pe_candidates
        else None
    )

    strongest_tech_symbol = (
        max(items, key=lambda it: _technical_strength_score(it["market"]))["symbol"]
        if items
        else None
    )

    return {
        "sector": sector,
        "items": items,
        "highlights": {
            "best_upside_symbol": best_upside_symbol,
            "lowest_pe_symbol": lowest_pe_symbol,
            "strongest_tech_symbol": strongest_tech_symbol,
        },
    }


def get_golden_stocks(
    analyst_collector: AnalystCollector,
    market_collector: MarketCollector,
    universe: Optional[list[str]] = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """En az 2 AL tavsiyesi alan hisseleri (buy_count * 12 + upside_percent) skoruyla sıralar."""
    if limit <= 0:
        return []

    target_universe = _resolve_universe(universe, analyst_collector)
    _prefetch_fundamentals(market_collector, target_universe)
    scored: list[dict[str, Any]] = []

    def _score_one(sym: str) -> Optional[dict[str, Any]]:
        try:
            consensus = analyst_collector.get_consensus(sym)
            if consensus.buy_count < 2:
                return None

            try:
                market = market_collector.get_snapshot(sym, include_news=False)
            except Exception as mkt_exc:
                logger.debug("Altın hisse piyasa verisi alınamadı (%s): %s", sym, mkt_exc)
                market = MarketSnapshot(symbol=sym, current_price=consensus.current_price)

            if market.current_price <= 0 and consensus.current_price > 0:
                market = market.model_copy(update={"current_price": consensus.current_price})

            score = round((consensus.buy_count * 12) + consensus.upside_percent, 2)
            return {
                "symbol": sym,
                "market": market,
                "consensus": consensus,
                "score": score,
            }
        except Exception as exc:
            logger.debug("Altın hisse taraması hatası (%s): %s", sym, exc)
            return None

    if target_universe:
        with ThreadPoolExecutor(max_workers=min(4, len(target_universe))) as pool:
            for item in pool.map(_score_one, target_universe):
                if item is not None:
                    scored.append(item)

    scored.sort(
        key=lambda item: (
            item["score"],
            item["consensus"].upside_percent,
            item["consensus"].buy_count,
        ),
        reverse=True,
    )
    return scored[:limit]


def get_dividend_leaders(
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
    universe: Optional[list[str]] = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Temettü verimi pozitif olan hisseleri en yüksek verimden başlayarak sıralar."""
    if limit <= 0:
        return []

    target_universe = _resolve_universe(universe, analyst_collector)
    _prefetch_fundamentals(market_collector, target_universe)
    candidates: list[tuple[str, MarketSnapshot]] = []

    def _fetch_div_snapshot(sym: str) -> Optional[tuple[str, MarketSnapshot]]:
        try:
            market = market_collector.get_snapshot(sym, include_news=False)
            if market.dividend_yield_pct is not None and market.dividend_yield_pct > 0:
                return (sym, market)
        except Exception as exc:
            logger.debug("Temettü liderleri taraması hatası (%s): %s", sym, exc)
        return None

    if target_universe:
        with ThreadPoolExecutor(max_workers=min(4, len(target_universe))) as pool:
            for pair in pool.map(_fetch_div_snapshot, target_universe):
                if pair is not None:
                    candidates.append(pair)

    candidates.sort(
        key=lambda pair: float(pair[1].dividend_yield_pct or 0.0),
        reverse=True,
    )

    top_candidates = candidates[:limit]

    def _enrich_dividend(pair: tuple[str, MarketSnapshot]) -> dict[str, Any]:
        sym, market = pair
        try:
            consensus = analyst_collector.get_consensus(
                sym,
                current_price=market.current_price if market.current_price > 0 else None,
            )
        except Exception as exc:
            logger.debug("Temettü lideri konsensüs hatası (%s): %s", sym, exc)
            consensus = AnalystConsensus(symbol=sym, current_price=market.current_price)

        if market.current_price <= 0 and consensus.current_price > 0:
            market = market.model_copy(update={"current_price": consensus.current_price})

        return {
            "symbol": sym,
            "market": market,
            "consensus": consensus,
            "dividend_yield_pct": market.dividend_yield_pct,
        }

    if not top_candidates:
        return []

    with ThreadPoolExecutor(max_workers=min(4, len(top_candidates))) as pool:
        return list(pool.map(_enrich_dividend, top_candidates))


def check_portfolio_sharp_moves(
    portfolio_items: list[PortfolioItem],
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
    db: DatabaseManager,
    move_threshold_pct: float = 4.5,
    mark: bool = False,
) -> list[PriceMoveAlert]:
    """Portföydeki hisseler için sert fiyat hareketi, aşırı RSI ve hedef yakınlığı uyarısı üretir."""
    threshold = abs(float(move_threshold_pct))
    alert_date = _today_istanbul_date()
    seen_symbols: set[str] = set()
    seen_alert_ids: set[str] = set()
    new_alerts: list[PriceMoveAlert] = []

    for item in portfolio_items:
        sym = _normalize_symbol(item.symbol)
        if not sym or sym in seen_symbols:
            continue
        seen_symbols.add(sym)

        try:
            market, consensus = _fetch_market_and_consensus(
                sym, market_collector=market_collector, analyst_collector=analyst_collector
            )
        except Exception as exc:
            logger.debug("Portföy alarm kontrolü başarısız (%s): %s", sym, exc)
            continue

        price = market.current_price if market.current_price > 0 else consensus.current_price
        rounded_price = round(price, 2)
        rounded_change = round(market.daily_change_pct, 2)
        candidates: list[PriceMoveAlert] = []

        def _make_alert(alert_type: str, message: str) -> PriceMoveAlert:
            return PriceMoveAlert(
                symbol=sym,
                alert_type=alert_type,  # type: ignore[arg-type]
                current_price=rounded_price,
                daily_change_pct=rounded_change,
                message=message,
                alert_date=alert_date,
            )

        if market.daily_change_pct >= threshold:
            candidates.append(
                _make_alert(
                    "SERT_YUKSELIS",
                    f"🚀 {sym} bugün %{market.daily_change_pct:+.2f} sert yükseldi (Güncel: {price:.2f} TL).",
                )
            )
        elif market.daily_change_pct <= -threshold:
            candidates.append(
                _make_alert(
                    "SERT_DUSUS",
                    f"📉 {sym} bugün %{market.daily_change_pct:+.2f} sert düştü (Güncel: {price:.2f} TL).",
                )
            )

        if market.rsi is not None:
            if market.rsi <= 30:
                candidates.append(
                    _make_alert(
                        "UCUZLUK_RSI",
                        f"🟢 {sym} aşırı satım / ucuzluk bölgesinde (RSI: {market.rsi:.1f}, Güncel: {price:.2f} TL).",
                    )
                )
            elif market.rsi >= 75:
                candidates.append(
                    _make_alert(
                        "ISINMA_RSI",
                        f"🔥 {sym} aşırı ısınma bölgesinde (RSI: {market.rsi:.1f}, Güncel: {price:.2f} TL).",
                    )
                )

        if (
            consensus.avg_target_price > 0
            and price > 0
            and price >= consensus.avg_target_price * 0.97
        ):
            candidates.append(
                _make_alert(
                    "HEDEF_YAKIN",
                    (
                        f"🎯 {sym} ortalama analist hedefine ({consensus.avg_target_price:.2f} TL) "
                        f"yaklaştı/ulaştı (Güncel: {price:.2f} TL)."
                    ),
                )
            )

        for alert in candidates:
            if alert.alert_id in seen_alert_ids:
                continue
            if db.is_price_alert_notified(alert.alert_id):
                continue
            seen_alert_ids.add(alert.alert_id)
            new_alerts.append(alert)
            if mark:
                db.mark_price_alert_notified(alert)

    return new_alerts
