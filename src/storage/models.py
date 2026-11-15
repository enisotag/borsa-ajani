"""Borsa ajanı için temel veri modelleri."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _utc_now_iso() -> str:
    """UTC zaman damgasını ISO-8601 formatında döndürür."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_bist_symbol(value: str) -> str:
    """BIST hisse kodunu büyük harfe çevirip '.IS' son ekini temizler."""
    return str(value).strip().upper().replace(".IS", "")


class PortfolioItem(BaseModel):
    """Kullanıcının portföyündeki veya izleme listesindeki bir hisse kaydı."""

    symbol: str
    lot: float = 0.0
    avg_cost: float = 0.0
    notes: str = ""
    added_at: str = Field(default_factory=_utc_now_iso)

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalize_bist_symbol(value)

    @property
    def is_owned(self) -> bool:
        """Lot ve alış maliyeti girilmişse kullanıcının elinde hisse olduğunu belirtir."""
        return self.lot > 0 and self.avg_cost > 0

    def calculate_pnl(
        self, current_price: float, target_price: Optional[float] = None
    ) -> dict[str, Optional[float]]:
        """Güncel fiyata ve (varsa) analist hedef fiyatına göre kâr/zarar hesaplar."""
        if not self.is_owned or current_price <= 0:
            return {
                "total_cost": None,
                "current_value": None,
                "pnl_tl": None,
                "pnl_pct": None,
                "target_value": None,
                "target_pnl_tl": None,
                "target_pnl_pct": None,
            }

        total_cost = round(self.lot * self.avg_cost, 2)
        current_value = round(self.lot * current_price, 2)
        pnl_tl = round(current_value - total_cost, 2)
        pnl_pct = round(((current_price - self.avg_cost) / self.avg_cost) * 100, 2)

        target_value: Optional[float] = None
        target_pnl_tl: Optional[float] = None
        target_pnl_pct: Optional[float] = None

        if target_price is not None and target_price > 0:
            target_value = round(self.lot * target_price, 2)
            target_pnl_tl = round(target_value - total_cost, 2)
            target_pnl_pct = round(((target_price - self.avg_cost) / self.avg_cost) * 100, 2)

        return {
            "total_cost": total_cost,
            "current_value": current_value,
            "pnl_tl": pnl_tl,
            "pnl_pct": pnl_pct,
            "target_value": target_value,
            "target_pnl_tl": target_pnl_tl,
            "target_pnl_pct": target_pnl_pct,
        }


class AnalystReport(BaseModel):
    """Tek bir aracı kurum veya analist kaynaklı hedef fiyat raporu."""

    symbol: str
    institution: str
    target_price: float
    current_price: Optional[float] = None
    upside_percent: float = 0.0
    recommendation: Literal["AL", "TUT", "SAT"] = "TUT"
    report_date: str = ""
    source: str = ""
    report_id: str = ""

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalize_bist_symbol(value)

    def model_post_init(self, __context: object) -> None:
        if not self.report_id:
            self.report_id = self.build_report_id(
                symbol=self.symbol,
                institution=self.institution,
                target_price=self.target_price,
                report_date=self.report_date,
            )

    @staticmethod
    def build_report_id(
        symbol: str, institution: str, target_price: float, report_date: str
    ) -> str:
        """Mükerrer bildirimleri önlemek için deterministik rapor kimliği üretir."""
        raw_key = f"{symbol.upper()}|{institution.strip().lower()}|{target_price:.2f}|{report_date.strip()}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]


class AnalystConsensus(BaseModel):
    """Bir hisse için toplanan tüm analist ve aracı kurum raporlarının özeti."""

    symbol: str
    current_price: float = 0.0
    avg_target_price: float = 0.0
    max_target_price: float = 0.0
    min_target_price: float = 0.0
    upside_percent: float = 0.0
    buy_count: int = 0
    hold_count: int = 0
    sell_count: int = 0
    total_analysts: int = 0
    recent_reports: list[AnalystReport] = Field(default_factory=list)
    in_model_portfolios: int = 0

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalize_bist_symbol(value)


class MarketSnapshot(BaseModel):
    """Bir hissenin fiyat, temel çarpan, hazır teknik sinyal ve haber özeti."""

    symbol: str
    company_name: str = ""
    current_price: float = 0.0
    daily_change_pct: float = 0.0
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    drawdown_from_high_pct: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    last_dividend_value: Optional[float] = None
    sector_tr: str = ""
    tv_recommendation: str = "BİLİNMİYOR"
    tv_buy_count: int = 0
    tv_neutral_count: int = 0
    tv_sell_count: int = 0
    rsi: Optional[float] = None
    plain_technical_comment: str = ""
    plain_valuation_comment: str = ""
    recent_news: list[str] = Field(default_factory=list)

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalize_bist_symbol(value)


class PriceMoveAlert(BaseModel):
    """Portföy veya izleme listesindeki sert fiyat, teknik veya hedef fiyat uyarısı."""

    alert_id: str = ""
    symbol: str
    alert_type: Literal[
        "SERT_YUKSELIS",
        "SERT_DUSUS",
        "UCUZLUK_RSI",
        "ISINMA_RSI",
        "HEDEF_YAKIN",
    ]
    current_price: float
    daily_change_pct: float = 0.0
    message: str
    alert_date: str

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        return _normalize_bist_symbol(value)

    def model_post_init(self, __context: object) -> None:
        if not self.alert_id:
            self.alert_id = self.build_alert_id(
                symbol=self.symbol,
                alert_type=self.alert_type,
                alert_date=self.alert_date,
            )

    @staticmethod
    def build_alert_id(symbol: str, alert_type: str, alert_date: str) -> str:
        """Mükerrer uyarıları önlemek için deterministik uyarı kimliği üretir."""
        clean_symbol = _normalize_bist_symbol(symbol)
        raw_key = f"{clean_symbol}|{alert_type.strip()}|{alert_date.strip()}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
