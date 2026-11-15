"""Google Gemini API (google-genai) ile sade ve karar odaklı BIST hisse yorumlayıcısı."""

from __future__ import annotations

import base64
import json
import logging
import math
import re
from typing import Any, Optional

from google import genai

from src.config import Settings, get_settings
from src.storage.models import (
    AnalystConsensus,
    AnalystReport,
    MarketSnapshot,
    PortfolioItem,
)

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """Sen Türkiye borsası (BIST) konusunda uzman, orta-uzun vadeli düşünen, vakti kısıtlı bir yatırımcıya rehberlik eden kişisel borsa asistanısın.
Kullanıcı teknik analiz grafiklerini ve karmaşık finansal terimleri yorumlamayı bilmiyor.
Bu yüzden:
1. Asla karmaşık teknik jargona (Fibonacci, Ichimoku, MACD kesişimi vb.) boğma; bunun yerine verilmiş hazır teknik ve temel özetleri "halk dilinde" sade cümlelerle anlat.
2. En büyük ağırlığı aracı kurumların hedef fiyat raporlarına, AL/TUT/SAT konsensüsüne ve getiri potansiyeline ver.
3. Yanıtını her zaman tam olarak şu 4 başlıkla yapılandır:
🎯 Aracı Kurumların Ortak Kararı ve Hedef Fiyat Potansiyeli
🏢 Şirket ve Teknik Görünümün "Halk Dilinde" Özeti
💼 Senin Portföy ve Maliyet Durumun
⚖️ Artılar, Riskler ve "Ne Yapılabilir?" Yorumu
4. Kısa, net, Telegram'da telefondan kolay okunacak uzunlukta Türkçe yaz. Yatırım tavsiyesi olmadığını tek kısa notla hatırlat."""

BULLETIN_SYSTEM_INSTRUCTION = """Sen Türkiye borsası (BIST) kapanış bülteni hazırlayan akıllı borsa asistanısın.
Teknik terimlere boğmadan, sade ve karar odaklı bir Türkçe ile gün sonu özetini hazırla.
Önce portföy/izleme listesini, ardından %35 üzeri hedef fiyat potansiyeli yakalayan fırsatları ve günün öne çıkan aracı kurum raporlarını özetle.
Telegram'da telefondan 1 dakikada okunacak netlikte yaz."""

COMPARISON_SYSTEM_INSTRUCTION = """Sen Türkiye borsası (BIST) konusunda uzman, orta-uzun vadeli düşünen bireysel yatırımcıya sade dille rehberlik eden kişisel borsa asistanısın.
Sana aynı sektörden veya seçilmiş hisselerden oluşan yan yana karşılaştırma verileri sunulacak.
Kurallar:
1. Karmaşık teknik jargona (Fibonacci, Ichimoku, MACD vb.) boğmadan, herkesin anlayacağı "halk dilinde" yaz.
2. Orta-uzun vadeli bir yatırımcı için hangi hissenin neden öne çıktığını (hedef fiyat potansiyeli, kurumların AL desteği, F/K ve PD/DD ucuzluğu, varsa temettü verimi ve hazır teknik durum) açıkça anlat.
3. Diğer hisselerin güçlü yanlarını ve dikkat edilmesi gereken riskleri kısaca özetle.
4. Sayısal tabloyu tek tek tekrar etmeden doğrudan yoruma odaklan; Telegram'da telefondan rahat okunacak kısalıkta Türkçe yaz.
5. En sona tek cümlelik "Yatırım tavsiyesi değildir." notu ekle."""

FREE_QA_SYSTEM_INSTRUCTION = """Sen Türkiye borsası (BIST) konusunda uzman, samimi ve sade dille konuşan kişisel borsa asistanısın.
Kullanıcının serbest sorusunu, sana verilen gerçek portföy verilerine ve güncel BIST fırsatlarına dayanarak yanıtla.
Kurallar:
1. Karmaşık finansal veya teknik terimler kullanma; teknik bilgisi olmayan bir yatırımcının anlayacağı sade Türkçe ile açıkla.
2. Yalnızca verilen portföy ve fırsat bağlamındaki verilere dayan; uydurma fiyat veya hedef verme, eksik bilgi varsa açıkça söyle.
3. Net, yol gösterici ve Telegram'da telefondan kolay okunacak uzunlukta yaz.
4. Yanıtın sonuna kısa bir "Bilgilendirme amaçlıdır, yatırım tavsiyesi değildir." notu ekle."""

