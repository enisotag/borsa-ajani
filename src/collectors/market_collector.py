"""BIST hisse fiyatları, temel çarpanlar, hazır teknik özetler ve haber toplayıcısı."""

from __future__ import annotations

import logging
import math
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import httpx
import yfinance as yf
from tradingview_ta import Interval, TA_Handler

from src.storage.models import MarketSnapshot

from .sector_collector import get_sector_and_peers

logger = logging.getLogger(__name__)

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
}

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

    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self.timeout_seconds = timeout_seconds

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
        yf_symbol = f"{clean_symbol}.IS"

        current_price = 0.0
        daily_change_pct = 0.0
        company_name = clean_symbol
        pe_ratio: Optional[float] = None
        pb_ratio: Optional[float] = None
        high_52w: Optional[float] = None
        low_52w: Optional[float] = None
        dividend_yield_pct: Optional[float] = None
        last_dividend_value: Optional[float] = None
        yf_sector: Optional[str] = None
        closes: list[float] = []

        try:
            ticker = yf.Ticker(yf_symbol)
            hist = ticker.history(period="3mo")
            if not hist.empty and "Close" in hist.columns:
                closes = [float(x) for x in hist["Close"].dropna().tolist()]
                if closes:
                    current_price = round(closes[-1], 2)
                if len(closes) >= 2 and closes[-2] > 0:
                    daily_change_pct = round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2)

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
                        daily_change_pct = round(((float(last_p) - float(prev_p)) / float(prev_p)) * 100, 2)

            company_name = str(
                info.get("shortName") or info.get("longName") or clean_symbol
            ).strip()

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
                raw_div_yield = self._parse_positive_float(info.get("trailingAnnualDividendYield"))
            if raw_div_yield is not None:
                normalized_div = raw_div_yield * 100.0 if 0 < raw_div_yield < 1 else raw_div_yield
                dividend_yield_pct = round(normalized_div, 2)

            raw_last_div = self._parse_positive_float(info.get("lastDividendValue"))
            if raw_last_div is not None:
                last_dividend_value = (
                    round(raw_last_div, 2) if raw_last_div >= 0.01 else round(raw_last_div, 4)
                )

            raw_sector = info.get("sector")
            if isinstance(raw_sector, str) and raw_sector.strip():
                yf_sector = raw_sector.strip()
        except Exception as exc:
            logger.warning("Yahoo Finance piyasa verisi çekilemedi (%s): %s", yf_symbol, exc)

        sector_tr = self._resolve_sector_tr(clean_symbol, yf_sector)

        drawdown_pct: Optional[float] = None
        if current_price > 0 and high_52w and high_52w > 0:
            drawdown_pct = round(((current_price - high_52w) / high_52w) * 100, 2)

        tv_rec, tv_buy, tv_neutral, tv_sell, rsi = self._fetch_tradingview_summary(clean_symbol)

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

        return MarketSnapshot(
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
