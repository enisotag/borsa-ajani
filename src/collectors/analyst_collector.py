"""Aracı kurum hedef fiyat raporları, model portföyler ve analist konsensüsü toplayıcısı."""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import httpx
import yfinance as yf
from bs4 import BeautifulSoup

from src.config import Settings, get_settings
from src.storage.models import AnalystConsensus, AnalystReport

from .market_collector import fetch_tradingview_scanner_data, get_tv_scanner_row

logger = logging.getLogger(__name__)

_BUY_KEYWORDS = (
    "al",
    "buy",
    "strong_buy",
    "outperform",
    "overweight",
    "endeks üstü",
    "endeksin üzerinde",
    "ekle",
    "model portföy",
)
_SELL_KEYWORDS = (
    "sat",
    "sell",
    "underperform",
    "underweight",
    "endeks altı",
    "endeksin altında",
    "azalt",
)


def normalize_recommendation(raw_text: str) -> str:
    """Türkçe/İngilizce aracı kurum tavsiyelerini AL, TUT veya SAT olarak standartlaştırır."""
    lowered = raw_text.strip().lower()
    if any(k in lowered for k in _SELL_KEYWORDS):
        return "SAT"
    if any(k in lowered for k in _BUY_KEYWORDS):
        return "AL"
    return "TUT"


def _parse_tr_float(text: str) -> Optional[float]:
    """'345,50 TL' veya '345.50' gibi Türkçe/İngilizce sayı metnini float'a çevirir."""
    cleaned = re.sub(r"[^\d,.\-]", "", text.strip())
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        return None