PORTFOLIO_OCR_SYSTEM_INSTRUCTION = """Sen Borsa İstanbul (BIST) aracı kurum ve banka portföy ekran görüntülerini okuyan hassas bir veri çıkarma asistanısın.
Görseldeki hisse kodlarını, lot/adet miktarlarını ve ortalama alış maliyetlerini tespit et.
Güncel fiyat veya toplam değer yerine varsa mutlaka alış/birim maliyet sütununu al.
Yanıt olarak SADECE geçerli bir JSON dizisi döndür; başka hiçbir metin, açıklama veya yorum ekleme."""


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Sözlük veya nesne üzerinden alanı güvenli biçimde okur."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        val = obj.get(key, default)
        return default if val is None else val
    val = getattr(obj, key, default)
    return default if val is None else val


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Değeri sonlu float'a dönüştürür; geçersizse default döndürür."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
        return num if math.isfinite(num) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """Değeri tam sayıya dönüştürür; geçersizse default döndürür."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_locale_number(value: Any) -> Optional[float]:
    """Türkçe (285,50 / 1.250,50) veya İngilizce (285.50) sayı metnini float'a çevirir."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None
    if not isinstance(value, str):
        return None

    cleaned = re.sub(r"(?i)\b(?:tl|try|adet|lot)\b|₺|\s+", "", value.strip())
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        if cleaned.count(",") == 1:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")

    try:
        num = float(cleaned)
        return num if math.isfinite(num) else None
    except ValueError:
        return None


def _normalize_bist_symbol(raw_symbol: Any) -> str:
    """Hisse kodunu BIST formatına (.IS/.E olmadan büyük harf) normalize eder."""
    sym = str(raw_symbol or "").strip().upper()
    if sym.startswith("BIST:"):
        sym = sym[5:].strip()
    sym = sym.replace(".IS", "")
    if sym.endswith(".E"):
        sym = sym[:-2]
    return sym.strip()


