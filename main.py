"""Borsa İstanbul (BIST) Yapay Zeka Analist ve Bildirim Ajanı giriş noktası."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from telegram import Bot

from src.ai.commentator import BorsaCommentator
from src.bot.telegram_bot import run_telegram_bot
from src.collectors.analyst_collector import AnalystCollector
from src.collectors.market_collector import MarketCollector
from src.config import Settings, get_settings
from src.scheduler.jobs import (
    analyze_stock,
    build_daily_bulletin_text,
    collect_pending_report_alerts,
    send_telegram_text,
)
from src.server.keep_alive import start_keep_alive_server, stop_keep_alive_server
from src.storage.db import DatabaseManager


def _configure_logging_and_stdout() -> None:
    """Windows konsolunda Türkçe karakterlerin düzgün basılmasını ve loglamayı ayarlar."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )


async def _send_cli_messages(settings: Settings, messages: list[str]) -> None:
    """Tek seferlik çalıştırmalarda Telegram'a mesaj gönderir."""
    if not settings.has_valid_telegram_token() or not settings.has_valid_chat_id():
        raise ValueError(
            "Telegram'a gönderim için geçerli TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID gereklidir. "
            "Sadece ekranda görmek için --dry-run kullanabilirsiniz."
        )
    bot = Bot(token=settings.telegram_bot_token)
    async with bot:
        for msg in messages:
            await send_telegram_text(bot, settings.telegram_chat_id, msg)


def run_test_stock(
    symbol: str,
    lot: Optional[float],
    cost: Optional[float],
    settings: Settings,
) -> int:
    """Seçilen tek hisse için canlı veri çekip AI/şablon yorumunu konsola basar."""
    db = DatabaseManager.from_settings(settings)
    market_collector = MarketCollector()
    analyst_collector = AnalystCollector(settings=settings)
    commentator = BorsaCommentator(settings=settings)

    text, _, _, _ = analyze_stock(
        symbol=symbol,
        db=db,
        market_collector=market_collector,
        analyst_collector=analyst_collector,
        commentator=commentator,
        override_lot=lot,
        override_cost=cost,
    )
    print(text)
    return 0


def run_once_summary(dry_run: bool, settings: Settings) -> int:
    """Günlük özet bültenini tek seferlik çalıştırır."""
    db = DatabaseManager.from_settings(settings)
    market_collector = MarketCollector()
    analyst_collector = AnalystCollector(settings=settings)
    commentator = BorsaCommentator(settings=settings)

    bulletin = build_daily_bulletin_text(
        db=db,
        market_collector=market_collector,
        analyst_collector=analyst_collector,
        commentator=commentator,
        settings=settings,
    )
    if dry_run:
        print(bulletin)
    else:
        asyncio.run(_send_cli_messages(settings, [bulletin]))
        print("Günlük özet Telegram'a gönderildi.")
    return 0


def run_once_scan(dry_run: bool, settings: Settings) -> int:
    """Yeni analist raporlarını ve BIST fırsatlarını tek seferlik tarar."""
    db = DatabaseManager.from_settings(settings)
    market_collector = MarketCollector()
    analyst_collector = AnalystCollector(settings=settings)

    alerts = collect_pending_report_alerts(
        db=db,
        market_collector=market_collector,
        analyst_collector=analyst_collector,
        settings=settings,
        mark_as_notified=not dry_run,
    )
    if dry_run:
        if not alerts:
            print("Filtreye takılan yeni bildirim adayı bulunamadı (veya tümü daha önce bildirildi).")
        else:
            for idx, alert in enumerate(alerts, start=1):
                print(f"--- Bildirim #{idx} ---")
                print(alert)
                print()
    else:
        if alerts:
            asyncio.run(_send_cli_messages(settings, alerts))
            print(f"{len(alerts)} adet bildirim Telegram'a gönderildi.")
        else:
            print("Gönderilecek yeni bildirim bulunamadı.")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Borsa İstanbul (BIST) Analist Raporu, Fırsat Radarı ve Portföy Ajanı"
    )
    parser.add_argument(
        "--test-stock",
        type=str,
        help="Telegram olmadan seçilen hisseyi test eder (ör. --test-stock THYAO)",
    )
    parser.add_argument("--lot", type=float, default=None, help="--test-stock için örnek lot sayısı")
    parser.add_argument("--cost", type=float, default=None, help="--test-stock için örnek alış maliyeti")
    parser.add_argument(
        "--once-summary",
        action="store_true",
        help="Günlük kapanış özet bültenini tek seferlik üretir",
    )
    parser.add_argument(
        "--once-scan",
        action="store_true",
        help="Yeni analist raporları ve BIST fırsatlarını tek seferlik tarar",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="--once-summary veya --once-scan çıktılarını Telegram'a atmadan konsola yazar",
    )
    parser.add_argument(
        "--with-health-server",
        action="store_true",
        help="Telegram botuyla birlikte /health keep-alive HTTP sunucusunu başlatır",
    )
    return parser.parse_args(argv)


def _should_start_health_server(args: argparse.Namespace) -> bool:
    """Keep-alive HTTP sunucusunun başlatılıp başlatılmayacağını belirler."""
    if getattr(args, "with_health_server", False):
        return True
    enable_flag = os.getenv("ENABLE_HEALTH_SERVER", "").strip().lower()
    if enable_flag in {"1", "true", "yes", "on"}:
        return True
    if enable_flag in {"0", "false", "no", "off"}:
        return False
    return bool(os.getenv("PORT", "").strip())


def main(argv: Optional[list[str]] = None) -> int:
    _configure_logging_and_stdout()
    args = parse_args(argv)
    settings = get_settings()

    if args.test_stock:
        return run_test_stock(args.test_stock, args.lot, args.cost, settings)
    if args.once_summary:
        return run_once_summary(args.dry_run, settings)
    if args.once_scan:
        return run_once_scan(args.dry_run, settings)

    health_server = None
    if _should_start_health_server(args):
        health_server, bound_port = start_keep_alive_server()
        logging.getLogger(__name__).info(
            "Keep-alive HTTP sağlık sunucusu port %d üzerinde başlatıldı.", bound_port
        )

    try:
        run_telegram_bot(settings)
    finally:
        stop_keep_alive_server(health_server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
