"""BIST hisse fiyatları, temel çarpanlar, hazır teknik özetler ve haber toplayıcısı."""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Optional
from urllib.parse import quote_plus

import feedparser
import httpx
import yfinance as yf
from tradingview_ta import Interval, TA_Handler
from tradingview_ta.technicals import Compute

from src.storage.models import MarketSnapshot

from .sector_collector import get_sector_and_peers

logger = logging.getLogger(__name__)

_TV_SCANNER_URL = "https://scanner.tradingview.com/turkey/scan"
_TV_SCANNER_COLUMNS: tuple[str, ...] = (
    "name",
    "description",
    "close",
    "change",
    "sector",
    "price_earnings_ttm",
    "price_book_ratio",
    "price_book_fq",
    "price_52_week_high",
    "price_52_week_low",
    "dividends_yield_current",
    "dps_common_stock_prim_issue_fy",
    "Recommend.All",
    "RSI",
    "price_target_average",
    "price_target_high",
    "price_target_low",
    "recommendation_buy",
    "recommendation_over",
    "recommendation_hold",
    "recommendation_sell",
    "recommendation_under",
    "recommendation_total",
)

_BIST_COMPANY_NAMES: dict[str, str] = {
    "THYAO": "Türk Hava Yolları",
    "TUPRS": "Tüpraş",
    "ASELS": "Aselsan",
    "EREGL": "Erdemir",
    "BIMAS": "BİM Mağazalar",
    "SISE": "Şişecam",
    "AKBNK": "Akbank",
    "GARAN": "Garanti BBVA",
    "ISCTR": "İş Bankası (C)",
    "YKBNK": "Yapı Kredi",
    "KCHOL": "Koç Holding",
    "SAHOL": "Sabancı Holding",
    "FROTO": "Ford Otosan",
    "TOASO": "Tofaş Oto",
    "TCELL": "Turkcell",
    "TTKOM": "Türk Telekom",
    "PETKM": "Petkim",
    "HEKTS": "Hektaş",
    "SASA": "Sasa Polyester",
    "ENKAI": "Enka İnşaat",
    "ARCLK": "Arçelik",
    "MGROS": "Migros",
    "PGSUS": "Pegasus",
    "KOZAL": "Koza Altın",
    "KRDMD": "Kardemir (D)",
    "ASTOR": "Astor Enerji",
    "KONTR": "Kontrolmatik",
    "TAVHL": "TAV Havalimanları",
    "ALARK": "Alarko Holding",
    "VESTL": "Vestel",
    "DOAS": "Doğuş Otomotiv",
    "ULKER": "Ülker Bisküvi",
    "CCOLA": "Coca-Cola İçecek",
    "AEFES": "Anadolu Efes",
    "EKGYO": "Emlak Konut GYO",
    "OYAKC": "Oyak Çimento",
    "CIMSA": "Çimsa",
    "MAVI": "Mavi Giyim",
    "AGHOL": "Anadolu Grubu Holding",
    "SOKM": "Şok Marketler",
    "CLEBI": "Çelebi Hava Servisi",
    "VAKBN": "VakıfBank",
    "HALKB": "Halkbank",
    "TSKB": "TSKB",
    "DOHOL": "Doğan Holding",
    "TTRAK": "Türk Traktör",
    "OTKAR": "Otokar",
    "ENJSA": "Enerjisa",
    "AKSEN": "Aksa Enerji",
    "SDTTR": "SDT Uzay",
    "MIATK": "Mia Teknoloji",
    "ISDMR": "İskenderun Demir Çelik",
    "VESBE": "Vestel Beyaz Eşya",
}