class BorsaCommentator:
    """Google Gemini (`gemini-3.8-flash`) kullanarak BIST hisselerini sade Türkçe ile yorumlar."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._client: Optional[genai.Client] = None
        if self.settings.has_valid_gemini_key():
            try:
                self._client = genai.Client(api_key=self.settings.gemini_api_key)
            except Exception as exc:
                logger.warning("Gemini istemcisi başlatılamadı: %s", exc)

    def _call_gemini(self, prompt_text: str, system_instruction: str) -> Optional[str]:
        """Güncel google-genai Interactions API ile Gemini 3.8 Flash modelini çağırır."""
        if self._client is None:
            return None
        try:
            interaction = self._client.interactions.create(
                model=self.settings.gemini_model,
                system_instruction=system_instruction,
                input=prompt_text,
            )
            text = (interaction.output_text or "").strip()
            return text or None
        except Exception as exc:
            logger.warning("Gemini API çağrısı başarısız, yedek şablon kullanılacak: %s", exc)
            return None

    @staticmethod
    def _build_portfolio_context(
        portfolio_item: Optional[PortfolioItem],
        current_price: float,
        avg_target_price: float,
    ) -> str:
        if portfolio_item is None:
            return "Bu hisse şu an portföyünde veya izleme listende kayıtlı değil."
        if not portfolio_item.is_owned:
            return "Bu hisse İzleme Listende kayıtlı (lot/maliyet girilmemiş)."

        pnl = portfolio_item.calculate_pnl(
            current_price=current_price,
            target_price=avg_target_price if avg_target_price > 0 else None,
        )
        if pnl["total_cost"] is None or pnl["current_value"] is None:
            raw_cost = portfolio_item.lot * portfolio_item.avg_cost
            return (
                f"Elinde {portfolio_item.lot:g} lot, ortalama {portfolio_item.avg_cost:.2f} TL maliyetle bulunuyor "
                f"(Toplam maliyet: {raw_cost:,.2f} TL). Güncel piyasa fiyatı alınamadığı için anlık kâr/zarar hesaplanamadı."
            )

        pnl_tl = pnl["pnl_tl"] or 0.0
        pnl_pct = pnl["pnl_pct"] or 0.0
        sign = "+" if pnl_tl >= 0 else ""
        base_line = (
            f"Elinde {portfolio_item.lot:g} lot, ortalama {portfolio_item.avg_cost:.2f} TL maliyetle bulunuyor "
            f"(Toplam maliyet: {pnl['total_cost']:,.2f} TL, Güncel değer: {pnl['current_value']:,.2f} TL). "
            f"Şu anki durumun: {sign}{pnl_pct:.2f}% ({sign}{pnl_tl:,.2f} TL)."
        )
        if pnl["target_pnl_pct"] is not None and pnl["target_pnl_tl"] is not None:
            t_sign = "+" if pnl["target_pnl_tl"] >= 0 else ""
            base_line += (
                f" Analist ortalama hedef fiyatı ({avg_target_price:.2f} TL) görülürse, "
                f"senin alış maliyetine göre toplam {t_sign}{pnl['target_pnl_pct']:.2f}% "
                f"({t_sign}{pnl['target_pnl_tl']:,.2f} TL) sonuç oluşur."
            )
        return base_line

    @staticmethod
    def _format_reports_block(consensus: AnalystConsensus) -> str:
        if not consensus.recent_reports:
            return "Tekil aracı kurum rapor detayı bulunamadı."
        lines: list[str] = []
        for rep in consensus.recent_reports[:5]:
            upside_str = f" (%{rep.upside_percent:+.1f})" if rep.upside_percent else ""
            date_str = f" [{rep.report_date}]" if rep.report_date else ""
            lines.append(
                f"- {rep.institution}: {rep.recommendation} | Hedef: {rep.target_price:.2f} TL{upside_str}{date_str}"
            )
        return "\n".join(lines)

    def _format_fallback_stock_report(
        self,
        symbol: str,
        consensus: AnalystConsensus,
        market: MarketSnapshot,
        portfolio_item: Optional[PortfolioItem] = None,
    ) -> str:
        """Gemini anahtarı yoksa veya erişilemezse aynı 4 başlıkla zengin özet üretir."""
        price = market.current_price or consensus.current_price
        portfolio_text = self._build_portfolio_context(
            portfolio_item=portfolio_item,
            current_price=price,
            avg_target_price=consensus.avg_target_price,
        )

        # Bölüm 1: Analist Konsensüsü
        if consensus.avg_target_price > 0 and price > 0:
            analyst_summary = (
                f"• Güncel Fiyat: {price:.2f} TL (Günlük: %{market.daily_change_pct:+.2f})\n"
                f"• Ortalama Hedef Fiyat: {consensus.avg_target_price:.2f} TL "
                f"(En düşük: {consensus.min_target_price:.2f} TL - En yüksek: {consensus.max_target_price:.2f} TL)\n"
                f"• Beklenen Getiri Potansiyeli: %{consensus.upside_percent:+.1f}\n"
                f"• Kurum Dağılımı ({consensus.total_analysts} değerlendirme): "
                f"{consensus.buy_count} AL / {consensus.hold_count} TUT / {consensus.sell_count} SAT"
            )
            if consensus.recent_reports:
                analyst_summary += "\n• Öne Çıkan Kurum Hedefleri:\n" + self._format_reports_block(consensus)
        else:
            analyst_summary = (
                f"• Güncel Fiyat: {price:.2f} TL (Günlük: %{market.daily_change_pct:+.2f})\n"
                "• Bu hisse için aktif aracı kurum hedef fiyat konsensüsü şu an sınırlı."
            )

        # Bölüm 2: Halk Diliyle Şirket ve Teknik Durum
        news_part = ""
        if market.recent_news:
            top_news = "\n".join(f"  - {headline}" for headline in market.recent_news[:3])
            news_part = f"\n• Son Haber/KAP Başlıkları:\n{top_news}"

        company_summary = (
            f"• Teknik Durum: {market.plain_technical_comment}\n"
            f"• Değerleme Özeti: {market.plain_valuation_comment}"
            f"{news_part}"
        )

        # Bölüm 4: Artılar, Riskler ve Ne Yapılabilir?
        pros: list[str] = []
        risks: list[str] = []

        if consensus.upside_percent >= self.settings.min_upside_percent:
            pros.append(
                f"Analistlerin ortalama hedef fiyatı güncel fiyata göre %{consensus.upside_percent:.1f} gibi güçlü bir potansiyel sunuyor."
            )
        elif consensus.upside_percent > 10:
            pros.append(
                f"Analist hedeflerine göre %{consensus.upside_percent:.1f} yukarı yönlü makul alan bulunuyor."
            )
        else:
            risks.append("Analist hedef fiyatlarına göre kısa vadede marj daralmış görünüyor.")

        if consensus.buy_count >= 2:
            pros.append(f"Birden fazla kurum/analist ({consensus.buy_count} adet) 'AL' yönünde görüş bildiriyor.")

        if "AL" in market.tv_recommendation:
            pros.append(f"Hazır teknik göstergeler şu an olumlu ({market.tv_recommendation}).")
        elif "SAT" in market.tv_recommendation:
            risks.append(f"Kısa vadeli teknik sinyaller zayıf ({market.tv_recommendation}); aceleci olmamakta fayda var.")

        if market.rsi is not None and market.rsi >= 70:
            risks.append("Son yükseliş hızlı olmuş, kısa vadede kâr satışı görülebilir.")
        if not risks:
            risks.append("Piyasa geneli dalgalanmaları ve sektörel haber akışı takip edilmeli.")
        if not pros:
            pros.append("Uzun vadeli bilanço ve yeni gelecek aracı kurum raporları izlenmeli.")

        if consensus.upside_percent >= self.settings.min_upside_percent and consensus.buy_count >= 2:
            action_note = (
                "Orta-uzun vadede analist desteği ve getiri potansiyeli cazip bölgede; "
                "kademeli alım/tutma stratejisi için güçlü adaylardan biri olarak izlenebilir."
            )
        elif portfolio_item and portfolio_item.is_owned and consensus.upside_percent > 15:
            action_note = "Elindeki pozisyonu korumak (TUT) ve yeni bilançoları beklemek mantıklı görünüyor."
        else:
            action_note = "Yeni pozisyon için acele etmeden geri çekilmeleri veya yeni aracı kurum raporlarını beklemek daha güvenli olabilir."

        pros_text = "\n".join(f"  + {p}" for p in pros)
        risks_text = "\n".join(f"  - {r}" for r in risks)

        return (
            f"📌 {symbol} ({market.company_name}) Analiz Raporu\n\n"
            f"🎯 Aracı Kurumların Ortak Kararı ve Hedef Fiyat Potansiyeli\n"
            f"{analyst_summary}\n\n"
            f"🏢 Şirket ve Teknik Görünümün \"Halk Dilinde\" Özeti\n"
            f"{company_summary}\n\n"
            f"💼 Senin Portföy ve Maliyet Durumun\n"
            f"• {portfolio_text}\n\n"
            f"⚖️ Artılar, Riskler ve \"Ne Yapılabilir?\" Yorumu\n"
            f"Artılar:\n{pros_text}\n"
            f"Riskler:\n{risks_text}\n"
            f"Özet Görüş: {action_note}\n\n"
            f"⚠️ Not: Bu özet bilgilendirme amaçlıdır, kesin yatırım tavsiyesi değildir."
        )

    def comment_on_stock(
        self,
        symbol: str,
        consensus: AnalystConsensus,
        market: MarketSnapshot,
        portfolio_item: Optional[PortfolioItem] = None,
    ) -> str:
        """Bir hisse için toplanan tüm verileri Gemini 3.8 Flash ile yorumlar."""
        clean_symbol = symbol.strip().upper().replace(".IS", "")
        price = market.current_price or consensus.current_price
        portfolio_context = self._build_portfolio_context(
            portfolio_item=portfolio_item,
            current_price=price,
            avg_target_price=consensus.avg_target_price,
        )
        reports_block = self._format_reports_block(consensus)
        news_block = "\n".join(f"- {n}" for n in market.recent_news[:4]) or "Güncel öne çıkan haber yok."

        prompt = f"""Hisse: {clean_symbol} ({market.company_name})
