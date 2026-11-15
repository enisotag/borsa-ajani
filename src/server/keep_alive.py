"""Render ve Hugging Face Spaces gibi bulut platformları için keep-alive HTTP sunucusu."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SELF_PING_INTERVAL_SECONDS = 600
DEFAULT_PORT = 7860


class _KeepAliveServer(ThreadingHTTPServer):
    """Başlangıç zamanı ve kapanış sinyali taşıyan ThreadingHTTPServer alt sınıfı."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
    ) -> None:
        self.started_at: float = time.monotonic()
        self.stop_event: threading.Event = threading.Event()
        self.serve_thread: Optional[threading.Thread] = None
        self.ping_thread: Optional[threading.Thread] = None
        super().__init__(server_address, request_handler_class)


class _KeepAliveRequestHandler(BaseHTTPRequestHandler):
    """`/` ve `/health` uç noktalarına 200 JSON yanıtı döndüren işleyici."""

    server: _KeepAliveServer

    def _build_health_body(self) -> bytes:
        uptime_seconds = round(max(0.0, time.monotonic() - self.server.started_at), 2)
        payload = {
            "status": "ok",
            "service": "borsa-ajani",
            "uptime_seconds": uptime_seconds,
        }
        return json.dumps(payload).encode("utf-8")

    def _write_json(self, status_code: int, body: bytes, send_body: bool = True) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _handle_request(self, send_body: bool) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/health"):
            self._write_json(200, self._build_health_body(), send_body=send_body)
            return
        not_found = json.dumps({"status": "not_found"}).encode("utf-8")
        self._write_json(404, not_found, send_body=send_body)

    def do_GET(self) -> None:
        """GET / ve GET /health isteklerini yanıtlar."""
        self._handle_request(send_body=True)

    def do_HEAD(self) -> None:
        """HEAD / ve HEAD /health isteklerini yanıtlar."""
        self._handle_request(send_body=False)

    def log_message(self, format: str, *args: object) -> None:
        """Varsayılan stderr erişim loglarını debug düzeyine yönlendirir."""
        logger.debug("Keep-alive HTTP: " + format, *args)


def _resolve_port(port: Optional[int]) -> int:
    """Açıkça verilen portu veya PORT ortam değişkenini çözümler."""
    if port is not None:
        return int(port)
    raw_port = os.getenv("PORT", str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
    return int(raw_port)


def _resolve_self_ping_url() -> Optional[str]:
    """KEEP_ALIVE_URL veya RENDER_EXTERNAL_URL tanımlıysa tam /health URL'ini döndürür."""
    base_url = (
        os.getenv("KEEP_ALIVE_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    )
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/health"


def _ping_health_url(target_url: str) -> None:
    """Hedef sağlık uç noktasına HTTP GET atar; tüm hataları sessizce yutar."""
    try:
        req = urllib.request.Request(
            target_url,
            headers={"User-Agent": "borsa-ajani-keep-alive/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(128)
        logger.debug("Keep-alive self-ping başarılı: %s", target_url)
    except Exception as exc:
        logger.debug("Keep-alive self-ping hatası (%s): %s", target_url, exc)


def _self_ping_loop(
    target_url: str,
    stop_event: threading.Event,
    interval_seconds: float = SELF_PING_INTERVAL_SECONDS,
) -> None:
    """Sunucu durdurulana kadar belirli aralıklarla kendi /health uç noktasını pingler."""
    while not stop_event.wait(timeout=interval_seconds):
        _ping_health_url(target_url)


def start_keep_alive_server(
    host: str = "0.0.0.0",
    port: Optional[int] = None,
) -> tuple[ThreadingHTTPServer, int]:
    """Arka plan daemon iş parçacığında hafif HTTP health sunucusunu başlatır.

    Args:
        host: Dinlenecek ağ adresi (varsayılan ``"0.0.0.0"``).
        port: Dinlenecek port. ``None`` ise ``PORT`` ortam değişkeni (varsayılan ``7860``)
            kullanılır. ``0`` geçilirse işletim sisteminin atadığı geçici port döndürülür.

    Returns:
        ``(server, actual_port)`` ikilisi.
    """
    resolved_port = _resolve_port(port)
    server = _KeepAliveServer((host, resolved_port), _KeepAliveRequestHandler)
    actual_port = int(server.server_address[1])

    serve_thread = threading.Thread(
        target=server.serve_forever,
        name=f"keep-alive-http-{actual_port}",
        daemon=True,
    )
    server.serve_thread = serve_thread
    serve_thread.start()

    self_ping_url = _resolve_self_ping_url()
    if self_ping_url:
        ping_thread = threading.Thread(
            target=_self_ping_loop,
            args=(self_ping_url, server.stop_event),
            name="keep-alive-self-ping",
            daemon=True,
        )
        server.ping_thread = ping_thread
        ping_thread.start()
        logger.info("Keep-alive self-ping etkin: %s", self_ping_url)

    logger.info("Keep-alive HTTP sunucusu dinliyor: http://%s:%d/health", host, actual_port)
    return server, actual_port


def stop_keep_alive_server(server: Optional[ThreadingHTTPServer]) -> None:
    """Keep-alive HTTP sunucusunu ve varsa self-ping döngüsünü temizce durdurur."""
    if server is None:
        return

    stop_event = getattr(server, "stop_event", None)
    if isinstance(stop_event, threading.Event):
        stop_event.set()

    try:
        server.shutdown()
    except Exception:
        pass

    try:
        server.server_close()
    except Exception:
        pass

    serve_thread = getattr(server, "serve_thread", None)
    if isinstance(serve_thread, threading.Thread) and serve_thread.is_alive():
        serve_thread.join(timeout=2.0)