_YF_SECTOR_TR_MAP: dict[str, str] = {
    "Industrials": "Sanayi",
    "Financial Services": "Finans & Bankacılık",
    "Consumer Cyclical": "Tüketici & Perakende",
    "Consumer Defensive": "Gıda & Tüketim",
    "Energy": "Enerji",
    "Basic Materials": "Kimya & Hammadde",
    "Technology": "Teknoloji",
    "Communication Services": "Telekomünikasyon",
    "Real Estate": "GYO & İnşaat",
    "Utilities": "Enerji & Altyapı",
    "Healthcare": "Sağlık",
    "Transportation": "Ulaştırma",
    "Communications": "Telekomünikasyon",
    "Energy Minerals": "Enerji",
    "Finance": "Finans & Bankacılık",
    "Retail Trade": "Perakende & Ticaret",
    "Process Industries": "Sanayi & Kimya",
    "Non-Energy Minerals": "Maden & Çimento",
    "Consumer Non-Durables": "Gıda & Tüketim",
    "Consumer Durables": "Dayanıklı Tüketim & Otomotiv",
    "Producer Manufacturing": "Üretim & Sanayi",
    "Electronic Technology": "Teknoloji & Savunma",
}

_TV_SCANNER_LOCK = threading.Lock()
_TV_SCANNER_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_tv_scanner_cache() -> None:
    """Paylaşılan TradingView tarayıcı önbelleğini temizler."""
    with _TV_SCANNER_LOCK:
        _TV_SCANNER_CACHE.clear()