Güncel Fiyat: {price:.2f} TL (Günlük Değişim: %{market.daily_change_pct:+.2f})

[ARACI KURUM VE ANALİST VERİLERİ]
- Ortalama Hedef Fiyat: {consensus.avg_target_price:.2f} TL (Min: {consensus.min_target_price:.2f} TL / Max: {consensus.max_target_price:.2f} TL)
- Hedef Fiyat Potansiyeli: %{consensus.upside_percent:+.1f}
- Tavsiye Dağılımı ({consensus.total_analysts} görüş): {consensus.buy_count} AL, {consensus.hold_count} TUT, {consensus.sell_count} SAT
- Son Raporlar:
{reports_block}

[HAZIR YORUMLANMIŞ TEKNİK VE TEMEL DURUM]
- Teknik Özet: {market.plain_technical_comment}
- Değerleme Özeti: {market.plain_valuation_comment}
- F/K: {market.pe_ratio or 'Veri yok'} | PD/DD: {market.pb_ratio or 'Veri yok'} | 52H Zirveden Uzaklık: %{market.drawdown_from_high_pct if market.drawdown_from_high_pct is not None else 'Veri yok'}

[SON HABER / KAP BAŞLIKLARI]
{news_block}

[KULLANICININ PORTFÖY DURUMU]
{portfolio_context}

