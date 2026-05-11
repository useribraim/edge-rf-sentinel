#!/usr/bin/env python3
"""Record SDR signal-strength incidents with timestamps."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use rtl_power to watch an RF frequency range, build a rolling "
            "baseline, and write timestamped signal-strength incidents."
        )
    )
    parser.add_argument(
        "--preset",
        choices=["base", "mobile"],
        default=None,
        help=(
            "Apply a preset before explicit CLI overrides: base=fixed-site "
            "example, mobile=mobile/uplink example."
        ),
    )
    parser.add_argument(
        "--range",
        default=None,
        help="rtl_power frequency range. Default: 390M:395M:25k",
    )
    parser.add_argument(
        "--interval",
        default=None,
        help="rtl_power integration interval. Default: 5s",
    )
    parser.add_argument(
        "--gain",
        default=None,
        help="RTL-SDR gain passed to rtl_power. Try 20, 30, 40, or auto.",
    )
    parser.add_argument(
        "--ppm",
        type=int,
        default=0,
        help="Frequency correction in ppm. Default: 0",
    )
    parser.add_argument(
        "--baseline-samples",
        type=int,
        default=60,
        help="Rolling samples per channel used for the baseline. Default: 60",
    )
    parser.add_argument(
        "--warmup-samples",
        type=int,
        default=12,
        help="Samples before alerting starts. Default: 12",
    )
    parser.add_argument(
        "--threshold-db",
        type=float,
        default=None,
        help="Alert when channel power is this many dB above baseline. Default: 8",
    )
    parser.add_argument(
        "--incident-min-power-db",
        type=float,
        default=None,
        help=(
            "Only create incidents when absolute power is at or above this dB. "
            "Default: -20"
        ),
    )
    parser.add_argument(
        "--cluster-khz",
        type=float,
        default=60.0,
        help="Group active dashboard incidents within this bandwidth. Default: 60",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="Show this many strongest bins each interval. Default: 8",
    )
    parser.add_argument(
        "--hold-samples",
        type=int,
        default=3,
        help="End an incident after this many below-threshold samples. Default: 3",
    )
    parser.add_argument(
        "--activity-log",
        default=None,
        help="CSV file for timestamped incidents. Default: logs/rf_activity.csv",
    )
    parser.add_argument(
        "--readings-log",
        default=None,
        help="CSV file for strongest readings each scan row. Default: logs/rf_readings.csv",
    )
    parser.add_argument(
        "--log-top",
        type=int,
        default=5,
        help="Write this many strongest readings per scan row. Default: 5",
    )
    parser.add_argument(
        "--absolute-strong-db",
        type=float,
        default=None,
        help=(
            "Dashboard marks absolute signal power at or above this dB as strong. "
            "Default: -10"
        ),
    )
    parser.add_argument(
        "--absolute-extreme-db",
        type=float,
        default=None,
        help=(
            "Dashboard marks absolute signal power at or above this dB as extreme. "
            "Default: -5"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print strongest-bin status lines; still writes CSV.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Serve a local visual dashboard at http://127.0.0.1:8765.",
    )
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard host. Default: 127.0.0.1",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8765,
        help="Dashboard port. Default: 8765",
    )
    return apply_preset_defaults(parser.parse_args())


def apply_preset_defaults(args: argparse.Namespace) -> argparse.Namespace:
    presets = {
        "base": {
            "range": "390M:395M:25k",
            "interval": "1s",
            "gain": "30",
            "threshold_db": 8.0,
            "incident_min_power_db": -20.0,
            "activity_log": "logs/rf_base_activity.csv",
            "readings_log": "logs/rf_base_readings.csv",
            "absolute_strong_db": -10.0,
            "absolute_extreme_db": -5.0,
        },
        "mobile": {
            "range": "380M:385M:25k",
            "interval": "1s",
            "gain": "30",
            "threshold_db": 6.0,
            "incident_min_power_db": -25.0,
            "activity_log": "logs/rf_mobile_activity.csv",
            "readings_log": "logs/rf_mobile_readings.csv",
            "absolute_strong_db": -18.0,
            "absolute_extreme_db": -12.0,
        },
    }
    defaults = {
        "range": "390M:395M:25k",
        "interval": "5s",
        "gain": "auto",
        "threshold_db": 8.0,
        "incident_min_power_db": -20.0,
        "activity_log": "logs/rf_activity.csv",
        "readings_log": "logs/rf_readings.csv",
        "absolute_strong_db": -10.0,
        "absolute_extreme_db": -5.0,
    }
    selected = presets.get(args.preset or "", defaults)

    for key, fallback in defaults.items():
        value = getattr(args, key)
        if value is None:
            setattr(args, key, selected.get(key, fallback))

    return args


def require_rtl_power() -> None:
    try:
        subprocess.run(["rtl_power", "-h"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        sys.exit(
            "rtl_power was not found. Install it with:\n"
            "  brew install librtlsdr\n"
            "Then plug in the NESDR and run this script again."
        )


def start_rtl_power(args: argparse.Namespace, output_path: Path) -> subprocess.Popen[str]:
    command = [
        "rtl_power",
        "-f",
        args.range,
        "-i",
        args.interval,
        "-g",
        str(args.gain),
        "-p",
        str(args.ppm),
        str(output_path),
    ]
    return subprocess.Popen(command, text=True)


def parse_power_row(row: list[str]) -> list[tuple[int, float]]:
    if len(row) < 7:
        return []

    start_hz = float(row[2])
    stop_hz = float(row[3])
    step_hz = float(row[4])
    powers = [float(value) for value in row[6:] if value]

    if not powers:
        return []

    bins: list[tuple[int, float]] = []
    for index, power_db in enumerate(powers):
        center_hz = start_hz + (index * step_hz) + (step_hz / 2)
        if center_hz <= stop_hz:
            bins.append((round(center_hz), power_db))
    return bins


def format_freq(hz: int) -> str:
    return f"{hz / 1_000_000:.6f} MHz"


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
    return b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RF Signal Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #090909;
      color: #f6f2e8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: #090909;
      transition: background 200ms linear;
    }
    body.normal { background: #0b1713; }
    body.elevated { background: #2b2208; }
    body.strong { background: #3a1108; }
    body.extreme {
      background: #7a0707;
      animation: pulse 900ms infinite alternate;
    }
    @keyframes pulse {
      from { background: #5f0606; }
      to { background: #b90808; }
    }
    main {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 18px;
      padding: 22px;
    }
    header, footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      font-size: clamp(14px, 2.2vw, 22px);
      color: rgba(255,255,255,.78);
    }
    .hero {
      display: grid;
      align-content: center;
      justify-items: center;
      text-align: center;
      gap: 14px;
    }
    .status {
      font-size: clamp(42px, 13vw, 150px);
      line-height: .92;
      font-weight: 850;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .delta {
      font-size: clamp(56px, 18vw, 220px);
      line-height: .9;
      font-weight: 900;
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }
    .freq {
      font-size: clamp(26px, 6vw, 76px);
      font-weight: 750;
      font-variant-numeric: tabular-nums;
    }
    .details {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      width: min(1100px, 100%);
    }
    .metric {
      border: 1px solid rgba(255,255,255,.18);
      background: rgba(0,0,0,.22);
      border-radius: 8px;
      padding: 14px;
      min-height: 96px;
    }
    .label {
      color: rgba(255,255,255,.58);
      font-size: clamp(13px, 1.9vw, 18px);
      margin-bottom: 6px;
    }
    .value {
      font-size: clamp(24px, 4vw, 48px);
      font-weight: 800;
      font-variant-numeric: tabular-nums;
      overflow-wrap: anywhere;
    }
    footer {
      font-variant-numeric: tabular-nums;
    }
    @media (max-width: 700px) {
      main { padding: 14px; gap: 12px; }
      header, footer { align-items: flex-start; flex-direction: column; }
      .details { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body class="normal">
  <main>
    <header>
      <div>RF monitor</div>
      <div id="updated">waiting</div>
    </header>
    <section class="hero">
      <div id="status" class="status">warming</div>
      <div id="delta" class="delta">+0.0 dB</div>
      <div id="freq" class="freq">---</div>
      <div class="details">
        <div class="metric">
          <div class="label">Power</div>
          <div id="power" class="value">---</div>
        </div>
        <div class="metric">
          <div class="label">Baseline</div>
          <div id="baseline" class="value">---</div>
        </div>
        <div class="metric">
          <div class="label">Active incidents</div>
          <div id="incidents" class="value">0</div>
        </div>
      </div>
    </section>
    <footer>
      <div id="range">range</div>
      <div id="samples">samples 0</div>
    </footer>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    function level(delta, threshold) {
      if (delta >= threshold + 10) return ["extreme", "extreme"];
      if (delta >= threshold + 5) return ["strong", "strong"];
      if (delta >= threshold) return ["elevated", "elevated"];
      return ["normal", "normal"];
    }
    async function refresh() {
      const response = await fetch("/state", { cache: "no-store" });
      const state = await response.json();
      const top = state.strongest && state.strongest.length ? state.strongest[0] : null;
      const active = state.active_incidents || [];
      const main = active.length ? active[0] : top;
      const delta = main && Number.isFinite(main.delta_db) ? main.delta_db : 0;
      const power = main && Number.isFinite(main.power_db) ? main.power_db : -999;
      const threshold = state.threshold_db || 8;
      const absoluteStrong = state.absolute_strong_db ?? -12;
      const absoluteExtreme = state.absolute_extreme_db ?? -8;
      let [className, label] = level(delta, threshold);
      if (power >= absoluteExtreme) [className, label] = ["extreme", "extreme"];
      else if (power >= absoluteStrong && className === "normal") [className, label] = ["strong", "strong"];

      document.body.className = className;
      $("status").textContent = state.sample_count < state.warmup_samples ? "warming" : label;
      $("delta").textContent = `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} dB`;
      $("freq").textContent = main ? `${main.frequency_mhz.toFixed(6)} MHz` : "---";
      $("power").textContent = main ? `${main.power_db.toFixed(1)} dB` : "---";
      $("baseline").textContent = main && Number.isFinite(main.baseline_db) ? `${main.baseline_db.toFixed(1)} dB` : "---";
      $("incidents").textContent = active.length;
      $("updated").textContent = state.updated_at || "waiting";
      $("range").textContent = state.range || "";
      $("samples").textContent = `samples ${state.sample_count || 0}`;
    }
    refresh().catch(() => {});
    setInterval(() => refresh().catch(() => {}), 1000);
  </script>
</body>
</html>
"""


