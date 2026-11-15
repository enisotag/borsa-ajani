"""SQLite tabanlı portföy ve bildirim geçmişi yöneticisi."""

from __future__ import annotations

from contextlib import contextmanager
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import AnalystReport, PortfolioItem, PriceMoveAlert


class DatabaseManager:
    """Portföy, izleme listesi ve gönderilmiş rapor kayıtlarını SQLite'ta yönetir."""

    def __init__(self, db_path: str | Path = "data/borsa_ajani.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._session() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist_portfolio (
                    symbol TEXT PRIMARY KEY,
                    lot REAL NOT NULL DEFAULT 0.0,
                    avg_cost REAL NOT NULL DEFAULT 0.0,
                    notes TEXT NOT NULL DEFAULT '',
                    added_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notified_reports (
                    report_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    institution TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    recommendation TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    notified_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notified_price_alerts (
                    alert_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    alert_date TEXT NOT NULL,
                    notified_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def upsert_portfolio_item(
        self,
        symbol: str,
        lot: float = 0.0,
        avg_cost: float = 0.0,
        notes: str = "",
    ) -> PortfolioItem:
        """Portföye/izleme listesine hisse ekler veya mevcut kaydı günceller."""
        clean_symbol = symbol.strip().upper().replace(".IS", "")
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._session() as conn:
            existing = conn.execute(
                "SELECT added_at FROM watchlist_portfolio WHERE symbol = ?",
                (clean_symbol,),
            ).fetchone()
            added_at = existing["added_at"] if existing else now_iso

            conn.execute(
                """
                INSERT INTO watchlist_portfolio (symbol, lot, avg_cost, notes, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    lot = excluded.lot,
                    avg_cost = excluded.avg_cost,
                    notes = excluded.notes
                """,
                (clean_symbol, float(lot), float(avg_cost), notes, added_at),
            )
            conn.commit()

        return PortfolioItem(
            symbol=clean_symbol,
            lot=float(lot),
            avg_cost=float(avg_cost),
            notes=notes,
            added_at=added_at,
        )

    def remove_portfolio_item(self, symbol: str) -> bool:
        """Belirtilen hisseyi portföy/izleme listesinden siler."""
        clean_symbol = symbol.strip().upper().replace(".IS", "")
        with self._session() as conn:
            cursor = conn.execute(
                "DELETE FROM watchlist_portfolio WHERE symbol = ?",
                (clean_symbol,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_portfolio_item(self, symbol: str) -> Optional[PortfolioItem]:
        """Belirtilen hissenin portföy kaydını döndürür."""
        clean_symbol = symbol.strip().upper().replace(".IS", "")
        with self._session() as conn:
            row = conn.execute(
                "SELECT symbol, lot, avg_cost, notes, added_at FROM watchlist_portfolio WHERE symbol = ?",
                (clean_symbol,),
            ).fetchone()
        if not row:
            return None
        return PortfolioItem(**dict(row))

    def list_portfolio_items(self) -> list[PortfolioItem]:
        """Tüm portföy ve izleme listesi kayıtlarını döndürür."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT symbol, lot, avg_cost, notes, added_at FROM watchlist_portfolio ORDER BY added_at ASC"
            ).fetchall()
        return [PortfolioItem(**dict(row)) for row in rows]

    def is_report_notified(self, report_id: str) -> bool:
        """Verilen rapor kimliğinin daha önce kullanıcıya bildirilip bildirilmediğini kontrol eder."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM notified_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return row is not None

    def mark_report_notified(self, report: AnalystReport) -> None:
        """Gönderilen aracı kurum raporunu mükerrer bildirim olmaması için kaydeder."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._session() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO notified_reports (
                    report_id, symbol, institution, target_price, recommendation, report_date, notified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.symbol,
                    report.institution,
                    float(report.target_price),
                    report.recommendation,
                    report.report_date,
                    now_iso,
                ),
            )
            conn.commit()

    def is_price_alert_notified(self, alert_id: str) -> bool:
        """Verilen fiyat/teknik uyarı kimliğinin daha önce bildirilip bildirilmediğini kontrol eder."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM notified_price_alerts WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
        return row is not None

    def mark_price_alert_notified(self, alert: PriceMoveAlert) -> None:
        """Gönderilen fiyat/teknik uyarısını mükerrer bildirim olmaması için kaydeder."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._session() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO notified_price_alerts (
                    alert_id, symbol, alert_type, alert_date, notified_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    alert.alert_id,
                    alert.symbol,
                    alert.alert_type,
                    alert.alert_date,
                    now_iso,
                ),
            )
            conn.commit()

    @staticmethod
    def _parse_nonneg_float(value: Any, default: float = 0.0) -> float:
        if value is None or value == "":
            return default
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = float(value)
        else:
            text = str(value).strip()
            if "," in text and "." in text:
                if text.rfind(",") > text.rfind("."):
                    text = text.replace(".", "").replace(",", ".")
                else:
                    text = text.replace(",", "")
            elif "," in text:
                text = text.replace(",", ".")
            try:
                parsed = float(text)
            except ValueError:
                return default
        if not math.isfinite(parsed) or parsed < 0:
            return default
        return parsed

    def bulk_upsert_portfolio(self, items: list[dict[str, Any]]) -> list[PortfolioItem]:
        """Birden fazla portföy kaydını toplu olarak ekler veya günceller."""
        saved: list[PortfolioItem] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            raw_symbol = entry.get("symbol")
            if raw_symbol is None:
                continue
            clean_symbol = str(raw_symbol).strip().upper().replace(".IS", "")
            if not clean_symbol or not clean_symbol.isalnum():
                continue

            lot = self._parse_nonneg_float(entry.get("lot"), default=0.0)
            avg_cost = self._parse_nonneg_float(entry.get("avg_cost"), default=0.0)
            notes = str(entry.get("notes") or "")
            saved.append(
                self.upsert_portfolio_item(
                    symbol=clean_symbol,
                    lot=lot,
                    avg_cost=avg_cost,
                    notes=notes,
                )
            )
        return saved