Lütfen bu verilere dayanarak, teknik analiz bilmeyen orta-uzun vadeli bir yatırımcı için belirlenen 4 başlıkta net, sade ve karar odaklı bir analiz yaz."""

        ai_text = self._call_gemini(prompt_text=prompt, system_instruction=SYSTEM_INSTRUCTION)
        if ai_text:
            return f"📌 {clean_symbol} ({market.company_name})\n\n{ai_text}"

        return self._format_fallback_stock_report(
            symbol=clean_symbol,
            consensus=consensus,
            market=market,
            portfolio_item=portfolio_item,
        )

    def generate_daily_bulletin(
        self,
        portfolio_evaluations: list[dict[str, Any]],
        opportunities: list[AnalystConsensus],
        new_reports: list[AnalystReport],
    ) -> str:
        """Akşam 18:30 günlük kapanış özet bültenini oluşturur."""
        portfolio_lines: list[str] = []
        total_cost = 0.0
        total_value = 0.0
        has_costed = False

        for item in portfolio_evaluations:
            p_item: PortfolioItem = item["portfolio"]
            market: MarketSnapshot = item["market"]
            consensus: AnalystConsensus = item["consensus"]
            price = market.current_price or consensus.current_price
            line = (
                f"- {p_item.symbol}: {price:.2f} TL (%{market.daily_change_pct:+.2f}) | "
                f"Hedef: {consensus.avg_target_price:.2f} TL (%{consensus.upside_percent:+.1f}) | "
                f"Teknik: {market.tv_recommendation}"
            )
            if p_item.is_owned and price > 0:
                pnl = p_item.calculate_pnl(price, consensus.avg_target_price)
                has_costed = True
                total_cost += pnl["total_cost"] or 0.0
                total_value += pnl["current_value"] or 0.0
                line += f" | K/Z: %{pnl['pnl_pct']:+.1f} ({pnl['pnl_tl']:+,.0f} TL)"
            portfolio_lines.append(line)

        portfolio_summary = "\n".join(portfolio_lines) if portfolio_lines else "Portföy/izleme listesi şu an boş."
        if has_costed and total_cost > 0:
            net_tl = total_value - total_cost
            net_pct = ((total_value - total_cost) / total_cost) * 100
            portfolio_summary += (
                f"\nToplam Maliyet: {total_cost:,.0f} TL | Güncel Değer: {total_value:,.0f} TL | "
                f"Net K/Z: {net_tl:+,.0f} TL (%{net_pct:+.2f})"
            )

        opp_lines = [
            f"- {opp.symbol}: Fiyat {opp.current_price:.2f} TL -> Hedef {opp.avg_target_price:.2f} TL "
            f"(%{opp.upside_percent:+.1f} potansiyel, {opp.buy_count} AL)"
            for opp in opportunities[:5]
        ]
        opp_summary = "\n".join(opp_lines) if opp_lines else "Bugün filtreyi aşan yeni öne çıkan fırsat bulunmadı."

        rep_lines = [
            f"- {r.symbol} ({r.institution}): {r.recommendation} | Hedef {r.target_price:.2f} TL (%{r.upside_percent:+.1f})"
            for r in new_reports[:6]
        ]
        rep_summary = "\n".join(rep_lines) if rep_lines else "Bugün yeni düşen tekil rapor yok."

        prompt = f"""Bugünün Borsa İstanbul (BIST) kapanış verileri aşağıdadır:

[1. PORTFÖY VE İZLEME LİSTESİ]
{portfolio_summary}

[2. EN YÜKSEK POTANSİYELLİ BIST FIRSATLARI (>= %{self.settings.min_upside_percent:.0f} ve >= {self.settings.min_buy_count} AL)]
{opp_summary}

[3. YENİ ARACI KURUM RAPORLARI]
{rep_summary}