def fetch_tradingview_scanner_data(
    symbols: list[str],
    timeout_seconds: float = 8.0,
    cache_ttl_seconds: float = 300.0,
) -> dict[str, dict[str, Any]]:
    """TradingView Turkey Scanner üzerinden toplu BIST fiyat, temel ve analist verisi çeker."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for sym in symbols:
        norm = str(sym).strip().upper().replace(".IS", "")
        if norm and norm not in seen:
            seen.add(norm)
            cleaned.append(norm)

    if not cleaned:
        return {}

    now_ts = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    if cache_ttl_seconds > 0:
        with _TV_SCANNER_LOCK:
            for sym in cleaned:
                cached = _TV_SCANNER_CACHE.get(sym)
                if cached and (now_ts - cached[0]) < cache_ttl_seconds:
                    results[sym] = cached[1]
                else:
                    missing.append(sym)
    else:
        missing = list(cleaned)

    if missing:
        payload = {
            "symbols": {"tickers": [f"BIST:{s}" for s in missing]},
            "columns": list(_TV_SCANNER_COLUMNS),
        }
        try:
            with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
                resp = client.post(
                    _TV_SCANNER_URL,
                    json=payload,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; BorsaAjani/1.0)"},
                )
            if resp.status_code == 200:
                rows = resp.json().get("data", [])
                fetched_at = time.monotonic()
                new_entries: dict[str, dict[str, Any]] = {}
                for row in rows:
                    raw_ticker = str(row.get("s") or "")
                    sym = raw_ticker.split(":")[-1].strip().upper()
                    values = row.get("d")
                    if sym and isinstance(values, list) and len(values) == len(_TV_SCANNER_COLUMNS):
                        entry = dict(zip(_TV_SCANNER_COLUMNS, values))
                        new_entries[sym] = entry
                        results[sym] = entry
                if cache_ttl_seconds > 0 and new_entries:
                    with _TV_SCANNER_LOCK:
                        for sym, entry in new_entries.items():
                            _TV_SCANNER_CACHE[sym] = (fetched_at, entry)
        except Exception as exc:
            logger.debug("TradingView toplu tarama verisi alınamadı: %s", exc)

    return results


def get_tv_scanner_row(
    symbol: str,
    timeout_seconds: float = 8.0,
    cache_ttl_seconds: float = 300.0,
) -> Optional[dict[str, Any]]:
    """Tek bir BIST hissesi için önbellekli TradingView Scanner satırını döner."""
    norm = str(symbol).strip().upper().replace(".IS", "")
    if not norm:
        return None
    return fetch_tradingview_scanner_data(
        [norm],
        timeout_seconds=timeout_seconds,
        cache_ttl_seconds=cache_ttl_seconds,
    ).get(norm)

_TV_RECOMMENDATION_MAP: dict[str, str] = {
    "STRONG_BUY": "GÜÇLÜ AL",
    "BUY": "AL",
    "NEUTRAL": "NÖTR",
    "SELL": "SAT",
    "STRONG_SELL": "GÜÇLÜ SAT",
}


def _tr_third_person_suffix(number: int) -> str:
    """Sayıya uygun Türkçe iyelik ekini ('i/'si/'sı/'ü/'u) döndürür."""
    ones = number % 10
    if ones in (1, 5, 8):
        return f"{number}'i"
    if ones in (2, 7):
        return f"{number}'si"
    if ones in (3, 4):
        return f"{number}'ü"
    if ones == 6:
        return f"{number}'sı"
    if ones == 9:
        return f"{number}'u"
    # Sonu 0 ile bitenler (0, 10, 20, 30...)
    tens = (number % 100) // 10
    if number == 0 or tens in (4, 6, 9):
        return f"{number}'ı"
    if tens in (1, 3):
        return f"{number}'u"
    return f"{number}'si"


def build_plain_technical_comment(
    tv_recommendation: str,
    tv_buy_count: int,
    tv_neutral_count: int,
    tv_sell_count: int,
    rsi: Optional[float],
) -> str:
    """Teknik analiz bilmeyen yatırımcı için indikatör özetini halk diline çevirir."""
    total_signals = tv_buy_count + tv_neutral_count + tv_sell_count
    parts: list[str] = []

    if total_signals > 0:
        parts.append(
            f"Hazır teknik göstergelerin {total_signals} tanesinden "
            f"{_tr_third_person_suffix(tv_buy_count)} AL, "
            f"{_tr_third_person_suffix(tv_neutral_count)} Nötr, "
            f"{_tr_third_person_suffix(tv_sell_count)} SAT sinyali veriyor "
            f"(Genel Teknik Görünüm: {tv_recommendation})."
        )
    elif tv_recommendation and tv_recommendation != "BİLİNMİYOR":
        parts.append(f"Genel teknik görünüm '{tv_recommendation}' yönünde.")

    if rsi is not None:
        if rsi >= 70:
            parts.append(
                f"Fiyat son günlerde hızlı yükselmiş (RSI: {rsi:.0f}), kısa vadede kâr satışı veya dinlenme gelebilir."
            )
        elif rsi <= 30:
            parts.append(
                f"Hisse son dönemde oldukça gerilemiş ve ucuz/aşırı satım bölgesine yaklaşmış (RSI: {rsi:.0f}); tepki alımları görülebilir."
            )
        elif rsi >= 50:
            parts.append(
                f"Fiyat dengeli ve yukarı yönlü bir güç gösteriyor, aşırı şişkinlik yok (RSI: {rsi:.0f})."
            )
        else:
            parts.append(
                f"Hisse sakin/dinlenme bölgesinde hareket ediyor, aşırı ısınma bulunmuyor (RSI: {rsi:.0f})."
            )

    if not parts:
        return "Teknik görünüm verisi şu an sınırlı; karar verirken aracı kurum hedefleri ön planda tutulabilir."

    return " ".join(parts)


def build_plain_valuation_comment(
    pe_ratio: Optional[float],
    pb_ratio: Optional[float],
    drawdown_from_high_pct: Optional[float],
) -> str:
    """F/K, PD/DD ve 52 haftalık zirveye uzaklığı sade Türkçe ile açıklar."""
    parts: list[str] = []

    if drawdown_from_high_pct is not None:
        if drawdown_from_high_pct <= -25:
            parts.append(
                f"Son 1 yılın zirvesinden %{abs(drawdown_from_high_pct):.1f} aşağıda, ciddi bir iskontoyla işlem görüyor."
            )
        elif drawdown_from_high_pct <= -10:
            parts.append(
                f"Son 1 yılın zirvesinden %{abs(drawdown_from_high_pct):.1f} daha düşük seviyede; tepe fiyatının altında."
            )
        elif drawdown_from_high_pct >= -3:
            parts.append("Son 1 yılın zirvesine oldukça yakın seviyelerde fiyatlanıyor.")
        else:
            parts.append(
                f"Son 1 yılın zirvesinin %{abs(drawdown_from_high_pct):.1f} altında dengeli seyrediyor."
            )

    valuation_bits: list[str] = []
    if pe_ratio is not None and pe_ratio > 0:
        if pe_ratio <= 8:
            valuation_bits.append(f"F/K ({pe_ratio:.1f}) kârına göre oldukça ucuz görünüyor")
        elif pe_ratio <= 18:
            valuation_bits.append(f"F/K ({pe_ratio:.1f}) makul seviyede")
        else:
            valuation_bits.append(f"F/K ({pe_ratio:.1f}) görece yüksek büyüme beklentisi fiyatlıyor")

    if pb_ratio is not None and pb_ratio > 0:
        if pb_ratio <= 1.5:
            valuation_bits.append(f"PD/DD ({pb_ratio:.2f}) defter değerine yakın/makul")
        elif pb_ratio <= 3.5:
            valuation_bits.append(f"PD/DD ({pb_ratio:.2f}) dengeli")
        else:
            valuation_bits.append(f"PD/DD ({pb_ratio:.2f}) yüksek primli")

    if valuation_bits:
        parts.append("Temel çarpanlarda " + ", ".join(valuation_bits) + ".")

    if not parts:
        return "Temel çarpan verileri güncel olarak sınırlı."

    return " ".join(parts)


class MarketCollector:
    """Yahoo Finance, TradingView TA ve Google News RSS üzerinden BIST verilerini toplar."""

    def __init__(self, timeout_seconds: float = 6.0, cache_ttl_seconds: float = 600.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache_lock = threading.Lock()
        self._snapshot_cache: dict[tuple[str, bool], tuple[float, MarketSnapshot]] = {}

    @staticmethod
    def normalize_bist_symbol(symbol: str) -> str:
        """Hisse kodunu BIST formatına (örn. THYAO) temizler."""
        return symbol.strip().upper().replace(".IS", "")

    @staticmethod
    def _parse_positive_float(value: object) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            num = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not math.isfinite(num) or num <= 0:
            return None
        return num

    @staticmethod
    def _resolve_sector_tr(symbol: str, yf_sector: Optional[str] = None) -> str:
        sector_tr, _ = get_sector_and_peers(symbol)
        if sector_tr != "Genel BIST":
            return sector_tr
        if yf_sector:
            mapped = _YF_SECTOR_TR_MAP.get(yf_sector.strip())
            if mapped:
                return mapped
        return sector_tr

    @staticmethod
    def _compute_rsi_from_closes(closes: list[float], period: int = 14) -> Optional[float]:
        if len(closes) <= period:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        recent = deltas[-period:]
        gains = [d for d in recent if d > 0]
        losses = [-d for d in recent if d < 0]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 2)

    def _fetch_tradingview_summary(
        self, symbol: str
    ) -> tuple[str, int, int, int, Optional[float]]:
        """TradingView hazır teknik sinyal özetini çeker."""
        try:
            handler = TA_Handler(
                symbol=symbol,
                screener="turkey",
                exchange="BIST",
                interval=Interval.INTERVAL_1_DAY,
                timeout=self.timeout_seconds,
            )
            analysis = handler.get_analysis()
            summary = analysis.summary or {}
            indicators = analysis.indicators or {}
            raw_rec = str(summary.get("RECOMMENDATION", "NEUTRAL")).upper()
            rec_tr = _TV_RECOMMENDATION_MAP.get(raw_rec, "NÖTR")
            buy_count = int(summary.get("BUY", 0) or 0)
            neutral_count = int(summary.get("NEUTRAL", 0) or 0)
            sell_count = int(summary.get("SELL", 0) or 0)
            rsi_val = indicators.get("RSI")
            rsi = round(float(rsi_val), 2) if rsi_val is not None else None
            return rec_tr, buy_count, neutral_count, sell_count, rsi
        except Exception as exc:
            logger.debug("TradingView verisi alınamadı (%s): %s", symbol, exc)
            return "BİLİNMİYOR", 0, 0, 0, None

    @staticmethod
    def _parse_finite_float(value: object) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            num = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return num if math.isfinite(num) else None

    def prefetch_fundamentals(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Verilen BIST hisseleri için TradingView Scanner verilerini tek istekte önbelleğe alır."""
        return fetch_tradingview_scanner_data(
            symbols,
            timeout_seconds=self.timeout_seconds,
            cache_ttl_seconds=min(300.0, self.cache_ttl_seconds),
        )

    @classmethod
    def _resolve_company_name(
        cls,
        clean_symbol: str,
        yf_name: Optional[str] = None,
        tv_description: Optional[str] = None,
    ) -> str:
        for candidate in (yf_name, tv_description):
            cleaned = str(candidate or "").strip()
            if cleaned and cleaned.upper() != clean_symbol:
                return cleaned
        return _BIST_COMPANY_NAMES.get(clean_symbol, clean_symbol)

    def fetch_recent_news(
        self, symbol: str, company_name: str = "", limit: int = 4
    ) -> list[str]:
        """Google News RSS üzerinden hisseye ait güncel haber ve KAP başlıklarını çeker."""
        clean_symbol = self.normalize_bist_symbol(symbol)
        query_terms = f"{clean_symbol} hisse KAP borsa"
        url = (
            f"https://news.google.com/rss/search?"
            f"q={quote_plus(query_terms)}&hl=tr&gl=TR&ceid=TR:tr"
        )
        try:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; BorsaAjani/1.0)"},
                )
                response.raise_for_status()
            parsed = feedparser.parse(response.text)
            headlines: list[str] = []
            for entry in parsed.entries[:limit]:
                title = getattr(entry, "title", "").strip()
                if title:
                    headlines.append(title)
            return headlines
        except Exception as exc:
            logger.debug("Haber akışı çekilemedi (%s): %s", clean_symbol, exc)
            return []

    def get_snapshot(self, symbol: str, include_news: bool = True) -> MarketSnapshot:
        """Bir BIST hissesi için fiyat, çarpan, hazır teknik yorum ve haber özeti döner."""
        clean_symbol = self.normalize_bist_symbol(symbol)
        now_ts = time.monotonic()
        with self._cache_lock:
            cached = self._snapshot_cache.get((clean_symbol, include_news))
            if not cached and not include_news:
                cached = self._snapshot_cache.get((clean_symbol, True))
            if cached and (now_ts - cached[0]) < self.cache_ttl_seconds:
                return cached[1]

        yf_symbol = f"{clean_symbol}.IS"

        tv_row = get_tv_scanner_row(
            clean_symbol,
            timeout_seconds=self.timeout_seconds,
            cache_ttl_seconds=min(300.0, self.cache_ttl_seconds),
        )
        tv_price = self._parse_positive_float(tv_row.get("close")) if tv_row else None
        tv_change = self._parse_finite_float(tv_row.get("change")) if tv_row else None
        tv_desc = str(tv_row.get("description") or "").strip() if tv_row else ""
        tv_pe = self._parse_positive_float(tv_row.get("price_earnings_ttm")) if tv_row else None
        tv_pb = (
            self._parse_positive_float(tv_row.get("price_book_ratio"))
            or self._parse_positive_float(tv_row.get("price_book_fq"))
            if tv_row
            else None
        )
        tv_h52 = self._parse_positive_float(tv_row.get("price_52_week_high")) if tv_row else None
        tv_l52 = self._parse_positive_float(tv_row.get("price_52_week_low")) if tv_row else None
        tv_div_yield = (
            self._parse_positive_float(tv_row.get("dividends_yield_current")) if tv_row else None
        )
        tv_last_div = (
            self._parse_positive_float(tv_row.get("dps_common_stock_prim_issue_fy"))
            if tv_row
            else None
        )
        tv_sector = str(tv_row.get("sector") or "").strip() if tv_row else ""
        tv_rsi = self._parse_positive_float(tv_row.get("RSI")) if tv_row else None
        tv_rec_all = self._parse_finite_float(tv_row.get("Recommend.All")) if tv_row else None

        has_tv_fundamentals = any(
            v is not None for v in (tv_pe, tv_pb, tv_h52, tv_div_yield)
        )
        should_query_yf = include_news or tv_price is None or not has_tv_fundamentals

        current_price = 0.0
        daily_change_pct = 0.0
        yf_company_name: Optional[str] = None
        pe_ratio: Optional[float] = None
        pb_ratio: Optional[float] = None
        high_52w: Optional[float] = None
        low_52w: Optional[float] = None
        dividend_yield_pct: Optional[float] = None
        last_dividend_value: Optional[float] = None
        yf_sector: Optional[str] = None
        closes: list[float] = []

        if should_query_yf:
            try:
                ticker = yf.Ticker(yf_symbol)
                hist = ticker.history(period="3mo")
                if not hist.empty and "Close" in hist.columns:
                    closes = [float(x) for x in hist["Close"].dropna().tolist()]
                    if closes:
                        current_price = round(closes[-1], 2)
                    if len(closes) >= 2 and closes[-2] > 0:
                        daily_change_pct = round(
                            ((closes[-1] - closes[-2]) / closes[-2]) * 100, 2
                        )

                info: dict = {}
                try:
                    info = ticker.info or {}
                except Exception as info_exc:
                    logger.debug("yfinance info okunamadı (%s): %s", yf_symbol, info_exc)

                if current_price <= 0:
                    fast = getattr(ticker, "fast_info", None)
                    if fast:
                        last_p = fast.get("last_price") or fast.get("lastPrice")
                        prev_p = fast.get("previous_close") or fast.get("previousClose")
                        if last_p:
                            current_price = round(float(last_p), 2)
                        if last_p and prev_p and float(prev_p) > 0:
                            daily_change_pct = round(
                                ((float(last_p) - float(prev_p)) / float(prev_p)) * 100, 2
                            )

                yf_company_name = str(
                    info.get("shortName") or info.get("longName") or ""
                ).strip() or None

                raw_pe = info.get("trailingPE") or info.get("forwardPE")
                if raw_pe and float(raw_pe) > 0:
                    pe_ratio = round(float(raw_pe), 2)

                raw_pb = info.get("priceToBook")
                if raw_pb and float(raw_pb) > 0:
                    pb_ratio = round(float(raw_pb), 2)

                raw_h52 = info.get("fiftyTwoWeekHigh")
                raw_l52 = info.get("fiftyTwoWeekLow")
                if raw_h52 and float(raw_h52) > 0:
                    high_52w = round(float(raw_h52), 2)
                if raw_l52 and float(raw_l52) > 0:
                    low_52w = round(float(raw_l52), 2)

                raw_div_yield = self._parse_positive_float(info.get("dividendYield"))
                if raw_div_yield is None:
                    raw_div_yield = self._parse_positive_float(
                        info.get("trailingAnnualDividendYield")
                    )
                if raw_div_yield is not None:
                    normalized_div = (
                        raw_div_yield * 100.0 if 0 < raw_div_yield < 1 else raw_div_yield
                    )
                    dividend_yield_pct = round(normalized_div, 2)

                raw_last_div = self._parse_positive_float(info.get("lastDividendValue"))
                if raw_last_div is not None:
                    last_dividend_value = (
                        round(raw_last_div, 2)
                        if raw_last_div >= 0.01
                        else round(raw_last_div, 4)
                    )

                raw_sector = info.get("sector")
                if isinstance(raw_sector, str) and raw_sector.strip():
                    yf_sector = raw_sector.strip()
            except Exception as exc:
                logger.warning("Yahoo Finance piyasa verisi çekilemedi (%s): %s", yf_symbol, exc)

        # TradingView Scanner yedek/birincil zenginleştirmesi (özellikle Yahoo 429 durumlarında)
        if current_price <= 0 and tv_price is not None:
            current_price = round(tv_price, 2)
        if daily_change_pct == 0.0 and tv_change is not None:
            daily_change_pct = round(tv_change, 2)
        if pe_ratio is None and tv_pe is not None:
            pe_ratio = round(tv_pe, 2)
        if pb_ratio is None and tv_pb is not None:
            pb_ratio = round(tv_pb, 2)
        if high_52w is None and tv_h52 is not None:
            high_52w = round(tv_h52, 2)
        if low_52w is None and tv_l52 is not None:
            low_52w = round(tv_l52, 2)
        if dividend_yield_pct is None and tv_div_yield is not None:
            dividend_yield_pct = round(tv_div_yield, 2)
        if last_dividend_value is None and tv_last_div is not None:
            last_dividend_value = (
                round(tv_last_div, 2) if tv_last_div >= 0.01 else round(tv_last_div, 4)
            )
        if not yf_sector and tv_sector:
            yf_sector = tv_sector

        company_name = self._resolve_company_name(
            clean_symbol, yf_name=yf_company_name, tv_description=tv_desc
        )
        sector_tr = self._resolve_sector_tr(clean_symbol, yf_sector)

        drawdown_pct: Optional[float] = None
        if current_price > 0 and high_52w and high_52w > 0:
            drawdown_pct = round(((current_price - high_52w) / high_52w) * 100, 2)

        if not include_news and tv_rec_all is not None and -1.0 <= tv_rec_all <= 1.0:
            computed = Compute.Recommend(tv_rec_all)
            tv_rec = _TV_RECOMMENDATION_MAP.get(computed, "NÖTR")
            tv_buy, tv_neutral, tv_sell = 0, 0, 0
            rsi = round(tv_rsi, 2) if tv_rsi is not None else None
        else:
            tv_rec, tv_buy, tv_neutral, tv_sell, rsi = self._fetch_tradingview_summary(clean_symbol)
            if tv_rec == "BİLİNMİYOR" and tv_rec_all is not None and -1.0 <= tv_rec_all <= 1.0:
                computed = Compute.Recommend(tv_rec_all)
                tv_rec = _TV_RECOMMENDATION_MAP.get(computed, "BİLİNMİYOR")
            if rsi is None and tv_rsi is not None:
                rsi = round(tv_rsi, 2)

        if rsi is None and closes:
            rsi = self._compute_rsi_from_closes(closes)
        if tv_rec == "BİLİNMİYOR" and len(closes) >= 20:
            sma20 = sum(closes[-20:]) / 20.0
            if current_price > sma20 * 1.02:
                tv_rec = "AL (Trend Yukarı)"
            elif current_price < sma20 * 0.98:
                tv_rec = "SAT (Trend Aşağı)"
            else:
                tv_rec = "NÖTR"

        plain_tech = build_plain_technical_comment(
            tv_recommendation=tv_rec,
            tv_buy_count=tv_buy,
            tv_neutral_count=tv_neutral,
            tv_sell_count=tv_sell,
            rsi=rsi,
        )
        plain_val = build_plain_valuation_comment(
            pe_ratio=pe_ratio,
            pb_ratio=pb_ratio,
            drawdown_from_high_pct=drawdown_pct,
        )
        news = self.fetch_recent_news(clean_symbol, company_name) if include_news else []

        snapshot = MarketSnapshot(
            symbol=clean_symbol,
            company_name=company_name,
            current_price=current_price,
            daily_change_pct=daily_change_pct,
            pe_ratio=pe_ratio,
            pb_ratio=pb_ratio,
            high_52w=high_52w,
            low_52w=low_52w,
            drawdown_from_high_pct=drawdown_pct,
            dividend_yield_pct=dividend_yield_pct,
            last_dividend_value=last_dividend_value,
            sector_tr=sector_tr,
            tv_recommendation=tv_rec,
            tv_buy_count=tv_buy,
            tv_neutral_count=tv_neutral,
            tv_sell_count=tv_sell,
            rsi=rsi,
            plain_technical_comment=plain_tech,
            plain_valuation_comment=plain_val,
            recent_news=news,
        )
        has_fundamentals = any(
            v is not None for v in (pe_ratio, pb_ratio, high_52w, dividend_yield_pct)
        )
        if current_price > 0 and has_fundamentals:
            with self._cache_lock:
                self._snapshot_cache[(clean_symbol, include_news)] = (time.monotonic(), snapshot)
        return snapshot

    def get_price_history(self, symbol: str, period: str = "6mo") -> list[tuple[str, float]]:
        """Yahoo Finance üzerinden belirtilen dönem için (YYYY-MM-DD, kapanış) fiyat geçmişi döner."""
        clean_symbol = self.normalize_bist_symbol(symbol)
        if not clean_symbol:
            return []

        yf_symbol = f"{clean_symbol}.IS"
        try:
            ticker = yf.Ticker(yf_symbol)
            hist = ticker.history(period=period)
            if hist is None or hist.empty or "Close" not in hist.columns:
                return []

            history: list[tuple[str, float]] = []
            for idx, close_val in hist["Close"].dropna().items():
                price = self._parse_positive_float(close_val)
                if price is None:
                    continue
                date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                history.append((date_str, round(price, 2)))
            return history
        except Exception as exc:
            logger.warning("Yahoo Finance fiyat geçmişi çekilemedi (%s): %s", yf_symbol, exc)
            return []
