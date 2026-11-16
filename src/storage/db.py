"""SQLite tabanlı portföy ve bildirim geçmişi yöneticisi."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import logging
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional

import httpx

from .models import AnalystReport, PortfolioItem, PriceMoveAlert

if TYPE_CHECKING:
    from src.config import Settings

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().replace(".IS", "")


def parse_initial_portfolio_string(raw: str) -> list[dict[str, Any]]:
    """`THYAO:100:285,TUPRS:50:162.5,ASELS` biçimindeki metni portföy kayıt listesine çevirir."""
    if not raw or not raw.strip():
        return []

    raw_chunks = re.split(r"[;\n]+|,(?=\s*[A-Za-z])", raw.strip())
    entries_by_symbol: dict[str, dict[str, Any]] = {}

    for chunk in raw_chunks:
        cleaned_chunk = chunk.strip()
        if not cleaned_chunk:
            continue
        parts = [p for p in re.split(r"[:\s]+", cleaned_chunk) if p]
        if not parts:
            continue
        symbol = _normalize_symbol(parts[0])
        if len(symbol) < 2 or not symbol.isalnum():
            continue

        lot = DatabaseManager._parse_nonneg_float(parts[1], default=0.0) if len(parts) >= 2 else 0.0
        avg_cost = (
            DatabaseManager._parse_nonneg_float(parts[2], default=0.0) if len(parts) >= 3 else 0.0
        )
        entries_by_symbol[symbol] = {
            "symbol": symbol,
            "lot": lot,
            "avg_cost": avg_cost,
        }

    return list(entries_by_symbol.values())


def format_portfolio_env_string(items: list[PortfolioItem]) -> str:
    """Portföy listesini `INITIAL_PORTFOLIO` ortam değişkenine uygun tek satır metne çevirir."""
    parts: list[str] = []
    for item in items:
        if item.is_owned:
            parts.append(f"{item.symbol}:{item.lot:g}:{item.avg_cost:g}")
        else:
            parts.append(item.symbol)
    return ",".join(parts)


class DatabaseManager:
    """Portföy, izleme listesi ve gönderilmiş rapor kayıtlarını SQLite'ta yönetir."""

    def __init__(
        self,
        db_path: str | Path = "data/borsa_ajani.db",
        initial_portfolio: str = "",
        github_token: str = "",
        github_repo: str = "enisotag/borsa-ajani",
        github_portfolio_path: str = "data/portfolio_backup.json",
        auto_bootstrap: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_portfolio = (initial_portfolio or "").strip()
        self.github_token = (github_token or "").strip()
        self.github_repo = (github_repo or "enisotag/borsa-ajani").strip()
        clean_gh_path = (github_portfolio_path or "data/portfolio_backup.json").strip().lstrip("/")
        self.github_portfolio_path = clean_gh_path or "data/portfolio_backup.json"
        self._sync_lock = threading.Lock()
        self._github_file_sha: Optional[str] = None
        self.last_sync_error: Optional[str] = None
        self._init_schema()
        if auto_bootstrap:
            self.bootstrap_portfolio()

    @classmethod
    def from_settings(cls, settings: Settings) -> DatabaseManager:
        """Uygulama ayarlarından kalıcılık ve GitHub yedeği etkin DatabaseManager oluşturur."""
        token = settings.github_token if settings.has_valid_github_token() else ""
        return cls(
            db_path=settings.db_path,
            initial_portfolio=settings.initial_portfolio,
            github_token=token,
            github_repo=settings.github_repo,
            github_portfolio_path=settings.github_portfolio_path,
            auto_bootstrap=True,
        )

    @property
    def has_github_sync(self) -> bool:
        """GitHub yedekleme/geri yükleme senkronizasyonunun yapılandırılıp yapılandırılmadığını döner."""
        return (
            bool(self.github_token)
            and not self.github_token.startswith("buraya_")
            and bool(self.github_repo)
        )

    def _github_contents_url(self) -> str:
        return f"https://api.github.com/repos/{self.github_repo}/contents/{self.github_portfolio_path}"

    def _github_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _fetch_github_backup_raw(
        self, client: httpx.Client
    ) -> tuple[Optional[dict[str, Any]], bool]:
        """GitHub Contents API'den mevcut yedek dosyasını okur; (json_data, exists) döndürür."""
        resp = client.get(self._github_contents_url(), headers=self._github_headers())
        if resp.status_code == 404:
            self._github_file_sha = None
            return None, False
        resp.raise_for_status()
        data = resp.json()
        self._github_file_sha = data.get("sha")
        return data, True

    def _restore_from_github(self) -> tuple[list[PortfolioItem], bool]:
        """GitHub'daki yedek JSON dosyasından portföyü geri yükler."""
        if not self.has_github_sync:
            return [], False
        try:
            with httpx.Client(timeout=8.0) as client:
                data, exists = self._fetch_github_backup_raw(client)
            if not exists or not data:
                return [], False

            encoded_content = str(data.get("content") or "").replace("\n", "")
            if not encoded_content:
                return [], True
            decoded_text = base64.b64decode(encoded_content).decode("utf-8")
            parsed = json.loads(decoded_text)
            raw_items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
            if not isinstance(raw_items, list):
                return [], True
            restored = self.bulk_upsert_portfolio(raw_items, sync=False)
            self.last_sync_error = None
            logger.info("GitHub yedeğinden %d portföy/izleme kaydı geri yüklendi.", len(restored))
            return restored, True
        except Exception as exc:
            self.last_sync_error = str(exc)
            logger.warning("GitHub portföy yedeği okunamadı: %s", exc)
            return [], False

    def _sync_to_github(self) -> bool:
        """Güncel portföyü GitHub deposundaki yedek JSON dosyasına kaydeder."""
        if not self.has_github_sync:
            return False

        with self._sync_lock:
            items = self.list_portfolio_items()
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "env_string": format_portfolio_env_string(items),
                "items": [
                    {
                        "symbol": item.symbol,
                        "lot": item.lot,
                        "avg_cost": item.avg_cost,
                        "notes": item.notes,
                        "added_at": item.added_at,
                    }
                    for item in items
                ],
            }
            content_b64 = base64.b64encode(
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            ).decode("ascii")

            try:
                with httpx.Client(timeout=8.0) as client:
                    if self._github_file_sha is None:
                        try:
                            self._fetch_github_backup_raw(client)
                        except Exception as pre_exc:
                            logger.debug("GitHub ön SHA kontrolü başarısız: %s", pre_exc)

                    for attempt in range(2):
                        body: dict[str, Any] = {
                            "message": "chore: auto-sync portfolio backup [skip ci]",
                            "content": content_b64,
                        }
                        if self._github_file_sha:
                            body["sha"] = self._github_file_sha

                        resp = client.put(
                            self._github_contents_url(),
                            headers=self._github_headers(),
                            json=body,
                        )
                        if resp.status_code in (200, 201):
                            new_sha = resp.json().get("content", {}).get("sha")
                            if new_sha:
                                self._github_file_sha = new_sha
                            self.last_sync_error = None
                            return True

                        if resp.status_code in (409, 422) and attempt == 0:
                            self._fetch_github_backup_raw(client)
                            continue

                        self.last_sync_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        logger.warning(
                            "GitHub portföy yedeği kaydedilemedi: %s", self.last_sync_error
                        )
                        return False
            except Exception as exc:
                self.last_sync_error = str(exc)
                logger.warning("GitHub portföy senkronizasyonu hatası: %s", exc)
                return False

        return False

    def bootstrap_portfolio(self) -> None:
        """Yerel veritabanı boşsa GitHub yedeğinden veya INITIAL_PORTFOLIO ayarından portföyü kurar."""
        if self.list_portfolio_items():
            return

        github_had_backup = False
        if self.has_github_sync:
            _, github_had_backup = self._restore_from_github()

        if not github_had_backup and self.initial_portfolio:
            seed_items = parse_initial_portfolio_string(self.initial_portfolio)
            if seed_items:
                saved = self.bulk_upsert_portfolio(seed_items, sync=False)
                logger.info(
                    "INITIAL_PORTFOLIO üzerinden %d portföy/izleme kaydı yüklendi.",
                    len(saved),
                )
                if self.has_github_sync and saved:
                    self._sync_to_github()

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
        sync: bool = True,
    ) -> PortfolioItem:
        """Portföye/izleme listesine hisse ekler veya mevcut kaydı günceller."""
        clean_symbol = _normalize_symbol(symbol)
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

        if sync and self.has_github_sync:
            self._sync_to_github()

        return PortfolioItem(
            symbol=clean_symbol,
            lot=float(lot),
            avg_cost=float(avg_cost),
            notes=notes,
            added_at=added_at,
        )

    def remove_portfolio_item(self, symbol: str, sync: bool = True) -> bool:
        """Belirtilen hisseyi portföy/izleme listesinden siler."""
        clean_symbol = _normalize_symbol(symbol)
        with self._session() as conn:
            cursor = conn.execute(
                "DELETE FROM watchlist_portfolio WHERE symbol = ?",
                (clean_symbol,),
            )
            conn.commit()
            removed = cursor.rowcount > 0
        if removed and sync and self.has_github_sync:
            self._sync_to_github()
        return removed

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

    def bulk_upsert_portfolio(
        self, items: list[dict[str, Any]], sync: bool = True
    ) -> list[PortfolioItem]:
        """Birden fazla portföy kaydını toplu olarak ekler veya günceller."""
        saved: list[PortfolioItem] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            raw_symbol = entry.get("symbol")
            if raw_symbol is None:
                continue
            clean_symbol = _normalize_symbol(str(raw_symbol))
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
                    sync=False,
                )
            )
        if saved and sync and self.has_github_sync:
            self._sync_to_github()
        return saved