Lütfen yatırımcıya akşam kahvesiyle birlikte 1 dakikada okuyabileceği, sade, net ve karar odaklı bir Günlük BIST Bülteni yaz."""

        ai_text = self._call_gemini(prompt_text=prompt, system_instruction=BULLETIN_SYSTEM_INSTRUCTION)
        if ai_text:
            return f"📊 BIST Günlük Kapanış Bülteni\n\n{ai_text}"

        return (
            "📊 BIST Günlük Kapanış Bülteni\n\n"
            f"💼 Portföy ve İzleme Listen:\n{portfolio_summary}\n\n"
            f"🚀 Fırsat Radarı (>= %{self.settings.min_upside_percent:.0f} Potansiyel & Çoklu AL):\n{opp_summary}\n\n"
            f"📰 Yeni Aracı Kurum Raporları:\n{rep_summary}\n\n"
            "💡 Detaylı analiz için sohbete hisse kodunu (ör. THYAO) yazman yeterli."
        )

    @staticmethod
    def _extract_comparison_rows(
        comparison_data: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """Karşılaştırma sözlüğünden veya nesnelerinden normalize satırlar çıkarır."""
        sector = str(_get_field(comparison_data, "sector", "Genel BIST") or "Genel BIST").strip()
        raw_items = _get_field(comparison_data, "items", []) or []
        raw_highlights = _get_field(comparison_data, "highlights", {}) or {}
        highlights = {
            "best_upside_symbol": _get_field(raw_highlights, "best_upside_symbol"),
            "lowest_pe_symbol": _get_field(raw_highlights, "lowest_pe_symbol"),
            "strongest_tech_symbol": _get_field(raw_highlights, "strongest_tech_symbol"),
        }

        rows: list[dict[str, Any]] = []
        for item in raw_items:
            market = _get_field(item, "market", {})
            consensus = _get_field(item, "consensus", {})
            raw_sym = (
                _get_field(item, "symbol", "")
                or _get_field(market, "symbol", "")
                or _get_field(consensus, "symbol", "")
            )
            symbol = _normalize_bist_symbol(raw_sym)
            if not symbol:
                continue

            market_price = _safe_float(_get_field(market, "current_price"), 0.0) or 0.0
            consensus_price = _safe_float(_get_field(consensus, "current_price"), 0.0) or 0.0
            current_price = market_price if market_price > 0 else consensus_price

            rows.append(
                {
                    "symbol": symbol,
                    "company_name": str(_get_field(market, "company_name", "") or "").strip(),
                    "current_price": current_price,
                    "daily_change_pct": _safe_float(_get_field(market, "daily_change_pct"), 0.0) or 0.0,
                    "pe_ratio": _safe_float(_get_field(market, "pe_ratio")),
                    "pb_ratio": _safe_float(_get_field(market, "pb_ratio")),
                    "dividend_yield_pct": _safe_float(_get_field(market, "dividend_yield_pct")),
                    "tv_recommendation": str(_get_field(market, "tv_recommendation", "BİLİNMİYOR") or "BİLİNMİYOR"),
                    "plain_technical_comment": str(_get_field(market, "plain_technical_comment", "") or "").strip(),
                    "avg_target_price": _safe_float(_get_field(consensus, "avg_target_price"), 0.0) or 0.0,
                    "upside_percent": _safe_float(_get_field(consensus, "upside_percent"), 0.0) or 0.0,
                    "buy_count": _safe_int(_get_field(consensus, "buy_count"), 0),
                    "hold_count": _safe_int(_get_field(consensus, "hold_count"), 0),
                    "sell_count": _safe_int(_get_field(consensus, "sell_count"), 0),
                }
            )
        return sector, rows, highlights

    @staticmethod
    def _build_comparison_summary(
        rows: list[dict[str, Any]],
        highlights: dict[str, Any],
    ) -> str:
        """Hisseler için yan yana okunabilir metin özeti oluşturur."""
        blocks: list[str] = []
        for row in rows:
            title = row["symbol"]
            if row["company_name"]:
                title = f"{title} ({row['company_name']})"

            price_str = (
                f"{row['current_price']:.2f} TL (%{row['daily_change_pct']:+.2f})"
                if row["current_price"] > 0
                else "Veri yok"
            )
            pe_str = f"{row['pe_ratio']:.2f}" if row["pe_ratio"] is not None and row["pe_ratio"] > 0 else "Veri yok"
            pb_str = f"{row['pb_ratio']:.2f}" if row["pb_ratio"] is not None and row["pb_ratio"] > 0 else "Veri yok"
            target_str = (
                f"{row['avg_target_price']:.2f} TL (%{row['upside_percent']:+.1f})"
                if row["avg_target_price"] > 0
                else "Hedef yok"
            )
            div_yield = row["dividend_yield_pct"]
            div_part = (
                f" | Temettü Verimi: %{div_yield:.2f}"
                if div_yield is not None and div_yield > 0
                else ""
            )
            tech_detail = row["plain_technical_comment"]
            tech_line = (
                f"{row['tv_recommendation']} — {tech_detail}"
                if tech_detail
                else row["tv_recommendation"]
            )

            blocks.append(
                f"• {title}\n"
                f"  Fiyat: {price_str} | F/K: {pe_str} | PD/DD: {pb_str}{div_part}\n"
                f"  Hedef Fiyat: {target_str} | Kurumlar: {row['buy_count']} AL / {row['hold_count']} TUT / {row['sell_count']} SAT\n"
                f"  Teknik Özet: {tech_line}"
            )

        highlight_parts: list[str] = []
        if highlights.get("best_upside_symbol"):
            highlight_parts.append(f"En Yüksek Potansiyel: {highlights['best_upside_symbol']}")
        if highlights.get("lowest_pe_symbol"):
            highlight_parts.append(f"En Düşük F/K: {highlights['lowest_pe_symbol']}")
        if highlights.get("strongest_tech_symbol"):
            highlight_parts.append(f"En Güçlü Teknik: {highlights['strongest_tech_symbol']}")

        summary = "\n\n".join(blocks)
        if highlight_parts:
            summary += "\n\n🔎 Öne Çıkanlar: " + " | ".join(highlight_parts)
        return summary

    @staticmethod
    def _score_comparison_row(row: dict[str, Any]) -> float:
        """Orta-uzun vadeli kıyaslama yedeği için deterministik puan hesaplar."""
        score = row["upside_percent"] + (row["buy_count"] * 10.0) - (row["sell_count"] * 8.0)
        tv_upper = row["tv_recommendation"].upper()
        if "GÜÇLÜ AL" in tv_upper:
            score += 10.0
        elif "AL" in tv_upper and "SAT" not in tv_upper:
            score += 6.0
        elif "GÜÇLÜ SAT" in tv_upper:
            score -= 10.0
        elif "SAT" in tv_upper:
            score -= 6.0

        pe = row["pe_ratio"]
        if pe is not None and pe > 0:
            if pe <= 10:
                score += 8.0
            elif pe <= 18:
                score += 4.0

        div_yield = row["dividend_yield_pct"]
        if div_yield is not None and div_yield > 0:
            score += min(div_yield, 15.0) * 1.5
        return score

    def _format_fallback_comparison(
        self,
        sector: str,
        rows: list[dict[str, Any]],
        highlights: dict[str, Any],
        summary_text: str,
    ) -> str:
        """Gemini kullanılamadığında deterministik Türkçe karşılaştırma yorumu üretir."""
        ranked = sorted(rows, key=self._score_comparison_row, reverse=True)
        standout = ranked[0]

        reasons: list[str] = []
        if standout["avg_target_price"] > 0:
            reasons.append(f"%{standout['upside_percent']:+.1f} analist hedef potansiyeli")
        if standout["buy_count"] > 0:
            reasons.append(f"{standout['buy_count']} kurumun 'AL' tavsiyesi")
        if standout["pe_ratio"] is not None and standout["pe_ratio"] > 0:
            reasons.append(f"{standout['pe_ratio']:.2f} F/K çarpanı")
        if standout["dividend_yield_pct"] is not None and standout["dividend_yield_pct"] > 0:
            reasons.append(f"%{standout['dividend_yield_pct']:.2f} temettü verimi")
        if "AL" in standout["tv_recommendation"].upper():
            reasons.append(f"olumlu teknik görünüm ({standout['tv_recommendation']})")

        reason_text = (
            ", ".join(reasons) + " ile orta-uzun vadede dengeli bir tablo sunuyor."
            if reasons
            else "mevcut veriler içinde görece daha dengeli bir görünüm sergiliyor."
        )

        extra_notes: list[str] = []
        lowest_pe = highlights.get("lowest_pe_symbol")
        if lowest_pe and lowest_pe != standout["symbol"]:
            extra_notes.append(f"• Çarpan ucuzluğu arayanlar için {lowest_pe} daha düşük F/K oranıyla dikkat çekiyor.")
        best_up = highlights.get("best_upside_symbol")
        if best_up and best_up != standout["symbol"]:
            extra_notes.append(f"• En yüksek hedef getiri marjı ise {best_up} tarafında bulunuyor.")

        div_candidates = [
            r for r in rows if r["dividend_yield_pct"] is not None and r["dividend_yield_pct"] > 0
        ]
        if div_candidates:
            top_div = max(div_candidates, key=lambda r: r["dividend_yield_pct"] or 0.0)
            if top_div["symbol"] != standout["symbol"]:
                extra_notes.append(
                    f"• Temettü geliri önceliği olan yatırımcılar {top_div['symbol']} "
                    f"(%{top_div['dividend_yield_pct']:.2f}) hissesini de ayrıca değerlendirebilir."
                )

        notes_block = ("\n" + "\n".join(extra_notes)) if extra_notes else ""
        return (
            f"⚔️ {sector} Sektörü Karşılaştırması\n\n"
            f"{summary_text}\n\n"
            f"🏆 Orta-Uzun Vadede Öne Çıkan: {standout['symbol']}\n"
            f"• {standout['symbol']}, {reason_text}"
            f"{notes_block}\n\n"
            "⚠️ Not: Bu karşılaştırma bilgilendirme amaçlıdır, kesin yatırım tavsiyesi değildir."
        )

    def comment_on_comparison(self, comparison_data: dict[str, Any]) -> str:
        """Birden fazla hisseyi yan yana karşılaştırıp sade Türkçe yorum üretir."""
        sector, rows, highlights = self._extract_comparison_rows(comparison_data)
        if not rows:
            return "⚠️ Karşılaştırılacak yeterli hisse verisi bulunamadı."

        summary_text = self._build_comparison_summary(rows=rows, highlights=highlights)
        prompt = f"""Sektör / Grup: {sector}

