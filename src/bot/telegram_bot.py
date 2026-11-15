"""Borsa İstanbul (BIST) interaktif Telegram botu ve komut yöneticisi."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import time as dt_time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.ai.commentator import BorsaCommentator
from src.charts import generate_portfolio_chart_png, generate_stock_chart_png
from src.collectors.analyst_collector import AnalystCollector
from src.collectors.market_collector import MarketCollector
from src.collectors.sector_collector import (
    BIST_SECTORS,
    compare_stocks,
    get_dividend_leaders,
    get_golden_stocks,
    get_sector_and_peers,
)
from src.config import DEFAULT_BIST_UNIVERSE, Settings, get_settings
from src.scheduler.jobs import (
    analyze_stock,
    build_daily_bulletin_text,
    daily_evening_summary_job,
    intraday_report_scan_job,
    send_telegram_text,
)
from src.storage.db import DatabaseManager

logger = logging.getLogger(__name__)

_KNOWN_BIST_TICKERS: set[str] = set(DEFAULT_BIST_UNIVERSE)
for _sector_members in BIST_SECTORS.values():
    _KNOWN_BIST_TICKERS.update(_sector_members)

_NON_TICKER_WORDS = {
    "ANALIZ", "ANALİZ", "DURUM", "DURUMU", "DURUMDA", "NEDIR", "NEDİR", "NASIL",
    "HISSE", "HİSSE", "HISSEM", "BORSA", "FIYAT", "FİYAT", "FIYATI", "HEDEF", "RAPOR",
    "RAPORLAR", "PORTFOY", "PORTFÖY", "OZET", "ÖZET", "FIRSAT", "FIRSATLAR",
    "YARDIM", "MERHABA", "SELAM", "TESEKKUR", "TEŞEKKÜR", "GUNCEL", "GÜNCEL",
    "YORUM", "ALINIR", "SATILIR", "BEKLE", "BUGUN", "BUGÜN", "YARIN",
    "ELIMDE", "ELİMDE", "SEPET", "HANGI", "HANGİ", "HANGISI", "NELER", "SENCE",
    "BENCE", "BIST", "BİST", "UZUN", "VADE", "VADEDE", "ORTA", "KISA", "PARA",
    "PARAM", "CAZIP", "CAZİP", "UCUZ", "PAHALI", "ALTIN", "TEMETTU", "ONERI",
    "ONER", "ONERIN", "NEDEN", "ZARAR", "KARAR", "DOLAR", "EURO", "FAIZ",
    "KRIZ", "RISK", "RİSK", "RISKI", "SEKTOR", "GENEL", "PIYASA", "ENDEKS",
    "MISIN", "MUSUN", "YAPAR", "KADAR", "DAHA", "DIGER", "BASKA", "ICIN",
    "GIBI", "KENDI", "BENIM", "SENIN", "BUNUN", "SUNUN", "ONUN", "EVET",
    "HAYIR", "TAMAM", "LUTFEN", "SIMDI", "SONRA", "ONCE", "ORAN", "ORANI",
    "DEGER", "HEPSI", "TUMU", "HAFTA", "AYLIK", "YILLIK", "SABAH", "AKSAM",
    "GECE", "BANKA", "ENERJI", "GIDA", "SANAYI", "TEKNIK", "TEMEL", "TREND",
    "SINYAL", "GIRIS", "CIKIS", "DESTEK", "DIRENC", "ZIRVE", "TABAN", "TAVAN",
    "HACIM", "DOVIZ", "GUMUS", "NAKIT", "KREDI", "YAPAY", "ZEKA", "AJAN",
    "ALIS", "SATIS", "KAZANC", "VERIM", "VERIMI", "VERIR", "KALIR", "GIDER",
    "GELIR", "OLUR", "OLDU", "EDER", "DUSTU", "CIKTI", "VARSA", "YOKSA",
    "KIMIN", "NEREDE", "TARIH", "SAAT", "SEANS", "HALKA", "GUNAYDIN", "NASILSIN",
    "KOLAY", "GELSIN", "GRAFIK", "DETAY", "LISTE", "EKLE", "CIKAR",
    "BANA", "SANA", "BIZE", "BİZE", "SIZE", "SİZE", "PEKI", "PEKİ", "ACABA",
    "KIME", "KİME", "NEYE", "NIYE", "NİYE", "NERDE", "HANI", "HANİ", "YANI",
    "YANİ", "BILE", "BİLE", "BELKI", "BELKİ", "SANKI", "SANKİ", "KESKE", "KEŞKE",
    "GUZEL", "GÜZEL", "KOTU", "KÖTÜ", "SAGLAM", "DOGRU", "DOĞRU", "YANLIS",
    "MIYIM", "MIYIZ", "OLMAZ", "OLMAK", "ALMAK", "SATMAK", "TUTMAK", "SORU",
    "CEVAP", "BILGI", "BİLGİ", "YENI", "YENİ", "ESKI", "ESKİ", "TANE", "ADET",
    "KERE", "DEFA", "FARK", "ARTI", "EKSI", "EKSİ", "KAZAN", "KAYIP", "SABIR",
    "UZMAN", "HEMEN", "AYRI", "TOPLU", "BASIT", "BASİT", "ACIK", "AÇIK", "OLAN",
    "EDEN", "ALAN", "SATAN", "YAPAN", "BAKAN", "SORAN", "VEREN", "GELEN", "GIDEN",
    "KALAN", "YATIR", "VARMI", "YOKMU", "BIRAZ", "COGU",
}

_QUESTION_PATTERN = re.compile(
    r"\?|\b(?:"
    r"ne|nedir|neler|nas[ıi]l|sepet\w*|portf[oö]y\w*|[oö]ner\w*|yorum\w*|sence|bence|"
    r"yat[ıi]r[ıi]m\w*|hangi\w*|ka[cç]|risk\w*|temett[uü]\w*|al[ıi]n[ıi]r|sat[ıi]l[ıi]r|"
    r"mant[ıi]kl[ıi]|neden|tavsiye\w*|hedef\w*|b[uü]t[cç]e\w*|vade\w*|tl"
    r")\b",
    flags=re.IGNORECASE,
)


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💼 Portföyüm", callback_data="menu:portfoy"),
                InlineKeyboardButton("🚀 Fırsatlar (%35+)", callback_data="menu:firsatlar"),
            ],
            [
                InlineKeyboardButton("🏆 Altın Hisseler", callback_data="menu:altinhisseler"),
                InlineKeyboardButton("💰 Temettü Radarı", callback_data="menu:temettu"),
            ],
            [
                InlineKeyboardButton("📰 Son Raporlar", callback_data="menu:raporlar"),
                InlineKeyboardButton("📊 Günlük Özet", callback_data="menu:ozet"),
            ],
        ]
    )


def _portfolio_summary_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Portföy Grafiği", callback_data="chart_portfolio")],
    ]
    rows.extend(list(row) for row in _main_menu_keyboard().inline_keyboard)
    return InlineKeyboardMarkup(rows)


def _stock_action_keyboard(symbol: str, is_in_portfolio: bool) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton("❌ Listeden Çıkar", callback_data=f"unwatch:{symbol}")
        if is_in_portfolio
        else InlineKeyboardButton("➕ İzleme Listesine Ekle", callback_data=f"watch:{symbol}")
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Güncelle", callback_data=f"refresh:{symbol}"),
                toggle_btn,
            ],
            [
                InlineKeyboardButton("📈 Grafik Göster", callback_data=f"chart:{symbol}"),
                InlineKeyboardButton("🏦 Kurum Rapor Detayları", callback_data=f"details:{symbol}"),
            ],
            [
                InlineKeyboardButton(
                    "⚔️ Sektör Rakipleriyle Kıyasla",
                    callback_data=f"compare_sector:{symbol}",
                ),
            ],
        ]
    )


def _parse_number(raw: str) -> Optional[float]:
    cleaned = raw.strip().replace(",", ".")
    try:
        val = float(cleaned)
        return val if val >= 0 else None
    except ValueError:
        return None


def _extract_symbols_from_text(text: str) -> list[str]:
    """Serbest metindeki olası BIST hisse kodlarını sırayla çıkarır."""
    upper_text = text.upper()
    tokens = re.findall(
        r"(?<![A-ZÇĞİÖŞÜ0-9])[A-Z][A-Z0-9]{3,5}(?![A-ZÇĞİÖŞÜ0-9])",
        upper_text,
    )
    found: list[str] = []
    for token in tokens:
        if token in _KNOWN_BIST_TICKERS or token not in _NON_TICKER_WORDS:
            if token not in found:
                found.append(token)
    return found


def _extract_symbol_from_text(text: str) -> Optional[str]:
    """Kullanıcının yazdığı serbest mesajdan BIST hisse kodunu yakalar."""
    symbols = _extract_symbols_from_text(text)
    if not symbols:
        return None
    for sym in symbols:
        if sym in _KNOWN_BIST_TICKERS:
            return sym
    return symbols[0]


def classify_free_text(text: str) -> tuple[str, Optional[str]]:
    """Serbest mesajın tekil hisse sorgusu mu, serbest soru mu yoksa yardım mı olduğunu belirler."""
    cleaned = (text or "").strip()
    if not cleaned:
        return "help", None

    symbols = _extract_symbols_from_text(cleaned)
    words = re.findall(r"\S+", cleaned)
    lowered = cleaned.replace("İ", "i").lower()
    has_question_cue = bool(_QUESTION_PATTERN.search(lowered))

    if len(symbols) == 1 and len(words) <= 4:
        candidate = symbols[0]
        if not any(kw in lowered for kw in ("sepet", "portföy", "portfoy")):
            if candidate in _KNOWN_BIST_TICKERS:
                return "stock", candidate
            is_explicit_uppercase = bool(re.search(rf"\b{re.escape(candidate)}\b", cleaned))
            if not has_question_cue or is_explicit_uppercase:
                return "stock", candidate

    if symbols and len(words) > 4:
        return "qa", None
    if len(symbols) >= 2:
        return "qa", None
    if has_question_cue or len(words) > 3:
        return "qa", None

    return "help", None


def _get_deps(context: ContextTypes.DEFAULT_TYPE) -> tuple[
    Settings, DatabaseManager, MarketCollector, AnalystCollector, BorsaCommentator
]:
    return (
        context.bot_data["settings"],
        context.bot_data["db"],
        context.bot_data["market_collector"],
        context.bot_data["analyst_collector"],
        context.bot_data["commentator"],
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Karşılama ve hızlı kullanım menüsünü gösterir."""
    if not update.effective_message or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    settings, _, _, _, _ = _get_deps(context)

    chat_note = (
        ""
        if settings.has_valid_chat_id()
        else f"\n🆔 Otomatik bildirimler için `.env` dosyasına `TELEGRAM_CHAT_ID={chat_id}` eklemeyi unutma.\n"
    )

    welcome_text = (
        "👋 **Borsa İstanbul (BIST) Analist & Portföy Ajanı**'na hoş geldin!\n\n"
        "Senin yerine aracı kurum hedef fiyat raporlarını tarar, teknik/temel verileri **halk dilinde** özetler "
        "ve önemli gelişmeleri cebine bildiririm.\n"
        f"{chat_note}\n"
        "📌 **Hızlı Kullanım:**\n"
        "• Sohbete doğrudan hisse kodu yaz: `THYAO` veya `ASELS ne durumda?`\n"
        "• Sohbete serbest yatırım sorusu sor: `Elimde 50 bin TL var nasıl bir sepet yapmalıyım?`\n"
        "• 📸 Portföy ekran görüntüsü gönder → hisseleri, lotları ve maliyetleri otomatik okuyayım\n"
        "• `/ekle THYAO 100 285` → 100 lot, 285 TL maliyetle portföye ekler\n"
        "• `/ekle TUPRS` → Maliyet girmeden izleme listesine ekler\n"
        "• `/cikar THYAO` → Listeden çıkarır\n"
        "• `/portfoy` → Portföy kâr/zarar, hedef fiyat ve grafik özeti\n"
        "• `/firsatlar` → En az %35+ potansiyelli ve çoklu AL alan BIST hisselerini bulur\n"
        "• `/altinhisseler` → Analist AL desteği + potansiyel skoruna göre en iyi BIST hisseleri\n"
        "• `/temettu` → En yüksek temettü verimine sahip BIST liderleri\n"
        "• `/kiyasla THYAO PGSUS` → Seçtiğin hisseleri yan yana kıyaslar ve yorumlar\n"
        "• `/raporlar` → Son aracı kurum hedef fiyat raporlarını listeler\n"
        "• `/ozet` → Günlük kapanış bültenini hemen hazırlar"
    )
    await send_telegram_text(
        context.bot, chat_id, welcome_text, reply_markup=_main_menu_keyboard()
    )


