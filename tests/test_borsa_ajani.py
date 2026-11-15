"""Borsa İstanbul (BIST) ajanı için birim ve entegrasyon testleri."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import main as app_main
from src.ai.commentator import BorsaCommentator
from src.bot.telegram_bot import (
    _extract_symbol_from_text,
    _main_menu_keyboard,
    _portfolio_summary_keyboard,
    _stock_action_keyboard,
    build_application,
    classify_free_text,
    cmd_altinhisseler,
    cmd_kiyasla,
    cmd_temettu,
    handle_callback_query,
    handle_free_text,
    handle_photo_message,
)
from src.charts import generate_portfolio_chart_png, generate_stock_chart_png
from src.collectors.analyst_collector import AnalystCollector, normalize_recommendation
from src.collectors.market_collector import (
    MarketCollector,
    build_plain_technical_comment,
    build_plain_valuation_comment,
)
from src.collectors.sector_collector import (
    check_portfolio_sharp_moves,
    compare_stocks,
    get_dividend_leaders,
    get_golden_stocks,
    get_sector_and_peers,
)
from src.config import Settings
from src.scheduler.jobs import collect_pending_report_alerts, format_for_telegram_html
from src.server.keep_alive import start_keep_alive_server, stop_keep_alive_server
from src.storage.db import DatabaseManager
from src.storage.models import AnalystConsensus, AnalystReport, MarketSnapshot, PortfolioItem


def test_portfolio_crud_and_pnl_calculation(tmp_path: Path) -> None:
    """Portföy ekleme/çıkarma, izleme listesi ve kâr/zarar + hedef getiri hesabını doğrular."""
    db = DatabaseManager(tmp_path / "test_portfolio.db")

    owned = db.upsert_portfolio_item("thyao.is", lot=100, avg_cost=250.0)
    watched = db.upsert_portfolio_item("tuprs")

    assert owned.symbol == "THYAO"
    assert owned.is_owned is True
    assert watched.symbol == "TUPRS"
    assert watched.is_owned is False

    pnl = owned.calculate_pnl(current_price=300.0, target_price=400.0)
    assert pnl["total_cost"] == 25000.0
    assert pnl["current_value"] == 30000.0
    assert pnl["pnl_tl"] == 5000.0
    assert pnl["pnl_pct"] == 20.0
    assert pnl["target_value"] == 40000.0
    assert pnl["target_pnl_tl"] == 15000.0
    assert pnl["target_pnl_pct"] == 60.0

    items = db.list_portfolio_items()
    assert [i.symbol for i in items] == ["THYAO", "TUPRS"]

    assert db.remove_portfolio_item("TUPRS") is True
    assert db.get_portfolio_item("TUPRS") is None
    assert db.remove_portfolio_item("TUPRS") is False


def test_notified_reports_deduplication(tmp_path: Path) -> None:
    """Aynı aracı kurum raporunun ikinci kez bildirilmemesini doğrular."""
    db = DatabaseManager(tmp_path / "test_reports.db")
    report = AnalystReport(
        symbol="ASELS",
        institution="İş Yatırım",
        target_price=85.5,
        recommendation="AL",
        report_date="2026-11-10",
        source="test",
    )

    assert db.is_report_notified(report.report_id) is False
    db.mark_report_notified(report)
    assert db.is_report_notified(report.report_id) is True


def test_opportunity_filter_rules() -> None:
    """Portföydeki hisseler için tüm raporların, dışındakiler için %35+ ve >=2 AL filtresinin çalıştığını doğrular."""
    settings = Settings(min_upside_percent=35.0, min_buy_count=2)
    collector = AnalystCollector(settings=settings)

    low_upside = AnalystConsensus(
        symbol="EREGL",
        current_price=50.0,
        avg_target_price=57.5,
        upside_percent=15.0,
        buy_count=3,
        total_analysts=4,
    )
    single_buy = AnalystConsensus(
        symbol="PETKM",
        current_price=20.0,
        avg_target_price=30.0,
        upside_percent=50.0,
        buy_count=1,
        total_analysts=2,
    )
    strong_opp = AnalystConsensus(
        symbol="THYAO",
        current_price=300.0,
        avg_target_price=435.0,
        upside_percent=45.0,
        buy_count=4,
        total_analysts=5,
    )

    # Portföy içindeki hisse %15 potansiyelle bile bildirilmeli
    assert collector.should_notify_report(low_upside, is_in_portfolio=True) is True
    # Portföy dışındaki hisse <%35 ise elenmeli
    assert collector.should_notify_report(low_upside, is_in_portfolio=False) is False
    # Portföy dışındaki hisse <2 AL ise elenmeli
    assert collector.should_notify_report(single_buy, is_in_portfolio=False) is False
    # Her iki şartı sağlayan fırsat bildirilmeli
    assert collector.should_notify_report(strong_opp, is_in_portfolio=False) is True


def test_plain_turkish_technical_and_valuation_comments() -> None:
    """Teknik ve temel verilerin halk dilinde anlamlı Türkçe metne dönüştüğünü doğrular."""
    tech_text = build_plain_technical_comment(
        tv_recommendation="GÜÇLÜ AL",
        tv_buy_count=17,
        tv_neutral_count=6,
        tv_sell_count=3,
        rsi=54.0,
    )
    assert "26 tanesinden 17'si AL" in tech_text
    assert "GÜÇLÜ AL" in tech_text
    assert "aşırı şişkinlik yok" in tech_text

    val_text = build_plain_valuation_comment(
        pe_ratio=6.4,
        pb_ratio=1.3,
        drawdown_from_high_pct=-26.0,
    )
    assert "zirvesinden %26.0 aşağıda" in val_text
    assert "oldukça ucuz" in val_text
    assert "defter değerine yakın" in val_text


def test_commentator_fallback_and_gemini_interactions(tmp_path: Path) -> None:
    """Yorumlayıcının hem yedek şablon hem Gemini Interactions API ile 4 başlıklı rapor ürettiğini doğrular."""
    consensus = AnalystConsensus(
        symbol="THYAO",
        current_price=300.0,
        avg_target_price=435.0,
        min_target_price=390.0,
        max_target_price=480.0,
        upside_percent=45.0,
        buy_count=5,
        hold_count=1,
        sell_count=0,
        total_analysts=6,
        recent_reports=[
            AnalystReport(
                symbol="THYAO",
                institution="İş Yatırım",
                target_price=450.0,
                upside_percent=50.0,
                recommendation="AL",
                report_date="2026-11-12",
                source="test",
            )
        ],
    )
    market = MarketSnapshot(
        symbol="THYAO",
        company_name="Türk Hava Yolları",
        current_price=300.0,
        daily_change_pct=1.8,
        pe_ratio=5.5,
        pb_ratio=0.9,
        drawdown_from_high_pct=-14.0,
        tv_recommendation="AL",
        tv_buy_count=15,
        tv_neutral_count=7,
        tv_sell_count=4,
        rsi=53.0,
        plain_technical_comment="Hazır teknik göstergelerin 26 tanesinden 15'i AL veriyor.",
        plain_valuation_comment="F/K ve PD/DD çarpanları ucuz bölgede.",
        recent_news=["THYAO yolcu sayısı açıklandı"],
    )
    db = DatabaseManager(tmp_path / "commentator.db")
    portfolio_item = db.upsert_portfolio_item("THYAO", lot=100, avg_cost=250.0)

    # 1) API key yokken fallback şablon testi
    no_key_settings = Settings(gemini_api_key="", gemini_model="gemini-3.8-flash")
    fallback_commentator = BorsaCommentator(settings=no_key_settings)
    fallback_text = fallback_commentator.comment_on_stock(
        "THYAO", consensus=consensus, market=market, portfolio_item=portfolio_item
    )

    assert "🎯 Aracı Kurumların Ortak Kararı ve Hedef Fiyat Potansiyeli" in fallback_text
    assert "🏢 Şirket ve Teknik Görünümün \"Halk Dilinde\" Özeti" in fallback_text
    assert "💼 Senin Portföy ve Maliyet Durumun" in fallback_text
    assert "⚖️ Artılar, Riskler ve \"Ne Yapılabilir?\" Yorumu" in fallback_text
    assert "100 lot" in fallback_text
    assert "+5,000.00 TL" in fallback_text

    # 1b) Fiyat sıfır geldiğinde çökmemeli
    zero_price_market = market.model_copy(update={"current_price": 0.0})
    zero_price_consensus = consensus.model_copy(update={"current_price": 0.0})
    zero_price_text = fallback_commentator.comment_on_stock(
        "THYAO", consensus=zero_price_consensus, market=zero_price_market, portfolio_item=portfolio_item
    )
    assert "anlık kâr/zarar hesaplanamadı" in zero_price_text

    # 2) Gemini Interactions API mock testi
    with_key_settings = Settings(gemini_api_key="AIzaFakeTestKey123", gemini_model="gemini-3.8-flash")
    ai_commentator = BorsaCommentator(settings=with_key_settings)
    mock_client = MagicMock()
    mock_interaction = MagicMock()
    mock_interaction.output_text = "Gemini 3.8 Flash test analizi."
    mock_client.interactions.create.return_value = mock_interaction
    ai_commentator._client = mock_client

    ai_text = ai_commentator.comment_on_stock(
        "THYAO", consensus=consensus, market=market, portfolio_item=portfolio_item
    )
    assert "Gemini 3.8 Flash test analizi." in ai_text
    mock_client.interactions.create.assert_called_once()
    call_kwargs = mock_client.interactions.create.call_args.kwargs
    assert call_kwargs["model"] == "gemini-3.8-flash"

    # 3) Sabah (09:30) ve akşam (18:30) günlük özet bülteni başlıkları
    evals = [{"portfolio": portfolio_item, "market": market, "consensus": consensus}]
    morning_bulletin = fallback_commentator.generate_daily_bulletin(
        portfolio_evaluations=evals,
        opportunities=[consensus],
        new_reports=consensus.recent_reports,
        session_label="morning",
    )
    assert "🌅 BIST Sabah Açılış Özet Bülteni (09:30)" in morning_bulletin
    evening_bulletin = fallback_commentator.generate_daily_bulletin(
        portfolio_evaluations=evals,
        opportunities=[consensus],
        new_reports=consensus.recent_reports,
    )
    assert "📊 BIST Günlük Kapanış Bülteni" in evening_bulletin


def test_html_table_parser_and_alert_dedup(tmp_path: Path) -> None:
    """HTML hedef fiyat ayrıştırıcıyı, serbest metin hisse yakalayıcıyı ve alert akışını doğrular."""
    html_sample = """
    <table>
      <tr><th>Hisse</th><th>Kurum</th><th>Hedef</th><th>Tavsiye</th><th>Tarih</th></tr>
      <tr><td>BIMAS</td><td>Gedik Yatırım</td><td>720,50 TL</td><td>AL</td><td>12.11.2026</td></tr>
      <tr><td>MGROS</td><td>Deniz Yatırım</td><td>650.00 TL</td><td>Endeks Üstü</td><td>12.11.2026</td></tr>
    </table>
    """
    settings = Settings(min_upside_percent=35.0, min_buy_count=2)
    collector = AnalystCollector(settings=settings)
    parsed = collector._parse_html_target_tables(html_sample, source_url="https://example.com")
    assert len(parsed) == 2
    assert parsed[0].symbol == "BIMAS"
    assert parsed[0].target_price == 720.50
    assert parsed[0].recommendation == "AL"
    assert parsed[1].recommendation == "AL"
    assert normalize_recommendation("Endeks Altı Getiri") == "SAT"

    assert _extract_symbol_from_text("thyao ne durumda") == "THYAO"
    assert _extract_symbol_from_text("merhaba nasılsın analiz") is None
    assert format_for_telegram_html("Test **kalın** <tag>") == "Test <b>kalın</b> &lt;tag&gt;"

    # Alert deduplication flow with mocked collectors
    db = DatabaseManager(tmp_path / "alerts.db")
    db.upsert_portfolio_item("BIMAS", lot=10, avg_cost=450.0)

    mock_market = MagicMock(spec=MarketCollector)
    mock_market.get_snapshot.return_value = MarketSnapshot(
        symbol="BIMAS",
        company_name="BİM Mağazalar",
        current_price=500.0,
        daily_change_pct=1.2,
        tv_recommendation="AL",
        plain_technical_comment="Teknik olumlu.",
    )
    mock_analyst = MagicMock(spec=AnalystCollector)
    mock_analyst.fetch_latest_reports.return_value = [parsed[0]]
    mock_analyst.get_consensus.return_value = AnalystConsensus(
        symbol="BIMAS",
        current_price=500.0,
        avg_target_price=720.5,
        upside_percent=44.1,
        buy_count=3,
        total_analysts=3,
    )
    mock_analyst.should_notify_report.return_value = True

    first_alerts = collect_pending_report_alerts(
        db=db,
        market_collector=mock_market,
        analyst_collector=mock_analyst,
        settings=settings,
        mark_as_notified=True,
    )
    assert len(first_alerts) == 1
    assert "BIMAS" in first_alerts[0]

    # İkinci çalışmada aynı rapor tekrar bildirilmemeli
    second_alerts = collect_pending_report_alerts(
        db=db,
        market_collector=mock_market,
        analyst_collector=mock_analyst,
        settings=settings,
        mark_as_notified=True,
    )
    assert second_alerts == []


def test_sharp_move_rsi_target_alerts_and_dedup(tmp_path: Path) -> None:
    """Sert fiyat hareketi (±4.5%), aşırı RSI, hedef yakınlığı ve günlük mükerrer alarm engellemesini doğrular."""
    db = DatabaseManager(tmp_path / "price_alerts.db")
    db.upsert_portfolio_item("THYAO", lot=100, avg_cost=250.0)
    db.upsert_portfolio_item("ASELS", lot=50, avg_cost=60.0)
    db.upsert_portfolio_item("TUPRS")

    snapshots = {
        "THYAO": MarketSnapshot(
            symbol="THYAO",
            company_name="Türk Hava Yolları",
            current_price=300.0,
            daily_change_pct=5.2,
            rsi=76.5,
            tv_recommendation="GÜÇLÜ AL",
        ),
        "ASELS": MarketSnapshot(
            symbol="ASELS",
            company_name="Aselsan",
            current_price=68.0,
            daily_change_pct=-4.9,
            rsi=28.0,
            tv_recommendation="SAT",
        ),
        "TUPRS": MarketSnapshot(
            symbol="TUPRS",
            company_name="Tüpraş",
            current_price=196.0,
            daily_change_pct=1.1,
            rsi=55.0,
            tv_recommendation="AL",
        ),
    }
    consensuses = {
        "THYAO": AnalystConsensus(symbol="THYAO", current_price=300.0, avg_target_price=400.0, upside_percent=33.3),
        "ASELS": AnalystConsensus(symbol="ASELS", current_price=68.0, avg_target_price=95.0, upside_percent=39.7),
        "TUPRS": AnalystConsensus(symbol="TUPRS", current_price=196.0, avg_target_price=200.0, upside_percent=2.0),
    }

    mock_market = MagicMock(spec=MarketCollector)
    mock_market.get_snapshot.side_effect = lambda sym, include_news=False: snapshots[sym]

    mock_analyst = MagicMock(spec=AnalystCollector)
    mock_analyst.fetch_latest_reports.return_value = []
    mock_analyst.get_consensus.side_effect = lambda sym, current_price=None: consensuses[sym]

    # 1) check_portfolio_sharp_moves doğrudan kontrolü (mark=False DB'ye yazmaz)
    raw_alerts = check_portfolio_sharp_moves(
        portfolio_items=db.list_portfolio_items(),
        market_collector=mock_market,
        analyst_collector=mock_analyst,
        db=db,
        mark=False,
    )
    alert_pairs = {(a.symbol, a.alert_type) for a in raw_alerts}
    assert ("THYAO", "SERT_YUKSELIS") in alert_pairs
    assert ("THYAO", "ISINMA_RSI") in alert_pairs
    assert ("ASELS", "SERT_DUSUS") in alert_pairs
    assert ("ASELS", "UCUZLUK_RSI") in alert_pairs
    assert ("TUPRS", "HEDEF_YAKIN") in alert_pairs
    assert db.is_price_alert_notified(raw_alerts[0].alert_id) is False

    # 2) collect_pending_report_alerts dry-run (mark_as_notified=False) -> mesaj döner ama DB işaretlemez
    settings = Settings()
    dry_alerts = collect_pending_report_alerts(
        db=db,
        market_collector=mock_market,
        analyst_collector=mock_analyst,
        settings=settings,
        mark_as_notified=False,
    )
    assert len(dry_alerts) == 5
    assert any("THYAO" in msg and "Sert Yükseliş" in msg for msg in dry_alerts)
    assert any("ASELS" in msg and "Sert Düşüş" in msg for msg in dry_alerts)
    assert any("TUPRS" in msg and ("Hedefe" in msg or "Hedef" in msg) for msg in dry_alerts)
    assert db.is_price_alert_notified(raw_alerts[0].alert_id) is False

    # 3) mark_as_notified=True -> bildirir ve aynı gün tekrar bildirmez
    sent_alerts = collect_pending_report_alerts(
        db=db,
        market_collector=mock_market,
        analyst_collector=mock_analyst,
        settings=settings,
        mark_as_notified=True,
    )
    assert len(sent_alerts) == 5
    for a in raw_alerts:
        assert db.is_price_alert_notified(a.alert_id) is True

    repeated_alerts = collect_pending_report_alerts(
        db=db,
        market_collector=mock_market,
        analyst_collector=mock_analyst,
        settings=settings,
        mark_as_notified=True,
    )
    assert repeated_alerts == []


def test_sector_peers_comparison_golden_and_dividend_leaders() -> None:
    """Sektör emsalleri, kıyaslama, Altın Hisseler ve Temettü Radarı işlevlerini doğrular."""
    sector, peers = get_sector_and_peers("thyao.is", max_peers=3)
    assert sector == "Havacılık"
    assert peers == ["PGSUS", "TAVHL", "CLEBI"]

    unknown_sector, fallback_peers = get_sector_and_peers("XYZABC", max_peers=2)
    assert unknown_sector == "Genel BIST"
    assert len(fallback_peers) == 2

    snapshots = {
        "THYAO": MarketSnapshot(
            symbol="THYAO",
            company_name="Türk Hava Yolları",
            current_price=300.0,
            daily_change_pct=1.5,
            pe_ratio=5.2,
            pb_ratio=0.9,
            dividend_yield_pct=1.5,
            tv_recommendation="GÜÇLÜ AL",
            tv_buy_count=18,
            plain_technical_comment="Teknik görünüm güçlü.",
        ),
        "PGSUS": MarketSnapshot(
            symbol="PGSUS",
            company_name="Pegasus",
            current_price=240.0,
            daily_change_pct=0.4,
            pe_ratio=8.1,
            pb_ratio=1.6,
            dividend_yield_pct=None,
            tv_recommendation="AL",
            tv_buy_count=12,
            plain_technical_comment="Trend pozitif.",
        ),
        "TUPRS": MarketSnapshot(
            symbol="TUPRS",
            company_name="Tüpraş",
            current_price=160.0,
            daily_change_pct=-0.3,
            pe_ratio=6.4,
            pb_ratio=1.8,
            dividend_yield_pct=8.75,
            last_dividend_value=14.0,
            tv_recommendation="NÖTR",
            tv_buy_count=9,
        ),
        "EREGL": MarketSnapshot(
            symbol="EREGL",
            company_name="Erdemir",
            current_price=50.0,
            daily_change_pct=0.2,
            pe_ratio=11.0,
            pb_ratio=1.1,
            dividend_yield_pct=5.25,
            tv_recommendation="AL",
            tv_buy_count=11,
        ),
    }
    consensuses = {
        "THYAO": AnalystConsensus(
            symbol="THYAO", current_price=300.0, avg_target_price=450.0,
            upside_percent=50.0, buy_count=5, hold_count=1, total_analysts=6,
        ),
        "PGSUS": AnalystConsensus(
            symbol="PGSUS", current_price=240.0, avg_target_price=384.0,
            upside_percent=60.0, buy_count=2, hold_count=2, total_analysts=4,
        ),
        "TUPRS": AnalystConsensus(
            symbol="TUPRS", current_price=160.0, avg_target_price=216.0,
            upside_percent=35.0, buy_count=3, hold_count=1, total_analysts=4,
        ),
        "EREGL": AnalystConsensus(
            symbol="EREGL", current_price=50.0, avg_target_price=60.0,
            upside_percent=20.0, buy_count=1, hold_count=3, total_analysts=4,
        ),
    }

    mock_market = MagicMock(spec=MarketCollector)
    mock_market.get_snapshot.side_effect = lambda sym, include_news=False: snapshots[sym]
    mock_analyst = MagicMock(spec=AnalystCollector)
    mock_analyst.get_consensus.side_effect = lambda sym, current_price=None: consensuses[sym]

    comp = compare_stocks(["THYAO", "PGSUS"], mock_market, mock_analyst)
    assert comp["sector"] == "Havacılık"
    assert comp["highlights"]["best_upside_symbol"] == "PGSUS"
    assert comp["highlights"]["lowest_pe_symbol"] == "THYAO"
    assert comp["highlights"]["strongest_tech_symbol"] == "THYAO"

    golden = get_golden_stocks(
        analyst_collector=mock_analyst,
        market_collector=mock_market,
        universe=["THYAO", "PGSUS", "TUPRS", "EREGL"],
        limit=5,
    )
    # EREGL buy_count=1 olduğu için elenmeli; THYAO (5*12+50=110) > PGSUS (2*12+60=84) > TUPRS (3*12+35=71)
    assert [g["symbol"] for g in golden] == ["THYAO", "PGSUS", "TUPRS"]
    assert golden[0]["score"] == 110.0

    div_leaders = get_dividend_leaders(
        market_collector=mock_market,
        analyst_collector=mock_analyst,
        universe=["THYAO", "PGSUS", "TUPRS", "EREGL"],
        limit=5,
    )
    # PGSUS temettü verimi None olduğu için hariç; TUPRS (%8.75) > EREGL (%5.25) > THYAO (%1.5)
    assert [d["symbol"] for d in div_leaders] == ["TUPRS", "EREGL", "THYAO"]
    assert div_leaders[0]["dividend_yield_pct"] == 8.75


def test_chart_generators_produce_valid_png(tmp_path: Path) -> None:
    """Hisse ve portföy grafik üreticilerinin geçerli PNG baytları döndürdüğünü doğrular."""
    base_date = datetime(2026, 9, 1)
    history = [
        ((base_date + timedelta(days=i)).strftime("%Y-%m-%d"), 270.0 + i * 0.8)
        for i in range(45)
    ]
    market = MarketSnapshot(symbol="THYAO", company_name="Türk Hava Yolları", current_price=305.2)
    consensus = AnalystConsensus(
        symbol="THYAO",
        current_price=305.2,
        avg_target_price=430.0,
        min_target_price=380.0,
        max_target_price=480.0,
        upside_percent=40.9,
        buy_count=5,
    )
    p_item = PortfolioItem(symbol="THYAO", lot=100, avg_cost=260.0)

    stock_png = generate_stock_chart_png(
        symbol="THYAO",
        market=market,
        consensus=consensus,
        portfolio_item=p_item,
        price_history=history,
    )
    assert stock_png.startswith(b"\x89PNG")
    assert len(stock_png) > 5000

    # Boş geçmişle de çökmeden geçerli PNG üretmeli
    stock_empty_png = generate_stock_chart_png(
        symbol="THYAO",
        market=market,
        consensus=consensus,
        portfolio_item=None,
        price_history=[],
    )
    assert stock_empty_png.startswith(b"\x89PNG")

    portfolio_png = generate_portfolio_chart_png(
        [
            {"portfolio": p_item, "market": market, "consensus": consensus},
            {
                "portfolio": PortfolioItem(symbol="TUPRS"),
                "market": MarketSnapshot(symbol="TUPRS", current_price=160.0),
                "consensus": AnalystConsensus(symbol="TUPRS", current_price=160.0, avg_target_price=220.0, upside_percent=37.5),
            },
        ]
    )
    assert portfolio_png.startswith(b"\x89PNG")
    assert len(portfolio_png) > 5000

    empty_portfolio_png = generate_portfolio_chart_png([])
    assert empty_portfolio_png.startswith(b"\x89PNG")


def test_commentator_comparison_free_qa_ocr_and_bulk_upsert(tmp_path: Path) -> None:
    """Karşılaştırma yorumu, serbest soru yanıtı, görsel OCR ve toplu portföy kaydını doğrular."""
    comparison_data = {
        "sector": "Havacılık",
        "items": [
            {
                "symbol": "THYAO",
                "market": MarketSnapshot(
                    symbol="THYAO", company_name="Türk Hava Yolları", current_price=300.0,
                    daily_change_pct=1.2, pe_ratio=5.5, pb_ratio=0.9, tv_recommendation="GÜÇLÜ AL",
                ),
                "consensus": AnalystConsensus(
                    symbol="THYAO", current_price=300.0, avg_target_price=435.0,
                    upside_percent=45.0, buy_count=5, hold_count=1, total_analysts=6,
                ),
            },
            {
                "symbol": "PGSUS",
                "market": MarketSnapshot(
                    symbol="PGSUS", company_name="Pegasus", current_price=240.0,
                    daily_change_pct=-0.5, pe_ratio=8.2, pb_ratio=1.6, tv_recommendation="AL",
                ),
                "consensus": AnalystConsensus(
                    symbol="PGSUS", current_price=240.0, avg_target_price=336.0,
                    upside_percent=40.0, buy_count=3, hold_count=2, total_analysts=5,
                ),
            },
        ],
        "highlights": {
            "best_upside_symbol": "THYAO",
            "lowest_pe_symbol": "THYAO",
            "strongest_tech_symbol": "THYAO",
        },
    }

    # Fallback test (API key olmadan)
    fallback_comm = BorsaCommentator(settings=Settings(gemini_api_key=""))
    comp_fallback = fallback_comm.comment_on_comparison(comparison_data)
    assert "Havacılık" in comp_fallback
    assert "THYAO" in comp_fallback and "PGSUS" in comp_fallback

    qa_fallback = fallback_comm.answer_free_question(
        question="Elimde 50 bin TL var nasıl bir sepet yapmalıyım?",
        portfolio_context="- THYAO: 100 lot @ 250 TL",
        opportunities_context="- TUPRS: %38 potansiyel",
    )
    assert "50 bin TL" in qa_fallback
    assert "THYAO" in qa_fallback
    assert "TUPRS" in qa_fallback

    # Mocked Gemini interactions.create testleri
    ai_comm = BorsaCommentator(settings=Settings(gemini_api_key="AIzaFakeTestKey123", gemini_model="gemini-3.8-flash"))
    mock_client = MagicMock()
    ai_comm._client = mock_client

    mock_client.interactions.create.return_value = MagicMock(output_text="THYAO daha ucuz çarpanlarla öne çıkıyor.")
    comp_ai = ai_comm.comment_on_comparison(comparison_data)
    assert "THYAO daha ucuz çarpanlarla öne çıkıyor." in comp_ai

    mock_client.interactions.create.return_value = MagicMock(output_text="Dengeli bir sepet oluşturabilirsin.")
    qa_ai = ai_comm.answer_free_question(
        question="Nasıl sepet yapayım?",
        portfolio_context="THYAO var",
        opportunities_context="TUPRS ve ASELS cazip",
    )
    assert "Dengeli bir sepet oluşturabilirsin." in qa_ai
    assert "yatırım tavsiyesi değildir" in qa_ai.lower()

    # OCR görsel ayrıştırma testi (fenced JSON + .IS + geçersiz satır eleme)
    ocr_json = """```json
    [
      {"symbol": "THYAO.IS", "lot": 150, "avg_cost": "285,50"},
      {"symbol": "ASELS", "lot": "40", "avg_cost": 62.25},
      {"symbol": "TRY", "lot": 1000, "avg_cost": 1.0},
      {"symbol": "SISE", "lot": -5, "avg_cost": 40.0}
    ]
    ```"""
    mock_client.interactions.create.return_value = MagicMock(output_text=ocr_json)
    extracted = ai_comm.extract_portfolio_from_image(b"fake-jpeg-bytes", mime_type="image/jpeg")
    assert extracted == [
        {"symbol": "THYAO", "lot": 150.0, "avg_cost": 285.5},
        {"symbol": "ASELS", "lot": 40.0, "avg_cost": 62.25},
    ]

    # DB bulk_upsert_portfolio
    db = DatabaseManager(tmp_path / "ocr_bulk.db")
    saved = db.bulk_upsert_portfolio(extracted)
    assert [s.symbol for s in saved] == ["THYAO", "ASELS"]
    thyao_db = db.get_portfolio_item("THYAO")
    assert thyao_db is not None
    assert thyao_db.lot == 150.0
    assert thyao_db.avg_cost == 285.5


def test_keep_alive_health_server_and_main_flags(monkeypatch) -> None:
    """Keep-alive HTTP /health sunucusunu ephemeral portta ve main.py bayraklarını doğrular."""
    server, port = start_keep_alive_server(host="127.0.0.1", port=0)
    try:
        assert port > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["status"] == "ok"
            assert payload["service"] == "borsa-ajani"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as root_resp:
            assert root_resp.status == 200

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nonexistent", timeout=3)
            assert False, "Bilinmeyen yol 404 döndürmeliydi"
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        stop_keep_alive_server(server)

    # main.py sağlık sunucusu bayrak mantığı
    monkeypatch.delenv("ENABLE_HEALTH_SERVER", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert app_main._should_start_health_server(app_main.parse_args([])) is False
    assert app_main._should_start_health_server(app_main.parse_args(["--with-health-server"])) is True

    monkeypatch.setenv("ENABLE_HEALTH_SERVER", "1")
    assert app_main._should_start_health_server(app_main.parse_args([])) is True

    monkeypatch.setenv("ENABLE_HEALTH_SERVER", "0")
    monkeypatch.setenv("PORT", "7860")
    assert app_main._should_start_health_server(app_main.parse_args([])) is False

    monkeypatch.delenv("ENABLE_HEALTH_SERVER", raising=False)
    assert app_main._should_start_health_server(app_main.parse_args([])) is True

    # --test-stock gibi tek seferlik modlarda health server başlatılmamalı
    with patch.object(app_main, "start_keep_alive_server") as mock_start, patch.object(
        app_main, "run_test_stock", return_value=0
    ) as mock_test_stock:
        rc = app_main.main(["--with-health-server", "--test-stock", "THYAO"])
        assert rc == 0
        mock_test_stock.assert_called_once()
        mock_start.assert_not_called()


def test_bot_routing_keyboards_handlers_and_application_wiring(tmp_path: Path) -> None:
    """Serbest metin yönlendirmesi, yeni klavyeler, Application handler/job sayıları ve bot akışlarını doğrular."""
    # 1) Doğal dil vs hisse kodu yönlendirmesi
    assert _extract_symbol_from_text("THYAO") == "THYAO"
    assert _extract_symbol_from_text("thyao ne durumda") == "THYAO"
    assert _extract_symbol_from_text("Elimde 50 bin TL var nasıl bir sepet yapmalıyım?") is None
    assert _extract_symbol_from_text("Portföyümdeki riskler neler?") is None

    assert classify_free_text("THYAO") == ("stock", "THYAO")
    assert classify_free_text("thyao ne durumda") == ("stock", "THYAO")
    assert classify_free_text("THYAO ne durumda") == ("stock", "THYAO")
    assert classify_free_text("ASELS analiz") == ("stock", "ASELS")
    assert classify_free_text("TUKAS ne durumda?") == ("stock", "TUKAS")
    assert classify_free_text("Bana hisse öner?") == ("qa", None)
    assert classify_free_text("Kime hisse öner?") == ("qa", None)
    assert classify_free_text("Peki ne alayım?") == ("qa", None)
    assert classify_free_text("Elimde 50 bin TL var nasıl bir sepet yapmalıyım?") == ("qa", None)
    assert classify_free_text("Portföyümdeki riskler neler?") == ("qa", None)
    assert classify_free_text("THYAO ve PGSUS arasında hangisi daha iyi?") == ("qa", None)
    assert classify_free_text("merhaba nasılsın analiz") == ("help", None)

    # 2) Menü ve aksiyon klavyeleri
    main_callbacks = [btn.callback_data for row in _main_menu_keyboard().inline_keyboard for btn in row]
    assert main_callbacks == [
        "menu:portfoy",
        "menu:firsatlar",
        "menu:altinhisseler",
        "menu:temettu",
        "menu:raporlar",
        "menu:ozet",
    ]
    stock_callbacks = [
        btn.callback_data for row in _stock_action_keyboard("THYAO", True).inline_keyboard for btn in row
    ]
    assert "chart:THYAO" in stock_callbacks
    assert "compare_sector:THYAO" in stock_callbacks
    assert "refresh:THYAO" in stock_callbacks
    assert "unwatch:THYAO" in stock_callbacks
    assert "details:THYAO" in stock_callbacks

    portfolio_callbacks = [
        btn.callback_data for row in _portfolio_summary_keyboard().inline_keyboard for btn in row
    ]
    assert "chart_portfolio" in portfolio_callbacks

    # 3) Application kurulumu (handler & job sayıları)
    settings = Settings(
        telegram_bot_token="123456:ABC-DEF_dummy_test_token",
        db_path=tmp_path / "bot_app.db",
    )
    app = build_application(settings)
    assert len(app.handlers[0]) == 14
    assert app.job_queue is not None
    jobs = app.job_queue.jobs()
    assert len(jobs) == 3
    assert {j.name for j in jobs} == {
        "intraday_report_scan",
        "daily_morning_summary",
        "daily_evening_summary",
    }

    # 4) Bot callback / fotoğraf OCR / Q&A entegrasyon testi
    db = DatabaseManager(tmp_path / "bot_flow.db")
    db.upsert_portfolio_item("THYAO", lot=100, avg_cost=260.0)

    mock_market = MagicMock(spec=MarketCollector)
    mock_market.get_snapshot.return_value = MarketSnapshot(
        symbol="THYAO",
        company_name="Türk Hava Yolları",
        current_price=300.0,
        daily_change_pct=1.8,
        pe_ratio=5.4,
        dividend_yield_pct=2.5,
        tv_recommendation="AL",
        plain_technical_comment="Teknik olumlu.",
    )
    mock_market.get_price_history.return_value = [
        ("2026-10-01", 280.0),
        ("2026-10-15", 290.0),
        ("2026-11-01", 300.0),
    ]

    mock_analyst = MagicMock(spec=AnalystCollector)
    mock_analyst.settings = settings
    mock_analyst.get_consensus.return_value = AnalystConsensus(
        symbol="THYAO",
        current_price=300.0,
        avg_target_price=430.0,
        min_target_price=390.0,
        max_target_price=470.0,
        upside_percent=43.3,
        buy_count=4,
        total_analysts=5,
    )
    mock_analyst.scan_opportunities.return_value = [
        AnalystConsensus(
            symbol="TUPRS", current_price=160.0, avg_target_price=230.0, upside_percent=43.7, buy_count=3
        )
    ]

    mock_commentator = MagicMock(spec=BorsaCommentator)
    mock_commentator.comment_on_stock.return_value = "THYAO kısa hisse analizi hazır."
    mock_commentator.extract_portfolio_from_image.return_value = [
        {"symbol": "ASELS", "lot": 50.0, "avg_cost": 65.0}
    ]
    mock_commentator.answer_free_question.return_value = "Portföyün için dengeli öneri."
    mock_commentator.comment_on_comparison.return_value = "Havacılık sektörü kıyaslaması hazır."

    def _make_wait_msg() -> MagicMock:
        m = MagicMock()
        m.delete = AsyncMock()
        m.edit_text = AsyncMock()
        return m

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=lambda **kwargs: _make_wait_msg())
    bot.send_photo = AsyncMock()

    context = MagicMock()
    context.bot = bot
    context.bot_data = {
        "settings": settings,
        "db": db,
        "market_collector": mock_market,
        "analyst_collector": mock_analyst,
        "commentator": mock_commentator,
    }
    context.user_data = {}
    context.args = ["THYAO", "PGSUS"]

    async def _run_bot_flows() -> None:
        # Fotoğraf OCR -> pending_ocr_items -> ocr_confirm -> DB kaydı
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"jpegdata"))
        photo_size = MagicMock()
        photo_size.get_file = AsyncMock(return_value=tg_file)
        photo_update = MagicMock()
        photo_update.effective_chat.id = 999
        photo_update.effective_message.photo = [photo_size]
        await handle_photo_message(photo_update, context)
        assert context.user_data["pending_ocr_items"] == [{"symbol": "ASELS", "lot": 50.0, "avg_cost": 65.0}]

        confirm_update = MagicMock()
        confirm_update.effective_chat.id = 999
        confirm_update.callback_query.data = "ocr_confirm"
        confirm_update.callback_query.answer = AsyncMock()
        confirm_update.callback_query.edit_message_reply_markup = AsyncMock()
        await handle_callback_query(confirm_update, context)
        assert "pending_ocr_items" not in context.user_data
        saved_asels = db.get_portfolio_item("ASELS")
        assert saved_asels is not None and saved_asels.lot == 50.0

        # Grafik butonları (chart:THYAO & chart_portfolio)
        chart_update = MagicMock()
        chart_update.effective_chat.id = 999
        chart_update.callback_query.data = "chart:THYAO"
        chart_update.callback_query.answer = AsyncMock()
        await handle_callback_query(chart_update, context)

        pchart_update = MagicMock()
        pchart_update.effective_chat.id = 999
        pchart_update.callback_query.data = "chart_portfolio"
        pchart_update.callback_query.answer = AsyncMock()
        await handle_callback_query(pchart_update, context)
        assert bot.send_photo.await_count == 2
        for call in bot.send_photo.await_args_list:
            assert call.kwargs["photo"].getvalue().startswith(b"\x89PNG")

        # Komutlar (/altinhisseler, /temettu, /kiyasla) + serbest soru
        cmd_update = MagicMock()
        cmd_update.effective_chat.id = 999
        cmd_update.effective_message = MagicMock()
        cmd_update.effective_message.reply_text = AsyncMock()
        await cmd_altinhisseler(cmd_update, context)
        await cmd_temettu(cmd_update, context)
        await cmd_kiyasla(cmd_update, context)
        mock_commentator.comment_on_comparison.assert_called_once()

        # Tek hisseyle /kiyasla THYAO -> sektör rakipleriyle otomatik karşılaştırma
        context.args = ["THYAO"]
        await cmd_kiyasla(cmd_update, context)
        assert mock_commentator.comment_on_comparison.call_count == 2
        single_comp_arg = mock_commentator.comment_on_comparison.call_args.args[0]
        assert single_comp_arg["sector"] == "Havacılık"
        assert [it["symbol"] for it in single_comp_arg["items"]] == ["THYAO", "PGSUS", "TAVHL", "CLEBI"]

        qa_update = MagicMock()
        qa_update.effective_chat.id = 999
        qa_update.effective_message.text = "Elimde 50 bin TL var nasıl bir sepet yapmalıyım?"
        await handle_free_text(qa_update, context)
        mock_commentator.answer_free_question.assert_called_once()

        # Kısa hisse sorgusu ("thyao ne durumda") -> analyze_stock
        stock_update = MagicMock()
        stock_update.effective_chat.id = 999
        stock_update.effective_message.text = "thyao ne durumda"
        await handle_free_text(stock_update, context)
        mock_commentator.comment_on_stock.assert_called_once()

        # Sektör kıyaslama ve menü callback'leri + ocr_cancel
        for cb_data in ("compare_sector:THYAO", "menu:altinhisseler", "menu:temettu"):
            cb_update = MagicMock()
            cb_update.effective_chat.id = 999
            cb_update.callback_query.data = cb_data
            cb_update.callback_query.answer = AsyncMock()
            await handle_callback_query(cb_update, context)
        assert mock_commentator.comment_on_comparison.call_count == 3

        context.user_data["pending_ocr_items"] = [{"symbol": "SISE", "lot": 10.0, "avg_cost": 40.0}]
        cancel_update = MagicMock()
        cancel_update.effective_chat.id = 999
        cancel_update.callback_query.data = "ocr_cancel"
        cancel_update.callback_query.answer = AsyncMock()
        cancel_update.callback_query.edit_message_reply_markup = AsyncMock()
        await handle_callback_query(cancel_update, context)
        assert "pending_ocr_items" not in context.user_data
        assert db.get_portfolio_item("SISE") is None

    asyncio.run(_run_bot_flows())
