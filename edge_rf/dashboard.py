"""Local dashboard server for the RF signal monitor."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class DashboardState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, object] = {
            "status": "warming",
            "message": "Building baseline",
            "updated_at": None,
            "sample_count": 0,
            "strongest": [],
            "active_incidents": [],
            "recent_events": [],
            "recent_observations": [],
            "recent_peaks": [],
            "strongest_incidents": [],
            "series": [],
            "tuning": {},
            "label_prompt": "",
            "threshold_db": 0,
            "range": "",
        }

    def update(self, **values: object) -> None:
        with self._lock:
            self._state.update(values)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state)


def dashboard_html() -> bytes:
    return (Path(__file__).resolve().parent / "dashboard.html").read_bytes()


def start_dashboard(
    state: DashboardState,
    host: str,
    port: int,
    mark_observation,
    select_tune,
    load_analysis,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/state":
                body = json.dumps(state.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/mark":
                params = parse_qs(parsed.query)
                label = params.get("label", ["manual_marker"])[0]
                marker = mark_observation(label)
                body = json.dumps(marker).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/select-tune":
                params = parse_qs(parsed.query)
                tune = params.get("tune", [""])[0]
                result = select_tune(tune)
                status = 200 if result.get("ok") else 400
                body = json.dumps(result).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/analysis":
                result = load_analysis()
                status = 200 if result.get("ok") else 500
                body = json.dumps(result).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path in {"/", "/index.html"}:
                body = dashboard_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