async def _send_stock_analysis(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
    symbol: str,
) -> None:
    settings, db, market_collector, analyst_collector, commentator = _get_deps(context)
    clean_symbol = MarketCollector.normalize_bist_symbol(symbol)

    wait_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔎 {clean_symbol} için aracı kurum hedefleri, piyasa verileri ve AI yorumu hazırlanıyor...",
    )
    try:
        analysis_text, _, _, portfolio_item = await asyncio.to_thread(
            analyze_stock,
            clean_symbol,
            db,
            market_collector,
            analyst_collector,
            commentator,
        )
        try:
            await wait_msg.delete()
        except Exception:
            pass
        markup = _stock_action_keyboard(clean_symbol, is_in_portfolio=portfolio_item is not None)
        await send_telegram_text(context.bot, chat_id, analysis_text, reply_markup=markup)
    except Exception as exc:
        logger.exception("Hisse analizi sırasında hata (%s): %s", clean_symbol, exc)
        await wait_msg.edit_text(
            f"❌ {clean_symbol} verisi alınırken bir hata oluştu. Sembol kodunu kontrol edip tekrar dener misin?"
        )


async def cmd_analiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/analiz <HİSSE>` komutunu işler."""
    if not update.effective_message or not update.effective_chat:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Kullanım: `/analiz THYAO` veya doğrudan sohbete `THYAO` yazabilirsin."
        )
        return
    await _send_stock_analysis(context, update.effective_chat.id, context.args[0])


async def cmd_ekle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/ekle <HİSSE> [LOT] [MALİYET]` komutunu işler."""
    if not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Kullanım:\n"
            "• Portföye maliyetli eklemek için: `/ekle THYAO 100 285.5`\n"
            "• Sadece izlemek için: `/ekle TUPRS`"
        )
        return

    _, db, _, _, _ = _get_deps(context)
    symbol = MarketCollector.normalize_bist_symbol(context.args[0])
    lot = 0.0
    avg_cost = 0.0

    if len(context.args) >= 3:
        parsed_lot = _parse_number(context.args[1])
        parsed_cost = _parse_number(context.args[2])
        if parsed_lot is None or parsed_cost is None or parsed_lot <= 0 or parsed_cost <= 0:
            await update.effective_message.reply_text(
                "Lot ve maliyet pozitif sayı olmalı. Örnek: `/ekle THYAO 100 285.5`"
            )
            return
        lot = parsed_lot
        avg_cost = parsed_cost
    elif len(context.args) == 2:
        await update.effective_message.reply_text(
            "Maliyet takibi için hem lot hem alış fiyatı girmelisin: `/ekle THYAO 100 285.5`\n"
            "Sadece izleme listesine eklemek için tek başına `/ekle THYAO` yazabilirsin."
        )
        return

    item = db.upsert_portfolio_item(symbol=symbol, lot=lot, avg_cost=avg_cost)
    if item.is_owned:
        msg = (
            f"✅ **{item.symbol}** portföyüne kaydedildi.\n"
            f"• Miktar: {item.lot:g} lot | Ortalama Maliyet: {item.avg_cost:.2f} TL\n"
            f"Güncel durumu görmek için `/portfoy` veya `{item.symbol}` yazabilirsin."
        )
    else:
        msg = (
            f"✅ **{item.symbol}** izleme listene eklendi.\n"
            f"Yeni analist raporu çıktığında anında bildirim alacaksın."
        )
    await send_telegram_text(context.bot, update.effective_chat.id, msg)


async def cmd_cikar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/cikar <HİSSE>` komutunu işler."""
    if not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Kullanım: `/cikar THYAO`")
        return

    _, db, _, _, _ = _get_deps(context)
    symbol = MarketCollector.normalize_bist_symbol(context.args[0])
    removed = db.remove_portfolio_item(symbol)
    if removed:
        await update.effective_message.reply_text(f"🗑️ {symbol} portföy/izleme listenden çıkarıldı.")
    else:
        await update.effective_message.reply_text(f"ℹ️ {symbol} zaten listende kayıtlı değil.")