[YAN YANA HİSSE KARŞILAŞTIRMASI]
{summary_text}

Yukarıdaki verilere bakarak, teknik analiz bilmeyen orta-uzun vadeli bir yatırımcı için hangi hissenin öne çıktığını, nedenlerini, diğer hisselerin artı/eksilerini sade ve anlaşılır Türkçe ile yorumla."""

        ai_text = self._call_gemini(
            prompt_text=prompt,
            system_instruction=COMPARISON_SYSTEM_INSTRUCTION,
        )
        if ai_text:
            return f"⚔️ {sector} Sektörü Karşılaştırması\n\n{summary_text}\n\n🤖 Yapay Zeka Değerlendirmesi:\n{ai_text}"

        return self._format_fallback_comparison(
            sector=sector,
            rows=rows,
            highlights=highlights,
            summary_text=summary_text,
        )

    def answer_free_question(
        self,
        question: str,
        portfolio_context: str,
        opportunities_context: str,
    ) -> str:
        """Kullanıcının portföy ve BIST fırsatları hakkındaki serbest sorusunu yanıtlar."""
        clean_question = (question or "").strip() or "Portföyüm ve güncel BIST fırsatları hakkında özet yorum yapar mısın?"
        clean_portfolio = (
            (portfolio_context or "").strip()
            or "Portföyünde veya izleme listende şu an kayıtlı hisse bulunmuyor."
        )
        clean_opportunities = (
            (opportunities_context or "").strip()
            or "Şu an öne çıkan güncel BIST fırsat verisi bulunmuyor."
        )

        prompt = f"""[KULLANICININ SORUSU]
{clean_question}