class AnalystCollector:
    """Yerel web hedef fiyat tabloları ve Yahoo Finance verilerinden analist konsensüsü üretir."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        timeout_seconds: float = 6.0,
        cache_ttl_seconds: float = 900.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache_lock = threading.Lock()
        self._web_reports_cache: Optional[tuple[float, list[AnalystReport]]] = None
        self._consensus_cache: dict[str, tuple[float, AnalystConsensus]] = {}

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.strip().upper().replace(".IS", "")

    def _fetch_web_target_reports(self, symbol: Optional[str] = None) -> list[AnalystReport]:
        """Halka açık BIST hedef fiyat web sayfasından aracı kurum raporlarını önbellekli toplar."""
        now_ts = time.monotonic()
        cached_reports: Optional[list[AnalystReport]] = None
        with self._cache_lock:
            if (
                self._web_reports_cache is not None
                and (now_ts - self._web_reports_cache[0]) < self.cache_ttl_seconds
            ):
                cached_reports = self._web_reports_cache[1]

        if cached_reports is None:
            urls = ["https://hedeffiyat.com.tr/"]
            reports: list[AnalystReport] = []
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            }

            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                for url in urls:
                    try:
                        resp = client.get(url, headers=headers)
                        if resp.status_code != 200:
                            continue
                        parsed_items = self._parse_html_target_tables(
                            resp.text, source_url=url, target_symbol=None
                        )
                        if parsed_items:
                            reports.extend(parsed_items)
                    except Exception as exc:
                        logger.debug("Web hedef fiyat sayfası okunamadı (%s): %s", url, exc)

            unique: dict[str, AnalystReport] = {}
            for report in reports:
                unique[report.report_id] = report
            cached_reports = list(unique.values())
            with self._cache_lock:
                self._web_reports_cache = (time.monotonic(), cached_reports)

        if symbol:
            clean_sym = self._normalize_symbol(symbol)
            return [r for r in cached_reports if r.symbol == clean_sym]
        return list(cached_reports)

    def _parse_html_target_tables(
        self,
        html: str,
        source_url: str,
        target_symbol: Optional[str] = None,
    ) -> list[AnalystReport]:
        """HTML tablo satırlarından hisse kodu, kurum, hedef fiyat ve tavsiye ayrıştırır."""
        soup = BeautifulSoup(html, "lxml")
        reports: list[AnalystReport] = []
        clean_filter = self._normalize_symbol(target_symbol) if target_symbol else None
        today_str = datetime.now().strftime("%Y-%m-%d")

        for row in soup.select("table tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            row_text = " | ".join(cells)
            symbol_match = re.search(r"\b([A-Z]{4,6})\b", cells[0].upper())
            sym = symbol_match.group(1) if symbol_match else (clean_filter or "")
            if not sym or (clean_filter and sym != clean_filter):
                continue

            # Sayısal hedef fiyat ara (genelde 2. veya 3. sütunlarda)
            target_price: Optional[float] = None
            for cell in cells[1:]:
                if "tl" in cell.lower() or re.match(r"^\d+[.,]\d{1,2}$", cell.strip()):
                    candidate = _parse_tr_float(cell)
                    if candidate and 1.0 <= candidate <= 100000.0:
                        target_price = candidate
                        break

            if not target_price:
                continue

            institution = "Aracı Kurum"
            for cell in cells[1:]:
                if any(
                    word in cell.lower()
                    for word in ("yatırım", "menkul", "finans", "bank", "kapital", "integral", "gedik", "deniz", "iş", "ak ", "yapı kredi", "garanti", "oyak", "tacirler", "info", "qnb", "teb")
                ):
                    institution = cell[:60]
                    break

            rec = normalize_recommendation(row_text)
            date_match = re.search(r"\b(\d{2}[./-]\d{2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})\b", row_text)
            report_date = date_match.group(1) if date_match else today_str

            reports.append(
                AnalystReport(
                    symbol=sym,
                    institution=institution,
                    target_price=target_price,
                    recommendation=rec,
                    report_date=report_date,
                    source=source_url,
                )
            )

        return reports

    def _fetch_yfinance_analyst_data(
        self, symbol: str, current_price: Optional[float] = None
    ) -> tuple[float, float, float, float, int, int, int, int, list[AnalystReport]]:
        """Yahoo Finance üzerinden analist hedef fiyat özetini ve kurum derecelendirmelerini çeker."""
        clean_sym = self._normalize_symbol(symbol)
        yf_symbol = f"{clean_sym}.IS"

        price = current_price or 0.0
        avg_tp = 0.0
        high_tp = 0.0
        low_tp = 0.0
        total_opinions = 0
        buy_count = 0
        hold_count = 0
        sell_count = 0
        reports: list[AnalystReport] = []

        try:
            ticker = yf.Ticker(yf_symbol)
            info = ticker.info or {}

            if price <= 0:
                price = float(
                    info.get("currentPrice")
                    or info.get("regularMarketPrice")
                    or info.get("previousClose")
                    or 0.0
                )

            avg_tp = float(info.get("targetMeanPrice") or 0.0)
            high_tp = float(info.get("targetHighPrice") or avg_tp or 0.0)
            low_tp = float(info.get("targetLowPrice") or avg_tp or 0.0)
            total_opinions = int(info.get("numberOfAnalystOpinions") or 0)
            rec_key = str(info.get("recommendationKey") or "").lower()

            # Kurum bazlı öneri dağılımı (recommendations / recommendations_summary)
            try:
                rec_df = ticker.recommendations
                if rec_df is not None and not rec_df.empty:
                    latest = rec_df.iloc[0]
                    strong_buy = int(latest.get("strongBuy", 0) or 0)
                    buy = int(latest.get("buy", 0) or 0)
                    hold = int(latest.get("hold", 0) or 0)
                    sell = int(latest.get("sell", 0) or 0)
                    strong_sell = int(latest.get("strongSell", 0) or 0)
                    buy_count = strong_buy + buy
                    hold_count = hold
                    sell_count = sell + strong_sell
            except Exception as rec_exc:
                logger.debug("yfinance recommendations okunamadı (%s): %s", yf_symbol, rec_exc)

            # Eğer recommendations tablosu boşsa recommendationKey ve opinion sayısından tahmin yürüt
            if (buy_count + hold_count + sell_count) == 0 and total_opinions > 0:
                norm_rec = normalize_recommendation(rec_key)
                if norm_rec == "AL":
                    buy_count = total_opinions
                elif norm_rec == "SAT":
                    sell_count = total_opinions
                else:
                    hold_count = total_opinions

            # Tekil kurum aksiyonları (upgrades_downgrades)
            try:
                up_df = ticker.upgrades_downgrades
                if up_df is not None and not up_df.empty:
                    for idx, row in up_df.head(6).iterrows():
                        firm = str(row.get("Firm", "Kurum")).strip()
                        grade = str(row.get("ToGrade") or row.get("Action") or "TUT")
                        target_val = row.get("currentPriceTarget")
                        tp = float(target_val) if target_val and float(target_val) > 0 else avg_tp
                        if tp <= 0:
                            continue
                        upside = round(((tp - price) / price) * 100, 2) if price > 0 else 0.0
                        date_str = str(idx)[:10]
                        reports.append(
                            AnalystReport(
                                symbol=clean_sym,
                                institution=firm,
                                target_price=round(tp, 2),
                                current_price=round(price, 2) if price > 0 else None,
                                upside_percent=upside,
                                recommendation=normalize_recommendation(grade),
                                report_date=date_str,
                                source="Yahoo Finance",
                            )
                        )
            except Exception as up_exc:
                logger.debug("yfinance upgrades_downgrades okunamadı (%s): %s", yf_symbol, up_exc)

            # Eğer tekil rapor listesi boş ama ortalama hedef fiyat varsa konsensüs özet raporu oluştur
            if not reports and avg_tp > 0:
                upside = round(((avg_tp - price) / price) * 100, 2) if price > 0 else 0.0
                reports.append(
                    AnalystReport(
                        symbol=clean_sym,
                        institution="Analist Konsensüsü",
                        target_price=round(avg_tp, 2),
                        current_price=round(price, 2) if price > 0 else None,
                        upside_percent=upside,
                        recommendation=normalize_recommendation(rec_key),
                        report_date=datetime.now().strftime("%Y-%m-%d"),
                        source="Yahoo Finance Konsensüs",
                    )
                )
        except Exception as exc:
            logger.warning("Yahoo Finance analist verisi alınamadı (%s): %s", yf_symbol, exc)

        return (
            round(price, 2),
            round(avg_tp, 2),
            round(high_tp, 2),
            round(low_tp, 2),
            total_opinions,
            buy_count,
            hold_count,
            sell_count,
            reports,
        )

    @staticmethod
    def _to_pos_float(value: object) -> float:
        if value is None or isinstance(value, bool):
            return 0.0
        try:
            num = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return num if math.isfinite(num) and num > 0 else 0.0

    @staticmethod
    def _to_nonneg_int(value: object) -> int:
        if value is None or isinstance(value, bool):
            return 0
        try:
            num = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        return int(num) if math.isfinite(num) and num > 0 else 0

    def _fetch_tv_consensus_data(
        self, symbol: str
    ) -> tuple[float, float, float, float, int, int, int, int, list[AnalystReport]]:
        """TradingView Turkey Scanner üzerinden analist konsensüsü ve hedef fiyat verisi çeker."""
        clean_sym = self._normalize_symbol(symbol)
        tv_row = get_tv_scanner_row(
            clean_sym,
            timeout_seconds=self.timeout_seconds,
            cache_ttl_seconds=min(300.0, self.cache_ttl_seconds),
        )
        if not tv_row:
            return 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, []

        price = self._to_pos_float(tv_row.get("close"))
        avg_tp = self._to_pos_float(tv_row.get("price_target_average"))
        high_tp = self._to_pos_float(tv_row.get("price_target_high")) or avg_tp
        low_tp = self._to_pos_float(tv_row.get("price_target_low")) or avg_tp
        buy_count = self._to_nonneg_int(tv_row.get("recommendation_buy")) + self._to_nonneg_int(
            tv_row.get("recommendation_over")
        )
        hold_count = self._to_nonneg_int(tv_row.get("recommendation_hold"))
        sell_count = self._to_nonneg_int(
            tv_row.get("recommendation_sell")
        ) + self._to_nonneg_int(tv_row.get("recommendation_under"))
        total_opinions = max(
            self._to_nonneg_int(tv_row.get("recommendation_total")),
            buy_count + hold_count + sell_count,
        )

        reports: list[AnalystReport] = []
        if avg_tp > 0:
            if buy_count >= hold_count and buy_count >= sell_count and buy_count > 0:
                rec_label = "AL"
            elif sell_count > buy_count and sell_count >= hold_count:
                rec_label = "SAT"
            else:
                rec_label = "TUT"
            upside = round(((avg_tp - price) / price) * 100, 2) if price > 0 else 0.0
            reports.append(
                AnalystReport(
                    symbol=clean_sym,
                    institution="Analist Konsensüsü (TradingView)",
                    target_price=round(avg_tp, 2),
                    current_price=round(price, 2) if price > 0 else None,
                    upside_percent=upside,
                    recommendation=rec_label,
                    report_date=datetime.now().strftime("%Y-%m-%d"),
                    source="TradingView Konsensüs",
                )
            )

        return (
            round(price, 2),
            round(avg_tp, 2),
            round(high_tp, 2),
            round(low_tp, 2),
            total_opinions,
            buy_count,
            hold_count,
            sell_count,
            reports,
        )

    def get_consensus(
        self, symbol: str, current_price: Optional[float] = None
    ) -> AnalystConsensus:
        """TradingView, yerel web tabloları ve Yahoo Finance verilerini birleştirerek konsensüs döner."""
        clean_sym = self._normalize_symbol(symbol)
        now_ts = time.monotonic()
        with self._cache_lock:
            cached = self._consensus_cache.get(clean_sym)
            if (
                cached
                and (now_ts - cached[0]) < self.cache_ttl_seconds
                and (cached[1].avg_target_price > 0 or cached[1].total_analysts > 0)
            ):
                cached_cons = cached[1]
                if current_price and current_price > 0 and cached_cons.avg_target_price > 0:
                    new_upside = round(
                        ((cached_cons.avg_target_price - current_price) / current_price) * 100,
                        2,
                    )
                    return cached_cons.model_copy(
                        update={
                            "current_price": round(current_price, 2),
                            "upside_percent": new_upside,
                        }
                    )
                return cached_cons

        (
            tv_price,
            tv_avg_tp,
            tv_high_tp,
            tv_low_tp,
            tv_total,
            tv_buy,
            tv_hold,
            tv_sell,
            tv_reports,
        ) = self._fetch_tv_consensus_data(clean_sym)

        has_tv_consensus = tv_avg_tp > 0 and (tv_buy + tv_hold + tv_sell) > 0
        if has_tv_consensus:
            yf_price = 0.0
            yf_avg_tp = 0.0
            yf_high_tp = 0.0
            yf_low_tp = 0.0
            yf_total = 0
            yf_buy = 0
            yf_hold = 0
            yf_sell = 0
            yf_reports: list[AnalystReport] = []
        else:
            (
                yf_price,
                yf_avg_tp,
                yf_high_tp,
                yf_low_tp,
                yf_total,
                yf_buy,
                yf_hold,
                yf_sell,
                yf_reports,
            ) = self._fetch_yfinance_analyst_data(clean_sym, current_price=current_price)

        web_reports = self._fetch_web_target_reports(clean_sym)
        price = current_price if (current_price and current_price > 0) else (tv_price or yf_price)

        avg_tp = tv_avg_tp if tv_avg_tp > 0 else yf_avg_tp
        high_tp = tv_high_tp if tv_high_tp > 0 else yf_high_tp
        low_tp = tv_low_tp if tv_low_tp > 0 else yf_low_tp
        if (tv_buy + tv_hold + tv_sell) > 0:
            buy_count, hold_count, sell_count, total_opinions = tv_buy, tv_hold, tv_sell, tv_total
        else:
            buy_count, hold_count, sell_count, total_opinions = yf_buy, yf_hold, yf_sell, yf_total

        # Web raporlarının getiri potansiyelini güncel fiyatla güncelle
        enriched_web_reports: list[AnalystReport] = []
        for rep in web_reports:
            upside = round(((rep.target_price - price) / price) * 100, 2) if price > 0 else 0.0
            enriched_web_reports.append(
                rep.model_copy(
                    update={
                        "current_price": price if price > 0 else None,
                        "upside_percent": upside,
                    }
                )
            )

        all_reports = enriched_web_reports + yf_reports
        if not all_reports and tv_reports:
            for rep in tv_reports:
                upside = round(((rep.target_price - price) / price) * 100, 2) if price > 0 else 0.0
                all_reports.append(
                    rep.model_copy(
                        update={
                            "current_price": price if price > 0 else None,
                            "upside_percent": upside,
                        }
                    )
                )

        # Eğer harici konsensüs hedef ortalaması yoksa yerel web raporlarından hesapla
        if avg_tp <= 0 and enriched_web_reports:
            prices = [r.target_price for r in enriched_web_reports]
            avg_tp = round(sum(prices) / len(prices), 2)
            high_tp = round(max(prices), 2)
            low_tp = round(min(prices), 2)

        # Kurum tavsiye dağılımını yerel raporlarla zenginleştir
        if (buy_count + hold_count + sell_count) == 0 and all_reports:
            buy_count = sum(1 for r in all_reports if r.recommendation == "AL")
            hold_count = sum(1 for r in all_reports if r.recommendation == "TUT")
            sell_count = sum(1 for r in all_reports if r.recommendation == "SAT")

        total_analysts = max(total_opinions, buy_count + hold_count + sell_count, len(all_reports))
        upside_percent = round(((avg_tp - price) / price) * 100, 2) if (price > 0 and avg_tp > 0) else 0.0
        in_model_portfolios = sum(
            1 for r in enriched_web_reports if "model portföy" in r.institution.lower() or "model" in r.source.lower()
        )

        consensus = AnalystConsensus(
            symbol=clean_sym,
            current_price=round(price, 2),
            avg_target_price=round(avg_tp, 2),
            max_target_price=round(high_tp, 2),
            min_target_price=round(low_tp, 2),
            upside_percent=upside_percent,
            buy_count=buy_count,
            hold_count=hold_count,
            sell_count=sell_count,
            total_analysts=total_analysts,
            recent_reports=all_reports[:8],
            in_model_portfolios=in_model_portfolios,
        )
        if consensus.avg_target_price > 0 or consensus.total_analysts > 0:
            with self._cache_lock:
                self._consensus_cache[clean_sym] = (time.monotonic(), consensus)
        return consensus

    def should_notify_report(
        self, consensus: AnalystConsensus, is_in_portfolio: bool
    ) -> bool:
        """Portföydeki hisseler için her raporu, dışındakiler için ise fırsat filtresini uygular."""
        if is_in_portfolio:
            return True
        return (
            consensus.upside_percent >= self.settings.min_upside_percent
            and consensus.buy_count >= self.settings.min_buy_count
        )

    def fetch_latest_reports(self, symbols: Optional[list[str]] = None) -> list[AnalystReport]:
        """Web hedef fiyat kaynaklarından ve gerekirse takip listesinden son raporları çeker."""
        reports = self._fetch_web_target_reports(symbol=None)
        if symbols:
            allowed = [self._normalize_symbol(s) for s in symbols]
            allowed_set = set(allowed)
            filtered = [r for r in reports if r.symbol in allowed_set]
            if filtered:
                return filtered
            # Web kaynaklarından veri dönmezse ilk birkaç sembol için konsensüsten paralel topla
            collected: list[AnalystReport] = []
            sample_symbols = allowed[:10]
            fetch_tradingview_scanner_data(
                sample_symbols,
                timeout_seconds=self.timeout_seconds,
                cache_ttl_seconds=min(300.0, self.cache_ttl_seconds),
            )
            with ThreadPoolExecutor(max_workers=4) as pool:
                for cons in pool.map(self.get_consensus, sample_symbols):
                    collected.extend(cons.recent_reports)
            return collected
        return reports

    def _evaluate_opportunity(
        self, clean_sym: str, owned_or_watched: set[str]
    ) -> Optional[AnalystConsensus]:
        try:
            consensus = self.get_consensus(clean_sym)
            if consensus.avg_target_price <= 0 or consensus.current_price <= 0:
                return None
            is_watched = clean_sym in owned_or_watched
            if (
                consensus.upside_percent >= self.settings.min_upside_percent
                and consensus.buy_count >= self.settings.min_buy_count
            ):
                return consensus
            if is_watched and consensus.upside_percent >= self.settings.min_upside_percent:
                return consensus
        except Exception as exc:
            logger.debug("Fırsat taraması sırasında hata (%s): %s", clean_sym, exc)
        return None

    def scan_opportunities(
        self,
        universe: Optional[list[str]] = None,
        portfolio_symbols: Optional[set[str]] = None,
        limit: int = 8,
    ) -> list[AnalystConsensus]:
        """BIST takip evreninde en az %35 potansiyelli ve çoklu AL tavsiyeli fırsatları paralel tarar."""
        target_universe = [self._normalize_symbol(s) for s in (universe or self.settings.bist_universe)]
        owned_or_watched = {self._normalize_symbol(s) for s in (portfolio_symbols or set())}

        fetch_tradingview_scanner_data(
            target_universe,
            timeout_seconds=self.timeout_seconds,
            cache_ttl_seconds=min(300.0, self.cache_ttl_seconds),
        )

        opportunities: list[AnalystConsensus] = []
        max_workers = min(4, max(1, len(target_universe)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = pool.map(
                lambda sym: self._evaluate_opportunity(sym, owned_or_watched),
                target_universe,
            )
            for item in results:
                if item is not None:
                    opportunities.append(item)

        opportunities.sort(key=lambda item: item.upside_percent, reverse=True)
        return opportunities[:limit]
