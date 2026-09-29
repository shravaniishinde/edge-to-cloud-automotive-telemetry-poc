"""
Standard-library HTTP server for the dashboard -- no web framework
dependency. Endpoints:

  GET  /                      the single-page UI (dashboard/frontend/)
  GET  /static/<file>         only the allowlisted frontend files
  GET  /api/health            liveness
  GET  /api/state[?vehicle=]  one JSON snapshot
  GET  /api/stream[?vehicle=] Server-Sent Events: a snapshot every STREAM_INTERVAL_SECONDS
  POST /api/actions/<action>  one of controls.ACTIONS; requires the
                              "X-Dashboard-Action: 1" header (a browser
                              cannot add it cross-origin without a CORS
                              preflight, which this server never grants)

Binds to 127.0.0.1 by default. Serves no secrets: the snapshot contains
only telemetry, gateway metrics/state, allowlisted log fields, and the
broker host/port.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from dashboard.backend.controls import ACTIONS, ActionError, DemoController
from dashboard.backend.sources import LogTap, TelemetrySubscriber
from dashboard.backend.state import DashboardState

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
STREAM_INTERVAL_SECONDS = 1.0
ACTION_HEADER = "X-Dashboard-Action"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:",
    "Cache-Control": "no-store",
}


class _DashboardHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer sets SO_REUSEADDR, which on Windows lets a second
    process bind a port that is already in use, so two dashboards would
    silently share it. On Windows, claim the port exclusively instead, so a
    second instance fails fast with "address in use". POSIX keeps the
    default (there SO_REUSEADDR does not allow two live listeners)."""

    daemon_threads = True
    if os.name == "nt":
        allow_reuse_address = False

        def server_bind(self) -> None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


class Dashboard:
    """Owns the state, the read-only sources, the demo controller, and
    the HTTP server. `start()` returns once the server is listening."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8080, mqtt_host: str = "localhost",
                 mqtt_port: int = 1883, external_buffer: Optional[str] = None,
                 scripted_overrides: Optional[Dict[str, Any]] = None) -> None:
        self.state = DashboardState()
        self.subscriber = TelemetrySubscriber(self.state, mqtt_host, mqtt_port)
        self.log_tap = LogTap(self.state)
        self.controller = DemoController(self.state, mqtt_host, mqtt_port, external_buffer=external_buffer,
                                         scripted_overrides=scripted_overrides)
        self.stopping = threading.Event()
        handler = type("BoundHandler", (_Handler,), {"dashboard": self})
        self.httpd = _DashboardHTTPServer((host, port), handler)  # raises OSError if the port is taken
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.log_tap.attach("edge_gateway")
        self.subscriber.start()
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        daemon=True, name="dashboard-http")
        self._thread.start()
        self.state.add_activity("dashboard", "INFO", "dashboard started",
                                {"broker": f"{self.subscriber.host}:{self.subscriber.port}"})

    def stop(self) -> None:
        self.stopping.set()
        self.controller.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.subscriber.stop()
        self.log_tap.detach()

    def snapshot(self, vehicle_id: Optional[str] = None) -> Dict[str, Any]:
        snap = self.state.snapshot(vehicle_id)
        snap["broker"] = self.subscriber.status()
        snap["gateway"] = self.controller.status()
        return snap


class _Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard  # bound per server instance
    server_version = "TelemetryDashboard/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args) -> None:  # keep the console quiet
        pass

    # --- helpers
    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"), "application/json")

    def _vehicle_param(self) -> Optional[str]:
        values = parse_qs(urlparse(self.path).query).get("vehicle")
        return values[0][:100] if values else None

    # --- GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, (FRONTEND_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/static/") and path[len("/static/"):] in STATIC_FILES:
            name = path[len("/static/"):]
            self._send(HTTPStatus.OK, (FRONTEND_DIR / name).read_bytes(), STATIC_FILES[name])
        elif path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True})
        elif path == "/api/state":
            self._json(HTTPStatus.OK, self.dashboard.snapshot(self._vehicle_param()))
        elif path == "/api/stream":
            self._stream()
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        vehicle = self._vehicle_param()
        stopping = self.dashboard.stopping
        try:
            while not stopping.is_set():
                data = json.dumps(self.dashboard.snapshot(vehicle), default=str)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                stopping.wait(STREAM_INTERVAL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return  # the browser went away

    # --- POST
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/actions/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        action = path[len("/api/actions/"):]
        if action not in ACTIONS:
            self._json(HTTPStatus.NOT_FOUND, {"error": f"unknown action {action!r}"})
            return
        if self.headers.get(ACTION_HEADER) != "1":
            self._json(HTTPStatus.FORBIDDEN, {"error": f"missing {ACTION_HEADER} header"})
            return
        try:
            message = self.dashboard.controller.perform(action)
        except ActionError as exc:
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
            return
        self._json(HTTPStatus.OK, {"ok": True, "message": message})