async def _send_portfolio_summary(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str
) -> None:
    _, db, market_collector, analyst_collector, _ = _get_deps(context)
    items = db.list_portfolio_items()
    if not items:
        await send_telegram_text(
            context.bot,
            chat_id,
            "💼 Portföyün ve izleme listen şu an boş.\n\n"
            "Eklemek için:\n"
            "• `/ekle THYAO 100 285` (Maliyetli portföy)\n"
            "• `/ekle TUPRS` (Sadece izleme)",
        )
        return

    wait_msg = await context.bot.send_message(
        chat_id=chat_id, text="💼 Portföy ve analist hedefleri hesaplanıyor..."
    )

    def _compute() -> str:
        lines = ["💼 **Portföy ve İzleme Listen**\n"]
        total_cost = 0.0
        total_value = 0.0
        has_owned = False

        for item in items:
            snap = market_collector.get_snapshot(item.symbol, include_news=False)
            cons = analyst_collector.get_consensus(
                item.symbol, current_price=snap.current_price if snap.current_price > 0 else None
            )
            price = snap.current_price or cons.current_price
            target_part = (
                f"🎯 Hedef: {cons.avg_target_price:.2f} TL (%{cons.upside_percent:+.1f})"
                if cons.avg_target_price > 0
                else "🎯 Hedef: Veri yok"
            )
            line = (
                f"• **{item.symbol}**: {price:.2f} TL (Günlük %{snap.daily_change_pct:+.2f}) | "
                f"{target_part} | Teknik: {snap.tv_recommendation}"
            )
            if item.is_owned and price > 0:
                pnl = item.calculate_pnl(price, cons.avg_target_price if cons.avg_target_price > 0 else None)
                has_owned = True
                total_cost += pnl["total_cost"] or 0.0
                total_value += pnl["current_value"] or 0.0
                line += (
                    f"\n  └ {item.lot:g} lot @ {item.avg_cost:.2f} TL → "
                    f"K/Z: **%{pnl['pnl_pct']:+.1f} ({pnl['pnl_tl']:+,.0f} TL)**"
                )
                if pnl["target_pnl_pct"] is not None:
                    line += f" | Hedefe göre: %{pnl['target_pnl_pct']:+.1f}"
            lines.append(line)

        if has_owned and total_cost > 0:
            net_tl = total_value - total_cost
            net_pct = ((total_value - total_cost) / total_cost) * 100
            lines.append(
                f"\n📊 **Toplam Portföy Özeti:**\n"
                f"Maliyet: {total_cost:,.0f} TL | Güncel Değer: {total_value:,.0f} TL | "
                f"Net K/Z: **{net_tl:+,.0f} TL (%{net_pct:+.2f})**"
            )
        return "\n".join(lines)

    try:
        report_text = await asyncio.to_thread(_compute)
    except Exception as exc:
        logger.exception("Portföy özeti hazırlanırken hata: %s", exc)
        await wait_msg.edit_text("❌ Portföy özeti hazırlanırken geçici bir hata oluştu. Lütfen tekrar dene.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass
    await send_telegram_text(
        context.bot, chat_id, report_text, reply_markup=_portfolio_summary_keyboard()
    )


async def cmd_portfoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/portfoy` komutunu işler."""
    if not update.effective_chat:
        return
    await _send_portfolio_summary(context, update.effective_chat.id)


async def _send_opportunities(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str
) -> None:
    settings, db, _, analyst_collector, _ = _get_deps(context)
    wait_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🚀 BIST hisseleri taranıyor (>= %{settings.min_upside_percent:.0f} potansiyel ve çoklu AL)...",
    )

    def _scan() -> list:
        portfolio_syms = {item.symbol for item in db.list_portfolio_items()}
        return analyst_collector.scan_opportunities(
            universe=settings.bist_universe,
            portfolio_symbols=portfolio_syms,
            limit=8,
        )

    try:
        opps = await asyncio.to_thread(_scan)
    except Exception as exc:
        logger.exception("Fırsatlar taranırken hata: %s", exc)
        await wait_msg.edit_text("❌ Fırsatlar taranırken geçici bir hata oluştu. Lütfen tekrar dene.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    if not opps:
        await send_telegram_text(
            context.bot,
            chat_id,
            f"ℹ️ Şu an BIST takip listesinde hem **%{settings.min_upside_percent:.0f}+ potansiyel** "
            f"hem de **en az {settings.min_buy_count} AL** şartını aynı anda sağlayan hisse bulunamadı.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    lines = [
        f"🚀 **BIST Fırsat Radarı (>= %{settings.min_upside_percent:.0f} Potansiyel & Çoklu AL)**\n"
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, opp in enumerate(opps, start=1):
        lines.append(
            f"{idx}. **{opp.symbol}** — Fiyat: {opp.current_price:.2f} TL → "
            f"Hedef: {opp.avg_target_price:.2f} TL (**%{opp.upside_percent:+.1f}**) | "
            f"{opp.buy_count} AL / {opp.hold_count} TUT / {opp.sell_count} SAT"
        )
        buttons.append(
            [InlineKeyboardButton(f"🔍 {opp.symbol} Detaylı Yorum", callback_data=f"analyze:{opp.symbol}")]
        )

    await send_telegram_text(
        context.bot,
        chat_id,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_firsatlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/firsatlar` komutunu işler."""
    if not update.effective_chat:
        return
    await _send_opportunities(context, update.effective_chat.id)


async def _send_latest_reports(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str
) -> None:
    settings, db, _, analyst_collector, _ = _get_deps(context)
    wait_msg = await context.bot.send_message(
        chat_id=chat_id, text="📰 Son aracı kurum hedef fiyat raporları toplanıyor..."
    )

    def _fetch() -> list:
        portfolio_syms = [item.symbol for item in db.list_portfolio_items()]
        target_syms = portfolio_syms + [s for s in settings.bist_universe if s not in portfolio_syms]
        return analyst_collector.fetch_latest_reports(symbols=target_syms[:12])

    try:
        reports = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.exception("Son raporlar çekilirken hata: %s", exc)
        await wait_msg.edit_text("❌ Raporlar çekilirken geçici bir hata oluştu. Lütfen tekrar dene.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    if not reports:
        await send_telegram_text(
            context.bot,
            chat_id,
            "ℹ️ Şu an listelenecek yeni aracı kurum raporu bulunamadı. Belirli bir hisse için kodunu yazabilirsin (ör. `THYAO`).",
        )
        return

    lines = ["📰 **Son Aracı Kurum Hedef Fiyat Raporları**\n"]
    for rep in reports[:12]:
        upside_str = f" (%{rep.upside_percent:+.1f})" if rep.upside_percent else ""
        date_str = f" • {rep.report_date}" if rep.report_date else ""
        lines.append(
            f"• **{rep.symbol}** ({rep.institution}): {rep.recommendation} | "
            f"Hedef: {rep.target_price:.2f} TL{upside_str}{date_str}"
        )

    await send_telegram_text(
        context.bot, chat_id, "\n".join(lines), reply_markup=_main_menu_keyboard()
    )


async def cmd_raporlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/raporlar` komutunu işler."""
    if not update.effective_chat:
        return
    await _send_latest_reports(context, update.effective_chat.id)


async def _send_daily_summary(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str
) -> None:
    settings, db, market_collector, analyst_collector, commentator = _get_deps(context)
    wait_msg = await context.bot.send_message(
        chat_id=chat_id, text="📊 Günlük BIST özet bülteni hazırlanıyor..."
    )
    try:
        bulletin = await asyncio.to_thread(
            build_daily_bulletin_text,
            db,
            market_collector,
            analyst_collector,
            commentator,
            settings,
        )
    except Exception as exc:
        logger.exception("Günlük bülten hazırlanırken hata: %s", exc)
        await wait_msg.edit_text("❌ Günlük bülten hazırlanırken geçici bir hata oluştu. Lütfen biraz sonra tekrar dene.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass
    await send_telegram_text(context.bot, chat_id, bulletin, reply_markup=_main_menu_keyboard())


async def cmd_ozet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/ozet` komutunu işler."""
    if not update.effective_chat:
        return
    await _send_daily_summary(context, update.effective_chat.id)


async def _send_golden_stocks(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str
) -> None:
    settings, _, market_collector, analyst_collector, _ = _get_deps(context)
    wait_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🏆 BIST Altın Hisseler taranıyor (analist AL desteği + hedef potansiyeli)...",
    )
    try:
        golden = await asyncio.to_thread(
            get_golden_stocks,
            analyst_collector,
            market_collector,
            settings.bist_universe,
            6,
        )
    except Exception as exc:
        logger.exception("Altın hisseler taranırken hata: %s", exc)
        await wait_msg.edit_text("❌ Altın hisseler taranırken bir hata oluştu.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    if not golden:
        await send_telegram_text(
            context.bot,
            chat_id,
            "ℹ️ Şu an en az 2 kurumdan 'AL' tavsiyesi alan altın hisse adayı bulunamadı.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    lines = ["🏆 **BIST Altın Hisseler (Analist Desteği & Potansiyel Skoru)**\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, entry in enumerate(golden, start=1):
        sym = entry["symbol"]
        market = entry["market"]
        cons = entry["consensus"]
        score = entry["score"]
        price = market.current_price or cons.current_price
        price_str = f"{price:.2f} TL" if price > 0 else "Veri yok"
        target_str = f"{cons.avg_target_price:.2f} TL" if cons.avg_target_price > 0 else "Veri yok"
        lines.append(
            f"{idx}. **{sym}** — Skor: **{score:.1f}** | Fiyat: {price_str} → "
            f"Hedef: {target_str} (**%{cons.upside_percent:+.1f}**) | "
            f"{cons.buy_count} AL / {cons.hold_count} TUT / {cons.sell_count} SAT"
        )
        buttons.append(
            [InlineKeyboardButton(f"🔍 {sym} Detaylı Yorum", callback_data=f"analyze:{sym}")]
        )

    await send_telegram_text(
        context.bot,
        chat_id,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_altinhisseler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/altinhisseler` komutunu işler."""
    if not update.effective_chat:
        return
    await _send_golden_stocks(context, update.effective_chat.id)


async def _send_dividend_leaders(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str
) -> None:
    settings, _, market_collector, analyst_collector, _ = _get_deps(context)
    wait_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="💰 BIST Temettü Radarı taranıyor...",
    )
    try:
        leaders = await asyncio.to_thread(
            get_dividend_leaders,
            market_collector,
            analyst_collector,
            settings.bist_universe,
            6,
        )
    except Exception as exc:
        logger.exception("Temettü liderleri taranırken hata: %s", exc)
        await wait_msg.edit_text("❌ Temettü verileri taranırken bir hata oluştu.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    if not leaders:
        await send_telegram_text(
            context.bot,
            chat_id,
            "ℹ️ Şu an BIST takip evreninde pozitif temettü verimi bilgisi bulunan hisse saptanamadı.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    lines = ["💰 **BIST Temettü Radarı (Temettü Verimi & Hedef Potansiyel)**\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for idx, entry in enumerate(leaders, start=1):
        sym = entry["symbol"]
        market = entry["market"]
        cons = entry["consensus"]
        div_yield = float(entry.get("dividend_yield_pct") or 0.0)
        price = market.current_price or cons.current_price
        price_str = f"{price:.2f} TL" if price > 0 else "Veri yok"
        target_part = (
            f"Hedef: {cons.avg_target_price:.2f} TL (**%{cons.upside_percent:+.1f}**)"
            if cons.avg_target_price > 0
            else "Hedef: Veri yok"
        )
        last_div_part = (
            f" • Son Temettü: {market.last_dividend_value:.2f} TL"
            if market.last_dividend_value
            else ""
        )
        lines.append(
            f"{idx}. **{sym}** — Temettü Verimi: **%{div_yield:.2f}**{last_div_part} | "
            f"Fiyat: {price_str} | {target_part}"
        )
        buttons.append(
            [InlineKeyboardButton(f"🔍 {sym} Detaylı Yorum", callback_data=f"analyze:{sym}")]
        )

    await send_telegram_text(
        context.bot,
        chat_id,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_temettu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/temettu` komutunu işler."""
    if not update.effective_chat:
        return
    await _send_dividend_leaders(context, update.effective_chat.id)


async def _send_stock_comparison(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
    symbols: list[str],
    wait_text: Optional[str] = None,
) -> None:
    _, _, market_collector, analyst_collector, commentator = _get_deps(context)
    clean_symbols: list[str] = []
    for raw in symbols:
        norm = MarketCollector.normalize_bist_symbol(raw)
        if norm and norm not in clean_symbols:
            clean_symbols.append(norm)
    clean_symbols = clean_symbols[:6]

    if len(clean_symbols) < 2:
        await send_telegram_text(
            context.bot,
            chat_id,
            "Karşılaştırma için en az 2 hisse kodu gerekli. Örnek: `/kiyasla THYAO PGSUS`",
        )
        return

    prompt_msg = wait_text or (
        f"⚔️ {', '.join(clean_symbols)} hisseleri karşılaştırılıyor ve AI yorumu hazırlanıyor..."
    )
    wait_msg = await context.bot.send_message(chat_id=chat_id, text=prompt_msg)

    def _compare() -> str:
        comparison_data = compare_stocks(
            symbols=clean_symbols,
            market_collector=market_collector,
            analyst_collector=analyst_collector,
        )
        return commentator.comment_on_comparison(comparison_data)

    try:
        comparison_text = await asyncio.to_thread(_compare)
    except Exception as exc:
        logger.exception("Hisse karşılaştırması sırasında hata (%s): %s", clean_symbols, exc)
        await wait_msg.edit_text("❌ Hisseler karşılaştırılırken bir hata oluştu.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass
    await send_telegram_text(
        context.bot, chat_id, comparison_text, reply_markup=_main_menu_keyboard()
    )


async def cmd_kiyasla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/kiyasla <HISSE1> [HISSE2 ...]` komutunu işler."""
    if not update.effective_message or not update.effective_chat:
        return
    raw_tokens = re.split(r"[\s,;]+", " ".join(context.args or []))
    symbols: list[str] = []
    for tok in raw_tokens:
        norm = MarketCollector.normalize_bist_symbol(tok)
        if norm and norm not in symbols:
            symbols.append(norm)

    if not symbols:
        await update.effective_message.reply_text(
            "Kullanım:\n"
            "• Tek hisseyi sektör rakipleriyle kıyaslamak için: `/kiyasla THYAO`\n"
            "• Seçili hisseleri yan yana kıyaslamak için: `/kiyasla THYAO PGSUS`"
        )
        return
    if len(symbols) == 1:
        await _send_sector_comparison(context, update.effective_chat.id, symbols[0])
        return
    await _send_stock_comparison(context, update.effective_chat.id, symbols)


async def _send_sector_comparison(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, symbol: str
) -> None:
    clean_symbol = MarketCollector.normalize_bist_symbol(symbol)
    sector_name, peers = get_sector_and_peers(clean_symbol)
    target_symbols = [clean_symbol] + [p for p in peers if p != clean_symbol]
    wait_text = (
        f"⚔️ {clean_symbol} ve {sector_name} sektöründeki rakipleri "
        f"({', '.join(peers)}) karşılaştırılıyor..."
        if peers
        else f"⚔️ {clean_symbol} karşılaştırması hazırlanıyor..."
    )
    await _send_stock_comparison(context, chat_id, target_symbols, wait_text=wait_text)


async def _send_stock_chart(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, symbol: str
) -> None:
    _, db, market_collector, analyst_collector, _ = _get_deps(context)
    clean_symbol = MarketCollector.normalize_bist_symbol(symbol)
    if not clean_symbol:
        return

    wait_msg = await context.bot.send_message(
        chat_id=chat_id, text=f"📈 {clean_symbol} fiyat ve analist hedef grafiği hazırlanıyor..."
    )

    def _render() -> tuple[bytes, str]:
        market = market_collector.get_snapshot(clean_symbol, include_news=False)
        consensus = analyst_collector.get_consensus(
            clean_symbol,
            current_price=market.current_price if market.current_price > 0 else None,
        )
        if market.current_price <= 0 and consensus.current_price > 0:
            market = market.model_copy(update={"current_price": consensus.current_price})
        price_history = market_collector.get_price_history(clean_symbol, period="6mo")
        portfolio_item = db.get_portfolio_item(clean_symbol)
        png_bytes = generate_stock_chart_png(
            symbol=clean_symbol,
            market=market,
            consensus=consensus,
            portfolio_item=portfolio_item,
            price_history=price_history,
        )
        price = market.current_price or consensus.current_price
        caption = f"📈 {clean_symbol} ({market.company_name})"
        if price > 0:
            caption += f" • Güncel: {price:.2f} TL"
        if consensus.avg_target_price > 0:
            caption += f" • Ort. Hedef: {consensus.avg_target_price:.2f} TL (%{consensus.upside_percent:+.1f})"
        return png_bytes, caption[:1024]

    try:
        png_bytes, caption = await asyncio.to_thread(_render)
    except Exception as exc:
        logger.exception("Hisse grafiği üretilirken hata (%s): %s", clean_symbol, exc)
        await wait_msg.edit_text(f"❌ {clean_symbol} için grafik oluşturulurken hata oluştu.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    photo_buf = io.BytesIO(png_bytes)
    photo_buf.name = f"{clean_symbol}_chart.png"
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=photo_buf,
        caption=caption,
        reply_markup=_main_menu_keyboard(),
    )


async def _send_portfolio_chart(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str
) -> None:
    _, db, market_collector, analyst_collector, _ = _get_deps(context)
    items = db.list_portfolio_items()
    if not items:
        await send_telegram_text(
            context.bot,
            chat_id,
            "💼 Portföyün ve izleme listen şu an boş. Önce `/ekle` komutuyla veya fotoğraf göndererek hisse ekle.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    wait_msg = await context.bot.send_message(
        chat_id=chat_id, text="📊 Portföy dağılım ve hedef getiri grafiği hazırlanıyor..."
    )

    def _render() -> tuple[bytes, str]:
        portfolio_evaluations: list[dict[str, Any]] = []
        for p_item in items:
            market = market_collector.get_snapshot(p_item.symbol, include_news=False)
            consensus = analyst_collector.get_consensus(
                p_item.symbol,
                current_price=market.current_price if market.current_price > 0 else None,
            )
            portfolio_evaluations.append(
                {
                    "portfolio": p_item,
                    "market": market,
                    "consensus": consensus,
                }
            )
        png_bytes = generate_portfolio_chart_png(portfolio_evaluations)
        caption = f"📊 Portföy ve İzleme Listesi Özeti ({len(items)} hisse)"
        return png_bytes, caption

    try:
        png_bytes, caption = await asyncio.to_thread(_render)
    except Exception as exc:
        logger.exception("Portföy grafiği üretilirken hata: %s", exc)
        await wait_msg.edit_text("❌ Portföy grafiği oluşturulurken hata oluştu.")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    photo_buf = io.BytesIO(png_bytes)
    photo_buf.name = "portfoy_chart.png"
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=photo_buf,
        caption=caption,
        reply_markup=_main_menu_keyboard(),
    )


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """InlineKeyboard buton tıklamalarını yönetir."""
    query = update.callback_query
    if not query or not query.data or not update.effective_chat:
        return
    await query.answer()

    chat_id = update.effective_chat.id
    action, _, payload = query.data.partition(":")
    _, db, _, analyst_collector, _ = _get_deps(context)

    if action == "menu":
        if payload == "portfoy":
            await _send_portfolio_summary(context, chat_id)
        elif payload == "firsatlar":
            await _send_opportunities(context, chat_id)
        elif payload == "altinhisseler":
            await _send_golden_stocks(context, chat_id)
        elif payload == "temettu":
            await _send_dividend_leaders(context, chat_id)
        elif payload == "raporlar":
            await _send_latest_reports(context, chat_id)
        elif payload == "ozet":
            await _send_daily_summary(context, chat_id)
    elif action in {"analyze", "refresh"} and payload:
        await _send_stock_analysis(context, chat_id, payload)
    elif action == "chart" and payload:
        await _send_stock_chart(context, chat_id, payload)
    elif action == "chart_portfolio":
        await _send_portfolio_chart(context, chat_id)
    elif action == "compare_sector" and payload:
        await _send_sector_comparison(context, chat_id, payload)
    elif action == "watch" and payload:
        db.upsert_portfolio_item(payload, lot=0.0, avg_cost=0.0)
        await send_telegram_text(
            context.bot, chat_id, f"✅ **{payload}** izleme listene eklendi."
        )
    elif action == "unwatch" and payload:
        db.remove_portfolio_item(payload)
        await send_telegram_text(
            context.bot, chat_id, f"🗑️ **{payload}** listenden çıkarıldı."
        )
    elif action == "details" and payload:
        consensus = await asyncio.to_thread(analyst_collector.get_consensus, payload)
        if not consensus.recent_reports:
            await send_telegram_text(
                context.bot, chat_id, f"ℹ️ {payload} için tekil kurum raporu detayı bulunamadı."
            )
            return
        lines = [f"🏦 **{payload} — Aracı Kurum Hedef Fiyat Detayları**\n"]
        for rep in consensus.recent_reports:
            upside_str = f" (%{rep.upside_percent:+.1f})" if rep.upside_percent else ""
            lines.append(
                f"• {rep.institution}: **{rep.recommendation}** | "
                f"{rep.target_price:.2f} TL{upside_str} [{rep.report_date}]"
            )
        await send_telegram_text(context.bot, chat_id, "\n".join(lines))
    elif action == "ocr_confirm":
        user_data = context.user_data if context.user_data is not None else {}
        pending_items = user_data.pop("pending_ocr_items", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if not pending_items:
            await send_telegram_text(
                context.bot,
                chat_id,
                "ℹ️ Onay bekleyen portföy görseli kaydı bulunamadı. Yeni bir ekran görüntüsü gönderebilirsin.",
                reply_markup=_main_menu_keyboard(),
            )
            return
        saved_items = db.bulk_upsert_portfolio(pending_items)
        if not saved_items:
            await send_telegram_text(
                context.bot,
                chat_id,
                "⚠️ Kaydedilecek geçerli hisse kaydı bulunamadı.",
                reply_markup=_main_menu_keyboard(),
            )
            return
        lines = ["✅ **Portföy Kayıtları Başarıyla Kaydedildi**\n"]
        for item in saved_items:
            if item.is_owned:
                lines.append(f"• **{item.symbol}**: {item.lot:g} lot @ {item.avg_cost:.2f} TL")
            else:
                lines.append(f"• **{item.symbol}**: İzleme listesine eklendi")
        lines.append("\nPortföy özetini ve grafiğini görmek için `/portfoy` yazabilirsin.")
        await send_telegram_text(
            context.bot, chat_id, "\n".join(lines), reply_markup=_portfolio_summary_keyboard()
        )
    elif action == "ocr_cancel":
        if context.user_data is not None:
            context.user_data.pop("pending_ocr_items", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_telegram_text(
            context.bot,
            chat_id,
            "❌ Portföy görseli aktarımı iptal edildi. Mevcut kayıtların değiştirilmedi.",
            reply_markup=_main_menu_keyboard(),
        )


async def handle_photo_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Kullanıcının gönderdiği portföy ekran görüntüsünden hisse, lot ve maliyet okur."""
    if not update.effective_message or not update.effective_chat:
        return
    photos = update.effective_message.photo
    if not photos:
        return

    chat_id = update.effective_chat.id
    _, _, _, _, commentator = _get_deps(context)

    wait_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="📸 Portföy ekran görüntüsü inceleniyor, hisseler ve maliyetler okunuyor...",
    )
    try:
        largest_photo = photos[-1]
        tg_file = await largest_photo.get_file()
        raw_bytes = await tg_file.download_as_bytearray()
        items = await asyncio.to_thread(
            commentator.extract_portfolio_from_image,
            bytes(raw_bytes),
            "image/jpeg",
        )
    except Exception as exc:
        logger.exception("Fotoğraf OCR işlenirken hata: %s", exc)
        try:
            await wait_msg.edit_text(
                "❌ Görsel işlenirken bir sorun oluştu. Daha net bir ekran görüntüsü gönderebilir "
                "veya `/ekle THYAO 100 285` komutuyla elle ekleyebilirsin."
            )
        except Exception:
            pass
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    if not items:
        await send_telegram_text(
            context.bot,
            chat_id,
            "ℹ️ Görselden net bir hisse, lot veya ortalama maliyet bilgisi çıkaramadım.\n\n"
            "• Hisse kodu, lot/adet ve ortalama maliyet sütunlarının net göründüğü bir ekran görüntüsü deneyebilirsin.\n"
            "• Dilersen hızlıca `/ekle THYAO 100 285` komutuyla elle de ekleyebilirsin.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    if context.user_data is not None:
        context.user_data["pending_ocr_items"] = items

    lines = ["📸 **Görselden Okunan Portföy Kayıtları**\n"]
    for idx, entry in enumerate(items, start=1):
        lines.append(
            f"{idx}. **{entry['symbol']}** — {entry['lot']:g} lot | "
            f"Ort. Maliyet: {entry['avg_cost']:.2f} TL"
        )
    lines.append("\nBu kayıtları portföyüne ekleyip güncellememi onaylıyor musun?")

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Onayla ve Kaydet", callback_data="ocr_confirm"),
                InlineKeyboardButton("❌ İptal", callback_data="ocr_cancel"),
            ]
        ]
    )
    await send_telegram_text(context.bot, chat_id, "\n".join(lines), reply_markup=markup)


def _build_qa_contexts_and_answer(
    question: str,
    settings: Settings,
    db: DatabaseManager,
    market_collector: MarketCollector,
    analyst_collector: AnalystCollector,
    commentator: BorsaCommentator,
) -> str:
    """Serbest yatırım sorusu için güncel portföy ve BIST fırsat bağlamını kurup yanıtlar."""
    portfolio_items = db.list_portfolio_items()
    portfolio_symbols = {item.symbol for item in portfolio_items}

    portfolio_lines: list[str] = []
    for item in portfolio_items[:12]:
        try:
            snap = market_collector.get_snapshot(item.symbol, include_news=False)
            cons = analyst_collector.get_consensus(
                item.symbol,
                current_price=snap.current_price if snap.current_price > 0 else None,
            )
            price = snap.current_price or cons.current_price
            sector_label = snap.sector_tr or "Genel BIST"
            line = (
                f"- {item.symbol} ({sector_label}): Fiyat {price:.2f} TL (%{snap.daily_change_pct:+.2f}), "
                f"Hedef {cons.avg_target_price:.2f} TL (%{cons.upside_percent:+.1f}), "
                f"Kurumlar {cons.buy_count} AL/{cons.hold_count} TUT/{cons.sell_count} SAT, "
                f"Teknik {snap.tv_recommendation}"
            )
            if item.is_owned and price > 0:
                pnl = item.calculate_pnl(price, cons.avg_target_price or None)
                line += (
                    f" | Pozisyon: {item.lot:g} lot @ {item.avg_cost:.2f} TL "
                    f"(K/Z %{pnl['pnl_pct']:+.1f}, {pnl['pnl_tl']:+,.0f} TL)"
                )
            portfolio_lines.append(line)
        except Exception as exc:
            logger.debug("Q&A portföy bağlamı hatası (%s): %s", item.symbol, exc)
            portfolio_lines.append(f"- {item.symbol}: {item.lot:g} lot @ {item.avg_cost:.2f} TL")

    portfolio_context = (
        "\n".join(portfolio_lines)
        if portfolio_lines
        else "Portföyünde veya izleme listende şu an kayıtlı hisse bulunmuyor."
    )

    opp_lines: list[str] = []
    try:
        opps = analyst_collector.scan_opportunities(
            universe=settings.bist_universe[:15],
            portfolio_symbols=portfolio_symbols,
            limit=5,
        )
        for opp in opps:
            opp_lines.append(
                f"- {opp.symbol}: Fiyat {opp.current_price:.2f} TL -> Hedef {opp.avg_target_price:.2f} TL "
                f"(%{opp.upside_percent:+.1f} potansiyel, {opp.buy_count} AL)"
            )
    except Exception as exc:
        logger.debug("Q&A fırsat bağlamı hatası: %s", exc)

    mentioned_symbols = _extract_symbols_from_text(question)
    extra_symbols = [s for s in mentioned_symbols if s not in portfolio_symbols][:3]
    for sym in extra_symbols:
        try:
            snap = market_collector.get_snapshot(sym, include_news=False)
            cons = analyst_collector.get_consensus(
                sym, current_price=snap.current_price if snap.current_price > 0 else None
            )
            price = snap.current_price or cons.current_price
            opp_lines.append(
                f"- [Sorulan Hisse] {sym} ({snap.sector_tr}): Fiyat {price:.2f} TL, "
                f"Hedef {cons.avg_target_price:.2f} TL (%{cons.upside_percent:+.1f}), "
                f"{cons.buy_count} AL/{cons.hold_count} TUT/{cons.sell_count} SAT, "
                f"F/K {snap.pe_ratio or 'Yok'}, Teknik {snap.tv_recommendation}"
            )
        except Exception as exc:
            logger.debug("Q&A sorulan hisse bağlamı hatası (%s): %s", sym, exc)

    opportunities_context = (
        "\n".join(opp_lines)
        if opp_lines
        else f"Takip evreni ({', '.join(settings.bist_universe[:10])}) için güncel güçlü fırsat bulunamadı."
    )

    return commentator.answer_free_question(
        question=question,
        portfolio_context=portfolio_context,
        opportunities_context=opportunities_context,
    )


async def _send_free_question_answer(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | str, question: str
) -> None:
    settings, db, market_collector, analyst_collector, commentator = _get_deps(context)
    wait_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🤔 Sorun değerlendiriliyor, portföy ve güncel BIST verilerine bakılıyor...",
    )
    try:
        answer_text = await asyncio.to_thread(
            _build_qa_contexts_and_answer,
            question,
            settings,
            db,
            market_collector,
            analyst_collector,
            commentator,
        )
    except Exception as exc:
        logger.exception("Serbest soru yanıtlanırken hata: %s", exc)
        await wait_msg.edit_text("❌ Sorun yanıtlanırken bir hata oluştu, lütfen tekrar dener misin?")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass
    await send_telegram_text(
        context.bot, chat_id, answer_text, reply_markup=_main_menu_keyboard()
    )


async def handle_free_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Serbest metni kısa hisse analizi veya doğal dil yatırım sorusu olarak yönlendirir."""
    if not update.effective_message or not update.effective_chat:
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return

    intent, symbol = classify_free_text(text)
    if intent == "stock" and symbol:
        await _send_stock_analysis(context, update.effective_chat.id, symbol)
    elif intent == "qa":
        await _send_free_question_answer(context, update.effective_chat.id, text)
    else:
        await send_telegram_text(
            context.bot,
            update.effective_chat.id,
            "Hangi hisseye bakmamı veya neyi yanıtlamamı istersin? Doğrudan hisse kodu yazabilir "
            "(ör. `THYAO`, `TUPRS`, `ASELS`), portföy görseli gönderebilir veya yatırım sorunu "
            "serbestçe sorabilirsin.",
            reply_markup=_main_menu_keyboard(),
        )


def _parse_report_time(time_str: str) -> dt_time:
    try:
        hour_str, min_str = time_str.strip().split(":", 1)
        return dt_time(hour=int(hour_str), minute=int(min_str))
    except Exception:
        return dt_time(hour=18, minute=30)


def build_application(settings: Optional[Settings] = None) -> Application:
    """Telegram Bot Application nesnesini komutlar ve zamanlayıcılarla kurar."""
    active_settings = settings or get_settings()
    if not active_settings.has_valid_telegram_token():
        raise ValueError(
            "Geçerli bir TELEGRAM_BOT_TOKEN bulunamadı. Lütfen .env dosyasını kontrol edin."
        )

    db = DatabaseManager(active_settings.db_path)
    market_collector = MarketCollector()
    analyst_collector = AnalystCollector(settings=active_settings)
    commentator = BorsaCommentator(settings=active_settings)

    app = ApplicationBuilder().token(active_settings.telegram_bot_token).build()
    app.bot_data["settings"] = active_settings
    app.bot_data["db"] = db
    app.bot_data["market_collector"] = market_collector
    app.bot_data["analyst_collector"] = analyst_collector
    app.bot_data["commentator"] = commentator

    app.add_handler(CommandHandler(["start", "yardim", "help"], cmd_start))
    app.add_handler(CommandHandler("analiz", cmd_analiz))
    app.add_handler(CommandHandler("ekle", cmd_ekle))
    app.add_handler(CommandHandler("cikar", cmd_cikar))
    app.add_handler(CommandHandler("portfoy", cmd_portfoy))
    app.add_handler(CommandHandler("firsatlar", cmd_firsatlar))
    app.add_handler(CommandHandler("altinhisseler", cmd_altinhisseler))
    app.add_handler(CommandHandler("temettu", cmd_temettu))
    app.add_handler(CommandHandler("kiyasla", cmd_kiyasla))
    app.add_handler(CommandHandler("raporlar", cmd_raporlar))
    app.add_handler(CommandHandler("ozet", cmd_ozet))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))

    if app.job_queue is not None:
        tz = ZoneInfo(active_settings.timezone)
        report_t = _parse_report_time(active_settings.daily_report_time)
        daily_time = dt_time(hour=report_t.hour, minute=report_t.minute, tzinfo=tz)

        app.job_queue.run_repeating(
            intraday_report_scan_job,
            interval=max(5, active_settings.scan_interval_minutes) * 60,
            first=45,
            name="intraday_report_scan",
        )
        app.job_queue.run_daily(
            daily_evening_summary_job,
            time=daily_time,
            name="daily_evening_summary",
        )

    return app


def run_telegram_bot(settings: Optional[Settings] = None) -> None:
    """İnteraktif Telegram botunu polling modunda başlatır."""
    app = build_application(settings)
    logger.info("Borsa Ajanı Telegram botu başlatıldı.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
