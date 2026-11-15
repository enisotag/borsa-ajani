"""Uygulama yapılandırma ve BIST takip evreni ayarları."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BIST_UNIVERSE: list[str] = [
    "THYAO", "TUPRS", "ASELS", "EREGL", "BIMAS",
    "SISE", "AKBNK", "GARAN", "ISCTR", "YKBNK",
    "KCHOL", "SAHOL", "FROTO", "TOASO", "TCELL",
    "TTKOM", "PETKM", "HEKTS", "SASA", "ENKAI",
    "ARCLK", "MGROS", "PGSUS", "KOZAL", "KRDMD",
    "ASTOR", "KONTR", "TAVHL", "ALARK", "VESTL",
    "DOAS", "ULKER", "CCOLA", "AEFES", "EKGYO",
    "OYAKC", "CIMSA", "MAVI", "AGHOL", "SOKM",
]


@dataclass
class Settings:
    """Ortam değişkenlerinden yüklenen uygulama ayarları."""

    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip())
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "").strip())
    min_upside_percent: float = field(
        default_factory=lambda: float(os.getenv("MIN_UPSIDE_PERCENT", "35.0"))
    )
    min_buy_count: int = field(
        default_factory=lambda: int(os.getenv("MIN_BUY_COUNT", "2"))
    )
    daily_report_time: str = field(default_factory=lambda: os.getenv("DAILY_REPORT_TIME", "18:30").strip())
    morning_report_time: str = field(default_factory=lambda: os.getenv("MORNING_REPORT_TIME", "09:30").strip())
    scan_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
    )
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Europe/Istanbul").strip())
    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", "data/borsa_ajani.db")))
    bist_universe: list[str] = field(default_factory=lambda: list(DEFAULT_BIST_UNIVERSE))

    def has_valid_gemini_key(self) -> bool:
        """Gemini API anahtarının placeholder olmadığını doğrular."""
        return bool(self.gemini_api_key) and not self.gemini_api_key.startswith("buraya_")

    def has_valid_telegram_token(self) -> bool:
        """Telegram token'ının placeholder olmadığını doğrular."""
        return bool(self.telegram_bot_token) and not self.telegram_bot_token.startswith("buraya_")

    def has_valid_chat_id(self) -> bool:
        """Telegram chat id değerinin tanımlı olduğunu doğrular."""
        return bool(self.telegram_chat_id) and not self.telegram_chat_id.startswith("buraya_")


def get_settings() -> Settings:
    """Güncel ortam değişkenleriyle Settings örneği döndürür."""
    load_dotenv(override=False)
    return Settings()