[KULLANICININ PORTFÖY DURUMU]
{clean_portfolio}

[GÜNCEL BIST FIRSATLARI]
{clean_opportunities}

Lütfen soruyu yukarıdaki gerçek portföy ve BIST fırsat verilerine dayanarak, karmaşık terim kullanmadan halk dilinde Türkçe olarak yanıtla."""

        ai_text = self._call_gemini(
            prompt_text=prompt,
            system_instruction=FREE_QA_SYSTEM_INSTRUCTION,
        )
        if ai_text:
            if "tavsiye" not in ai_text.lower():
                ai_text += "\n\n⚠️ Not: Bilgilendirme amaçlıdır, yatırım tavsiyesi değildir."
            return ai_text

        return (
            "💬 Portföy ve BIST Asistan Özeti\n\n"
            f"❓ Sorun: {clean_question}\n\n"
            f"💼 Portföy Durumun:\n{clean_portfolio}\n\n"
            f"🚀 Güncel BIST Fırsatları:\n{clean_opportunities}\n\n"
            "💡 Detaylı hisse analizi için doğrudan hisse kodunu (ör. THYAO) yazabilirsin.\n"
            "⚠️ Not: Bu bilgiler özet amaçlıdır, kesin yatırım tavsiyesi değildir."
        )

    def extract_portfolio_from_image(
        self,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
    ) -> list[dict[str, Any]]:
        """Portföy ekran görüntüsünden BIST hisse, lot ve ortalama maliyet bilgisini çıkarır."""
        if self._client is None or not image_bytes:
            return []

        clean_mime = (mime_type or "image/jpeg").strip().lower()
        if clean_mime == "image/jpg":
            clean_mime = "image/jpeg"
        if not clean_mime.startswith("image/"):
            clean_mime = "image/jpeg"

        base64_str = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            "Bu görseldeki Borsa İstanbul (BIST) portföy kayıtlarını oku ve SADECE şu biçimde "
            'geçerli bir JSON dizisi döndür: [{"symbol": "THYAO", "lot": 100.0, "avg_cost": 285.5}]. '
            "Hisse kodunu .IS olmadan büyük harf yaz, ondalık ayırıcı olarak nokta (.) kullan, "
            "güncel fiyat yerine ortalama alış maliyetini (avg_cost) al. "
            "Geçerli hisse bulunamazsa sadece [] döndür."
        )

        try:
            interaction = self._client.interactions.create(
                model=self.settings.gemini_model,
                system_instruction=PORTFOLIO_OCR_SYSTEM_INSTRUCTION,
                input=[
                    {"type": "image", "data": base64_str, "mime_type": clean_mime},
                    {"type": "text", "text": prompt},
                ],
            )
            raw_output = getattr(interaction, "output_text", None)
            raw_text = raw_output.strip() if isinstance(raw_output, str) else ""
            if not raw_text:
                return []

            fence_match = re.search(
                r"```(?:json)?\s*(.*?)```",
                raw_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            cleaned = fence_match.group(1).strip() if fence_match else raw_text.strip()

            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                start = cleaned.find("[")
                end = cleaned.rfind("]")
                if start == -1 or end <= start:
                    return []
                parsed = json.loads(cleaned[start : end + 1])

            if isinstance(parsed, dict) and "symbol" in parsed:
                parsed = [parsed]
            if not isinstance(parsed, list):
                return []

            results: list[dict[str, Any]] = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                symbol = _normalize_bist_symbol(
                    entry.get("symbol") or entry.get("ticker") or entry.get("hisse")
                )
                if not re.fullmatch(r"[A-Z][A-Z0-9]{2,5}", symbol):
                    continue
                if symbol in {"TRY", "USD", "EUR", "TL", "BIST"}:
                    continue

                lot = _parse_locale_number(entry.get("lot"))
                avg_cost = _parse_locale_number(entry.get("avg_cost"))
                if lot is None or avg_cost is None or lot <= 0 or avg_cost <= 0:
                    continue

                results.append(
                    {
                        "symbol": symbol,
                        "lot": float(lot),
                        "avg_cost": float(avg_cost),
                    }
                )
            return results
        except Exception as exc:
            logger.warning("Portföy görseli ayrıştırılamadı: %s", exc)
            return []
