"""Local dashboard server for the RF signal monitor."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from edge_rf.tuning import tuning_payload


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


def dashboard_config_payload(args) -> dict[str, object]:
    return {
        "threshold_db": args.threshold_db,
        "incident_min_power_db": args.incident_min_power_db,
        "warmup_samples": args.warmup_samples,
        "range": args.range,
        "absolute_strong_db": args.absolute_strong_db,
        "absolute_extreme_db": args.absolute_extreme_db,
        "cluster_khz": args.cluster_khz,
        "tuning": tuning_payload(args),
        "demo": args.demo,
    }


def dashboard_reset_payload(args) -> dict[str, object]:
    return {
        **dashboard_config_payload(args),
        "pending_tune": None,
        "sample_count": 0,
        "strongest": [],
        "active_incidents": [],
        "active_bins": [],
        "recent_events": [],
        "strongest_incidents": [],
        "series": [],
        "recent_peak": None,
        "recent_peaks": [],
        "status": "warming",
        "message": "Building baseline",
        "label_prompt": "",
    }


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

        def _send_body(
            self,
            status: int,
            content_type: str,
            body: bytes,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: object, status: int = 200) -> None:
            self._send_body(
                status,
                "application/json",
                json.dumps(payload).encode("utf-8"),
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)

            if parsed.path == "/state":
                self._send_json(state.snapshot())
                return

            if parsed.path == "/mark":
                params = parse_qs(parsed.query)
                label = params.get("label", ["manual_marker"])[0]
                marker = mark_observation(label)
                self._send_json(marker)
                return

            if parsed.path == "/select-tune":
                params = parse_qs(parsed.query)
                tune = params.get("tune", [""])[0]
                result = select_tune(tune)
                status = 200 if result.get("ok") else 400
                self._send_json(result, status)
                return

            if parsed.path == "/analysis":
                result = load_analysis()
                status = 200 if result.get("ok") else 500
                self._send_json(result, status)
                return

            if parsed.path in {"/", "/index.html"}:
                self._send_body(200, "text/html; charset=utf-8", dashboard_html())
                return

            self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