def start_dashboard(state: DashboardState, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/state":
                body = json.dumps(state.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path in {"/", "/index.html"}:
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


def open_activity_log(path: Path) -> tuple[object, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    fp = path.open("a", newline="")
    writer = csv.DictWriter(
        fp,
        fieldnames=[
            "timestamp",
            "event",
            "frequency_hz",
            "frequency_mhz",
            "power_db",
            "baseline_db",
            "delta_db",
            "threshold_db",
            "incident_min_power_db",
            "incident_start",
            "incident_duration_seconds",
            "peak_power_db",
            "peak_delta_db",
        ],
    )
    if needs_header:
        writer.writeheader()
        fp.flush()
    return fp, writer


def open_readings_log(path: Path) -> tuple[object, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    fp = path.open("a", newline="")
    writer = csv.DictWriter(
        fp,
        fieldnames=[
            "timestamp",
            "sample",
            "rank",
            "frequency_hz",
            "frequency_mhz",
            "power_db",
            "baseline_db",
            "delta_db",
            "threshold_db",
            "incident_min_power_db",
            "is_incident",
        ],
    )
    if needs_header:
        writer.writeheader()
        fp.flush()
    return fp, writer


def write_activity(
    writer: csv.DictWriter,
    *,
    event: str,
    timestamp: dt.datetime,
    freq_hz: int,
    power_db: float,
    baseline_db: float,
    delta_db: float,
    threshold_db: float,
    incident_min_power_db: float,
    incident_start: dt.datetime,
    peak_power_db: float,
    peak_delta_db: float,
) -> None:
    writer.writerow(
        {
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "event": event,
            "frequency_hz": freq_hz,
            "frequency_mhz": f"{freq_hz / 1_000_000:.6f}",
            "power_db": f"{power_db:.2f}",
            "baseline_db": f"{baseline_db:.2f}",
            "delta_db": f"{delta_db:.2f}",
            "threshold_db": f"{threshold_db:.2f}",
            "incident_min_power_db": f"{incident_min_power_db:.2f}",
            "incident_start": incident_start.isoformat(timespec="seconds"),
            "incident_duration_seconds": f"{(timestamp - incident_start).total_seconds():.0f}",
            "peak_power_db": f"{peak_power_db:.2f}",
            "peak_delta_db": f"{peak_delta_db:.2f}",
        }
    )


def write_reading(
    writer: csv.DictWriter,
    *,
    timestamp: dt.datetime,
    sample_count: int,
    rank: int,
    reading: dict[str, float | int],
    threshold_db: float,
    incident_min_power_db: float,
    is_incident: bool,
) -> None:
    writer.writerow(
        {
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "sample": sample_count,
            "rank": rank,
            "frequency_hz": int(reading["frequency_hz"]),
            "frequency_mhz": f"{float(reading['frequency_mhz']):.6f}",
            "power_db": f"{float(reading['power_db']):.2f}",
            "baseline_db": f"{float(reading['baseline_db']):.2f}",
            "delta_db": f"{float(reading['delta_db']):.2f}",
            "threshold_db": f"{threshold_db:.2f}",
            "incident_min_power_db": f"{incident_min_power_db:.2f}",
            "is_incident": int(is_incident),
        }
    )


def reading_payload(
    freq_hz: int,
    power_db: float,
    baseline_db: float,
    delta_db: float,
) -> dict[str, float | int]:
    return {
        "frequency_hz": freq_hz,
        "frequency_mhz": freq_hz / 1_000_000,
        "power_db": power_db,
        "baseline_db": baseline_db,
        "delta_db": delta_db,
    }


def cluster_readings(
    readings: list[dict[str, object]],
    cluster_hz: float,
) -> list[dict[str, object]]:
    if not readings:
        return []

    sorted_readings = sorted(readings, key=lambda item: int(item["frequency_hz"]))
    clusters: list[list[dict[str, object]]] = []

    for reading in sorted_readings:
        if not clusters:
            clusters.append([reading])
            continue

        previous = clusters[-1][-1]
        if int(reading["frequency_hz"]) - int(previous["frequency_hz"]) <= cluster_hz:
            clusters[-1].append(reading)
        else:
            clusters.append([reading])

    grouped: list[dict[str, object]] = []
    for cluster in clusters:
        strongest = max(cluster, key=lambda item: float(item["power_db"]))
        peak_delta = max(float(item["delta_db"]) for item in cluster)
        peak_power = max(float(item["power_db"]) for item in cluster)
        started_values = [
            str(item["started_at"]) for item in cluster if "started_at" in item
        ]
        grouped.append(
            {
                **strongest,
                "bin_count": len(cluster),
                "cluster_low_mhz": int(cluster[0]["frequency_hz"]) / 1_000_000,
                "cluster_high_mhz": int(cluster[-1]["frequency_hz"]) / 1_000_000,
                "peak_delta_db": peak_delta,
                "peak_power_db": peak_power,
                "started_at": min(started_values) if started_values else None,
            }
        )

    grouped.sort(key=lambda item: float(item["power_db"]), reverse=True)
    return grouped


def monitor(args: argparse.Namespace) -> int:
    require_rtl_power()

    baselines: dict[int, deque[float]] = defaultdict(
        lambda: deque(maxlen=args.baseline_samples)
    )
    incidents: dict[int, dict[str, object]] = {}
    sample_count = 0
    activity_fp, activity_writer = open_activity_log(Path(args.activity_log))
    readings_fp, readings_writer = open_readings_log(Path(args.readings_log))
    recent_events: deque[dict[str, object]] = deque(maxlen=20)
    dashboard_state = DashboardState()
    dashboard_state.update(
        threshold_db=args.threshold_db,
        incident_min_power_db=args.incident_min_power_db,
        warmup_samples=args.warmup_samples,
        range=args.range,
        absolute_strong_db=args.absolute_strong_db,
        absolute_extreme_db=args.absolute_extreme_db,
        cluster_khz=args.cluster_khz,
    )

    dashboard_server = None
    if args.dashboard:
        dashboard_server = start_dashboard(
            dashboard_state,
            args.dashboard_host,
            args.dashboard_port,
        )
        print(f"Dashboard: http://{args.dashboard_host}:{args.dashboard_port}")

    with tempfile.NamedTemporaryFile(prefix="rf_power_", suffix=".csv", delete=False) as fp:
        output_path = Path(fp.name)

    print(f"Monitoring {args.range}; writing temporary rtl_power CSV to {output_path}")
    print(f"Recording incidents to {Path(args.activity_log).resolve()}")
    print(f"Recording strongest readings to {Path(args.readings_log).resolve()}")
    print("Press Ctrl-C to stop.")

    process = start_rtl_power(args, output_path)
    last_size = 0

    try:
        while process.poll() is None:
            time.sleep(1)
            if not output_path.exists():
                continue

            current_size = output_path.stat().st_size
            if current_size == last_size:
                continue

            with output_path.open("r", newline="") as fp:
                fp.seek(last_size)
                rows = list(csv.reader(fp))
                last_size = fp.tell()

            for row in rows:
                bins = parse_power_row(row)
                if not bins:
                    continue

                sample_count += 1
                now = dt.datetime.now().astimezone()
                enriched: list[dict[str, float | int]] = []

                for freq_hz, power_db in bins:
                    history = baselines[freq_hz]
                    baseline = statistics.median(history) if history else power_db
                    delta = power_db - baseline
                    enriched.append(reading_payload(freq_hz, power_db, baseline, delta))
                    is_warm = sample_count > args.warmup_samples
                    is_high = (
                        is_warm
                        and delta >= args.threshold_db
                        and power_db >= args.incident_min_power_db
                    )
                    incident = incidents.get(freq_hz)

                    if is_high and incident is None:
                        incidents[freq_hz] = {
                            "start": now,
                            "peak_power": power_db,
                            "peak_delta": delta,
                            "below_count": 0,
                        }
                        write_activity(
                            activity_writer,
                            event="start",
                            timestamp=now,
                            freq_hz=freq_hz,
                            power_db=power_db,
                            baseline_db=baseline,
                            delta_db=delta,
                            threshold_db=args.threshold_db,
                            incident_min_power_db=args.incident_min_power_db,
                            incident_start=now,
                            peak_power_db=power_db,
                            peak_delta_db=delta,
                        )
                        activity_fp.flush()
                        recent_events.appendleft(
                            {
                                "event": "start",
                                "timestamp": now.isoformat(timespec="seconds"),
                                **reading_payload(freq_hz, power_db, baseline, delta),
                            }
                        )
                        print(
                            f"INCIDENT START {now.isoformat(timespec='seconds')} "
                            f"{format_freq(freq_hz)} {power_db:.1f} dB "
                            f"baseline {baseline:.1f} dB +{delta:.1f} dB"
                        )
                    elif is_high and incident is not None:
                        incident["below_count"] = 0
                        incident["peak_power"] = max(float(incident["peak_power"]), power_db)
                        incident["peak_delta"] = max(float(incident["peak_delta"]), delta)
                        write_activity(
                            activity_writer,
                            event="active",
                            timestamp=now,
                            freq_hz=freq_hz,
                            power_db=power_db,
                            baseline_db=baseline,
                            delta_db=delta,
                            threshold_db=args.threshold_db,
                            incident_min_power_db=args.incident_min_power_db,
                            incident_start=incident["start"],
                            peak_power_db=float(incident["peak_power"]),
                            peak_delta_db=float(incident["peak_delta"]),
                        )
                        activity_fp.flush()
                    elif incident is not None:
                        incident["below_count"] = int(incident["below_count"]) + 1
                        if int(incident["below_count"]) >= args.hold_samples:
                            write_activity(
                                activity_writer,
                                event="end",
                                timestamp=now,
                                freq_hz=freq_hz,
                                power_db=power_db,
                                baseline_db=baseline,
                                delta_db=delta,
                                threshold_db=args.threshold_db,
                                incident_min_power_db=args.incident_min_power_db,
                                incident_start=incident["start"],
                                peak_power_db=float(incident["peak_power"]),
                                peak_delta_db=float(incident["peak_delta"]),
                            )
                            activity_fp.flush()
                            recent_events.appendleft(
                                {
                                    "event": "end",
                                    "timestamp": now.isoformat(timespec="seconds"),
                                    "duration_seconds": (
                                        now - incident["start"]
                                    ).total_seconds(),
                                    **reading_payload(freq_hz, power_db, baseline, delta),
                                    "peak_delta_db": float(incident["peak_delta"]),
                                }
                            )
                            print(
                                f"INCIDENT END {now.isoformat(timespec='seconds')} "
                                f"{format_freq(freq_hz)} duration "
                                f"{(now - incident['start']).total_seconds():.0f}s "
                                f"peak +{float(incident['peak_delta']):.1f} dB"
                            )
                            del incidents[freq_hz]

                    history.append(power_db)

                strongest = sorted(
                    enriched,
                    key=lambda item: float(item["power_db"]),
                    reverse=True,
                )[: args.top]
                for rank, reading in enumerate(strongest[: args.log_top], start=1):
                    write_reading(
                        readings_writer,
                        timestamp=now,
                        sample_count=sample_count,
                        rank=rank,
                        reading=reading,
                        threshold_db=args.threshold_db,
                        incident_min_power_db=args.incident_min_power_db,
                        is_incident=int(reading["frequency_hz"]) in incidents,
                    )
                readings_fp.flush()
                active_incidents = []
                for freq_hz, incident in incidents.items():
                    latest = next(
                        (item for item in enriched if int(item["frequency_hz"]) == freq_hz),
                        None,
                    )
                    if latest is None:
                        continue
                    active_incidents.append(
                        {
                            **latest,
                            "started_at": incident["start"].isoformat(timespec="seconds"),
                            "duration_seconds": (
                                now - incident["start"]
                            ).total_seconds(),
                            "peak_power_db": float(incident["peak_power"]),
                            "peak_delta_db": float(incident["peak_delta"]),
                        }
                    )
                active_incidents.sort(
                    key=lambda item: float(item["delta_db"]),
                    reverse=True,
                )
                active_clusters = cluster_readings(
                    active_incidents,
                    args.cluster_khz * 1_000,
                )
                dashboard_state.update(
                    updated_at=now.isoformat(timespec="seconds"),
                    sample_count=sample_count,
                    strongest=strongest,
                    active_incidents=active_clusters,
                    active_bins=active_incidents,
                    recent_events=list(recent_events),
                    status="active" if active_incidents else "normal",
                    message="Active incident" if active_incidents else "Monitoring",
                )

                if not args.quiet:
                    timestamp = now.strftime("%H:%M:%S")
                    top_text = ", ".join(
                        f"{item['frequency_mhz']:.6f} MHz {item['power_db']:.1f} dB"
                        for item in strongest
                    )
                    print(f"[{timestamp}] strongest: {top_text}")

    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        final_time = dt.datetime.now().astimezone()
        for freq_hz, incident in list(incidents.items()):
            write_activity(
                activity_writer,
                event="end",
                timestamp=final_time,
                freq_hz=freq_hz,
                power_db=float("nan"),
                baseline_db=float("nan"),
                delta_db=float("nan"),
                threshold_db=args.threshold_db,
                incident_min_power_db=args.incident_min_power_db,
                incident_start=incident["start"],
                peak_power_db=float(incident["peak_power"]),
                peak_delta_db=float(incident["peak_delta"]),
            )
        activity_fp.flush()
        activity_fp.close()
        readings_fp.flush()
        readings_fp.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass
        if dashboard_server is not None:
            dashboard_server.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(monitor(parse_args()))
