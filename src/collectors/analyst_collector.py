"""Aracı kurum hedef fiyat raporları, model portföyler ve analist konsensüsü toplayıcısı."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import httpx
import yfinance as yf
from bs4 import BeautifulSoup

from src.config import Settings, get_settings
from src.storage.models import AnalystConsensus, AnalystReport

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
        timeout_seconds: float = 8.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.strip().upper().replace(".IS", "")

    def _fetch_web_target_reports(self, symbol: Optional[str] = None) -> list[AnalystReport]:
        """Halka açık BIST hedef fiyat web sayfalarından aracı kurum raporlarını toplar."""
        urls = [
            "https://hedeffiyat.com.tr/",
            "https://www.hisseonerileri.com/hedef-fiyatlar/",
        ]
        if symbol:
            clean_sym = self._normalize_symbol(symbol)
            urls.insert(0, f"https://hedeffiyat.com.tr/hisse/{clean_sym.lower()}/")

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
                        resp.text, source_url=url, target_symbol=symbol
                    )
                    if parsed_items:
                        reports.extend(parsed_items)
                        if symbol and len(reports) >= 3:
                            break
                except Exception as exc:
                    logger.debug("Web hedef fiyat sayfası okunamadı (%s): %s", url, exc)

        # Tekrarlanan raporları report_id üzerinden temizle
        unique: dict[str, AnalystReport] = {}
        for report in reports:
            unique[report.report_id] = report
        return list(unique.values())

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

    def get_consensus(
        self, symbol: str, current_price: Optional[float] = None
    ) -> AnalystConsensus:
        """Yerel web tabloları ve Yahoo Finance verilerini birleştirerek analist konsensüsü döner."""
        clean_sym = self._normalize_symbol(symbol)
        (
            yf_price,
            avg_tp,
            high_tp,
            low_tp,
            total_opinions,
            buy_count,
            hold_count,
            sell_count,
            yf_reports,
        ) = self._fetch_yfinance_analyst_data(clean_sym, current_price=current_price)

        web_reports = self._fetch_web_target_reports(clean_sym)
        price = current_price if (current_price and current_price > 0) else yf_price

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

        # Eğer Yahoo Finance hedef ortalaması yoksa yerel web raporlarından hesapla
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

        return AnalystConsensus(
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
            allowed = {self._normalize_symbol(s) for s in symbols}
            filtered = [r for r in reports if r.symbol in allowed]
            if filtered:
                return filtered
            # Web kaynaklarından veri dönmezse ilk birkaç sembol için konsensüsten topla
            collected: list[AnalystReport] = []
            for sym in list(allowed)[:10]:
                consensus = self.get_consensus(sym)
                collected.extend(consensus.recent_reports)
            return collected
        return reports

    def scan_opportunities(
        self,
        universe: Optional[list[str]] = None,
        portfolio_symbols: Optional[set[str]] = None,
        limit: int = 8,
    ) -> list[AnalystConsensus]:
        """BIST takip evreninde en az %35 potansiyelli ve çoklu AL tavsiyeli fırsatları tarar."""
        target_universe = universe or self.settings.bist_universe
        owned_or_watched = {self._normalize_symbol(s) for s in (portfolio_symbols or set())}

        opportunities: list[AnalystConsensus] = []
        for sym in target_universe:
            clean_sym = self._normalize_symbol(sym)
            try:
                consensus = self.get_consensus(clean_sym)
                if consensus.avg_target_price <= 0 or consensus.current_price <= 0:
                    continue
                is_watched = clean_sym in owned_or_watched
                # Fırsat radarı: Enflasyon üstü potansiyel (>= %35) + Çoklu AL (>= 2)
                if (
                    consensus.upside_percent >= self.settings.min_upside_percent
                    and consensus.buy_count >= self.settings.min_buy_count
                ):
                    opportunities.append(consensus)
                elif is_watched and consensus.upside_percent >= self.settings.min_upside_percent:
                    opportunities.append(consensus)
            except Exception as exc:
                logger.debug("Fırsat taraması sırasında hata (%s): %s", clean_sym, exc)

        opportunities.sort(key=lambda item: item.upside_percent, reverse=True)
        return opportunities[:limit]
