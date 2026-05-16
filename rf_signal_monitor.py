#!/usr/bin/env python3
"""Record SDR signal-strength incidents with timestamps."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


TUNE_PROFILES: dict[str, dict[str, object]] = {
    "balanced": {
        "label": "Balanced",
        "gain": "20",
        "threshold_db": 12.0,
        "incident_min_power_db": -24.0,
        "min_incident_samples": 4,
        "hold_samples": 5,
        "cluster_khz": 80.0,
        "absolute_strong_db": -18.0,
        "absolute_extreme_db": -12.0,
    },
    "strict": {
        "label": "Strict",
        "gain": "8.7",
        "threshold_db": 26.0,
        "incident_min_power_db": -10.0,
        "min_incident_samples": 12,
        "hold_samples": 12,
        "cluster_khz": 100.0,
        "absolute_strong_db": -8.0,
        "absolute_extreme_db": -4.0,
    },
    "sensitive": {
        "label": "Sensitive",
        "gain": "30",
        "threshold_db": 8.0,
        "incident_min_power_db": -30.0,
        "min_incident_samples": 2,
        "hold_samples": 3,
        "cluster_khz": 60.0,
        "absolute_strong_db": -22.0,
        "absolute_extreme_db": -16.0,
    },
}

TUNE_VALUE_KEYS = [
    "gain",
    "threshold_db",
    "incident_min_power_db",
    "min_incident_samples",
    "hold_samples",
    "cluster_khz",
    "absolute_strong_db",
    "absolute_extreme_db",
]


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
        "--tune",
        choices=sorted(TUNE_PROFILES),
        default=None,
        help="Sensitivity tune profile. Can also be changed from the dashboard.",
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
        default=None,
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
        default=None,
        help="End an incident after this many below-threshold samples. Default: 3",
    )
    parser.add_argument(
        "--min-incident-samples",
        type=int,
        default=None,
        help="Confirm a cluster incident after this many consecutive samples. Default: 3",
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
        "--observations-log",
        default=None,
        help="CSV file for manual field markers. Default: logs/rf_observations.csv",
    )
    parser.add_argument(
        "--session-dir",
        default=None,
        help=(
            "Directory for this run's CSV files. Default: "
            "logs/sessions/<timestamp>_<preset-or-rf>."
        ),
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
        "--demo",
        action="store_true",
        help="Run the dashboard with simulated RF data; no SDR hardware required.",
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
    args = parser.parse_args()
    explicit_logs = {
        "activity_log": args.activity_log is not None,
        "readings_log": args.readings_log is not None,
        "observations_log": args.observations_log is not None,
    }
    args = apply_preset_defaults(args)
    return apply_session_paths(args, explicit_logs)


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
            "observations_log": "logs/rf_base_observations.csv",
            "absolute_strong_db": -10.0,
            "absolute_extreme_db": -5.0,
            "cluster_khz": 60.0,
            "hold_samples": 3,
            "min_incident_samples": 3,
        },
        "mobile": {
            "range": "380M:385M:25k",
            "interval": "1s",
            "gain": "8.7",
            "threshold_db": 26.0,
            "incident_min_power_db": -10.0,
            "activity_log": "logs/rf_mobile_activity.csv",
            "readings_log": "logs/rf_mobile_readings.csv",
            "observations_log": "logs/rf_mobile_observations.csv",
            "absolute_strong_db": -8.0,
            "absolute_extreme_db": -4.0,
            "cluster_khz": 100.0,
            "hold_samples": 12,
            "min_incident_samples": 12,
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
        "observations_log": "logs/rf_observations.csv",
        "absolute_strong_db": -10.0,
        "absolute_extreme_db": -5.0,
        "cluster_khz": 60.0,
        "hold_samples": 3,
        "min_incident_samples": 3,
    }
    selected = dict(presets.get(args.preset or "", defaults))
    if args.tune:
        selected.update(tune_values(args.tune))

    for key, fallback in defaults.items():
        value = getattr(args, key)
        if value is None:
            setattr(args, key, selected.get(key, fallback))

    args.tune = args.tune or ("strict" if args.preset == "mobile" else "custom")
    return args


def apply_session_paths(
    args: argparse.Namespace,
    explicit_logs: dict[str, bool],
) -> argparse.Namespace:
    session_id = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    mode = args.preset or "rf"
    args.session_id = f"{session_id}_{mode}"
    session_dir = Path(args.session_dir) if args.session_dir else Path("logs/sessions") / args.session_id
    args.session_dir = str(session_dir)

    if not any(explicit_logs.values()):
        args.activity_log = str(session_dir / "activity.csv")
        args.readings_log = str(session_dir / "readings.csv")
        args.observations_log = str(session_dir / "observations.csv")
        return args

    if not explicit_logs["activity_log"]:
        args.activity_log = str(session_dir / "activity.csv")
    if not explicit_logs["readings_log"]:
        args.readings_log = str(session_dir / "readings.csv")
    if not explicit_logs["observations_log"]:
        args.observations_log = str(session_dir / "observations.csv")
    return args


def tune_values(name: str) -> dict[str, object]:
    profile = TUNE_PROFILES.get(name, {})
    return {key: profile[key] for key in TUNE_VALUE_KEYS if key in profile}


def apply_tune(args: argparse.Namespace, tune_name: str) -> None:
    for key, value in tune_values(tune_name).items():
        setattr(args, key, value)
    args.tune = tune_name


def tuning_payload(args: argparse.Namespace) -> dict[str, object]:
    tune_name = getattr(args, "tune", "custom")
    profile = TUNE_PROFILES.get(tune_name, {})
    return {
        "tune": tune_name,
        "tune_label": profile.get("label", tune_name.replace("_", " ")),
        "profiles": [
            {"name": name, "label": str(profile["label"])}
            for name, profile in TUNE_PROFILES.items()
        ],
        "range": args.range,
        "interval": args.interval,
        "gain": args.gain,
        "threshold_db": args.threshold_db,
        "incident_min_power_db": args.incident_min_power_db,
        "hold_samples": args.hold_samples,
        "min_incident_samples": args.min_incident_samples,
        "cluster_khz": args.cluster_khz,
        "baseline_samples": args.baseline_samples,
        "warmup_samples": args.warmup_samples,
        "session_id": getattr(args, "session_id", ""),
        "session_dir": getattr(args, "session_dir", ""),
        "activity_log": args.activity_log,
        "readings_log": args.readings_log,
        "observations_log": args.observations_log,
    }


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


def parse_frequency_value(value: str) -> float:
    text = value.strip().lower()
    multipliers = {
        "g": 1_000_000_000,
        "m": 1_000_000,
        "k": 1_000,
    }
    suffix = text[-1]
    if suffix in multipliers:
        return float(text[:-1]) * multipliers[suffix]
    return float(text)


def parse_range_spec(range_spec: str) -> tuple[int, int, int]:
    start, stop, step = range_spec.split(":", 2)
    return (
        round(parse_frequency_value(start)),
        round(parse_frequency_value(stop)),
        round(parse_frequency_value(step)),
    )


def demo_power_row(args: argparse.Namespace, sample_count: int) -> list[str]:
    start_hz, stop_hz, step_hz = parse_range_spec(args.range)
    bin_count = max(1, int((stop_hz - start_hz) / step_hz))
    now = dt.datetime.now().astimezone()
    sweep = math.sin(sample_count / 9) * 2.0
    burst_center = start_hz + int((0.25 + 0.5 * ((sample_count // 25) % 2)) * (stop_hz - start_hz))
    burst_active = sample_count % 38 in {8, 9, 10, 11, 12, 13}

    powers: list[str] = []
    for index in range(bin_count):
        center_hz = start_hz + index * step_hz + step_hz / 2
        noise = random.uniform(-2.2, 2.2)
        power = -36.0 + sweep + noise
        if burst_active:
            distance_bins = abs(center_hz - burst_center) / max(step_hz, 1)
            power += max(0.0, 20.0 - distance_bins * 5.5)
        powers.append(f"{power:.2f}")

    return [
        now.strftime("%Y-%m-%d"),
        now.strftime("%H:%M:%S"),
        str(start_hz),
        str(stop_hz),
        str(step_hz),
        str(bin_count),
        *powers,
    ]


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
    body.normal { background: #071511; }
    body.elevated { background: #242006; }
    body.strong { background: #331208; }
    body.extreme {
      background: #780606;
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
      gap: 14px;
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
    .mode-switch {
      display: inline-grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      padding: 5px;
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(0,0,0,.22);
    }
    .mode-tab {
      appearance: none;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: rgba(255,255,255,.72);
      min-height: 38px;
      padding: 0 14px;
      font: inherit;
      font-weight: 820;
    }
    .mode-tab.active {
      background: rgba(255,244,214,.16);
      color: #fff4d6;
    }
    .mode-panel.hidden {
      display: none;
    }
    .hero {
      display: grid;
      align-content: start;
      gap: 16px;
      width: min(1500px, 100%);
      margin: 0 auto;
    }
    .summary {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 14px;
      align-items: stretch;
    }
    .primary {
      text-align: center;
      display: grid;
      align-content: center;
      gap: 8px;
      min-height: 320px;
    }
    .status {
      font-size: clamp(34px, 8vw, 104px);
      line-height: .96;
      font-weight: 850;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .power-main {
      font-size: clamp(64px, 15vw, 180px);
      line-height: .9;
      font-weight: 900;
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }
    .subline {
      display: flex;
      justify-content: center;
      flex-wrap: wrap;
      gap: 18px;
      color: rgba(255,255,255,.78);
      font-size: clamp(20px, 3vw, 36px);
      font-weight: 720;
      font-variant-numeric: tabular-nums;
    }
    .freq {
      font-size: clamp(24px, 4.8vw, 58px);
      font-weight: 750;
      font-variant-numeric: tabular-nums;
    }
    .side {
      display: grid;
      grid-template-rows: 1fr 1fr;
      gap: 12px;
    }
    .details {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
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
    .peak .value {
      font-size: clamp(28px, 4vw, 56px);
    }
    .recent-peaks {
      display: grid;
      gap: 6px;
      margin-top: 6px;
    }
    .peak-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 6px;
      align-items: start;
      border: 1px solid rgba(255,255,255,.10);
      border-radius: 8px;
      background: rgba(0,0,0,.18);
      padding: 8px 10px;
      font-variant-numeric: tabular-nums;
    }
    .peak-row .peak-time {
      color: rgba(255,255,255,.72);
      font-size: clamp(12px, 1.3vw, 16px);
      font-weight: 850;
      line-height: 1.1;
    }
    .peak-row .peak-db {
      color: #fff4d6;
      grid-column: 1 / -1;
      font-size: clamp(24px, 3.2vw, 38px);
      font-weight: 950;
      line-height: 1;
      overflow-wrap: normal;
      white-space: nowrap;
    }
    .peak-row .peak-age {
      color: #fff4d6;
      font-size: clamp(17px, 2.2vw, 27px);
      font-weight: 950;
      text-align: right;
      line-height: .95;
      white-space: nowrap;
    }
    .peak-row .peak-detail {
      grid-column: 1 / -1;
      color: rgba(255,255,255,.58);
      font-size: clamp(11px, 1.2vw, 15px);
      font-weight: 750;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .muted-line {
      color: rgba(255,255,255,.62);
      font-size: clamp(14px, 1.8vw, 20px);
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }
    .chart-wrap {
      border: 1px solid rgba(255,255,255,.18);
      background: rgba(0,0,0,.22);
      border-radius: 8px;
      padding: 16px;
    }
    .chart-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: rgba(255,255,255,.68);
      font-size: clamp(13px, 1.8vw, 18px);
      margin-bottom: 8px;
    }
    canvas {
      display: block;
      width: 100%;
      height: clamp(460px, 52vh, 700px);
    }
    .time-axis {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin: 8px 4px 0 56px;
      color: rgba(255,255,255,.78);
      font-size: clamp(12px, 1.6vw, 18px);
      font-weight: 750;
      font-variant-numeric: tabular-nums;
    }
    .time-axis span {
      text-align: center;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .marker-row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }
    .mark-button {
      appearance: none;
      border: 1px solid rgba(255,255,255,.22);
      border-radius: 8px;
      background: rgba(255,244,214,.14);
      color: #f6f2e8;
      min-height: 82px;
      font-size: clamp(24px, 3.4vw, 42px);
      font-weight: 900;
      font-family: inherit;
      text-transform: uppercase;
    }
    .mark-button:active {
      transform: translateY(1px);
      background: rgba(255,255,255,.18);
    }
    .marker-status {
      color: rgba(255,255,255,.68);
      font-size: clamp(14px, 1.8vw, 20px);
      font-weight: 650;
      min-height: 24px;
    }
    .label-prompt {
      border: 1px solid rgba(255,255,255,.16);
      border-radius: 8px;
      background: rgba(0,0,0,.2);
      color: rgba(255,255,255,.72);
      font-size: clamp(16px, 2vw, 24px);
      font-weight: 820;
      padding: 14px 16px;
      min-height: 58px;
    }
    .label-prompt.active {
      border-color: rgba(88, 214, 141, .55);
      background: rgba(25, 111, 61, .24);
      color: #d8ffe6;
    }
    .label-prompt.warning {
      border-color: rgba(255, 197, 78, .65);
      background: rgba(125, 82, 8, .32);
      color: #ffe4a8;
    }
    .info-grid {
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 12px;
    }
    .info-panel {
      border: 1px solid rgba(255,255,255,.18);
      background: rgba(0,0,0,.22);
      border-radius: 8px;
      padding: 14px;
      min-height: 150px;
    }
    .analysis-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .analysis-wide {
      grid-column: 1 / -1;
    }
    .analysis-list {
      display: grid;
      gap: 8px;
      color: rgba(255,255,255,.82);
      font-size: clamp(13px, 1.5vw, 18px);
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }
    .analysis-row {
      display: grid;
      grid-template-columns: 92px 1fr 90px 90px 92px;
      gap: 10px;
      align-items: baseline;
      border-bottom: 1px solid rgba(255,255,255,.08);
      padding-bottom: 6px;
    }
    .analysis-row.wide {
      grid-template-columns: 120px 1fr 96px 96px 96px 96px;
    }
    .quality-card {
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 8px;
      padding: 16px;
      background: rgba(0,0,0,.22);
    }
    .quality-card.green {
      border-color: rgba(88, 214, 141, .55);
      background: rgba(25, 111, 61, .22);
    }
    .quality-card.amber {
      border-color: rgba(255, 197, 78, .65);
      background: rgba(125, 82, 8, .30);
    }
    .quality-card.red {
      border-color: rgba(255, 95, 95, .72);
      background: rgba(138, 20, 20, .32);
    }
    .quality-title {
      font-size: clamp(20px, 2.8vw, 34px);
      font-weight: 900;
      margin-bottom: 6px;
    }
    .pattern-summary {
      display: grid;
      grid-template-columns: minmax(0, 1fr) repeat(4, minmax(88px, 130px));
      gap: 10px;
      align-items: stretch;
    }
    .pattern-message {
      border: 1px solid rgba(255,255,255,.14);
      border-radius: 8px;
      background: rgba(0,0,0,.20);
      padding: 14px;
      font-size: clamp(17px, 2.1vw, 26px);
      font-weight: 850;
      line-height: 1.25;
    }
    .pattern-count {
      border: 1px solid rgba(255,255,255,.14);
      border-radius: 8px;
      background: rgba(255,255,255,.06);
      padding: 12px;
      text-align: center;
    }
    .pattern-count strong {
      display: block;
      font-size: clamp(24px, 3.2vw, 42px);
      line-height: 1;
    }
    .pattern-count span {
      color: rgba(255,255,255,.62);
      font-size: clamp(12px, 1.4vw, 16px);
      font-weight: 800;
      text-transform: uppercase;
    }
    .pattern-row {
      display: grid;
      grid-template-columns: 132px 1fr 90px 86px 92px 108px 1.4fr;
      gap: 10px;
      align-items: baseline;
      border-bottom: 1px solid rgba(255,255,255,.08);
      padding-bottom: 8px;
    }
    .pattern-chip {
      display: inline-block;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 900;
      text-transform: uppercase;
      background: rgba(255,255,255,.1);
      color: rgba(255,255,255,.82);
    }
    .pattern-chip.high { background: rgba(255, 95, 95, .28); color: #ffd1d1; }
    .pattern-chip.medium { background: rgba(255, 197, 78, .25); color: #ffe4a8; }
    .pattern-chip.low { background: rgba(95, 180, 255, .22); color: #cfeaff; }
    .pattern-chip.noise { background: rgba(255,255,255,.08); color: rgba(255,255,255,.62); }
    .info-title {
      color: rgba(255,255,255,.68);
      font-size: clamp(14px, 1.8vw, 20px);
      font-weight: 800;
      margin-bottom: 10px;
    }
    .tune-list, .incident-list {
      display: grid;
      gap: 7px;
      font-size: clamp(13px, 1.5vw, 18px);
      font-weight: 650;
      color: rgba(255,255,255,.8);
      font-variant-numeric: tabular-nums;
    }
    .tune-control {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 120px;
      gap: 8px;
      margin-bottom: 12px;
    }
    .tune-control select,
    .tune-control button {
      min-height: 54px;
      border: 1px solid rgba(255,255,255,.22);
      border-radius: 8px;
      background: rgba(255,255,255,.08);
      color: #f6f2e8;
      font: inherit;
      font-size: clamp(16px, 2vw, 22px);
      font-weight: 850;
      padding: 0 12px;
    }
    .tune-control button:active {
      transform: translateY(1px);
      background: rgba(255,255,255,.18);
    }
    .tune-row, .incident-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      padding-bottom: 5px;
    }
    .incident-row {
      display: grid;
      grid-template-columns: 78px 1fr 90px 90px;
      align-items: baseline;
    }
    footer {
      font-variant-numeric: tabular-nums;
    }
    @media (max-width: 700px) {
      main { padding: 14px; gap: 12px; }
      header, footer { align-items: flex-start; flex-direction: column; }
      .summary { grid-template-columns: 1fr; }
      .primary { min-height: 220px; }
      .details { grid-template-columns: 1fr; }
      .marker-row { grid-template-columns: 1fr; }
      .info-grid { grid-template-columns: 1fr; }
      .analysis-grid { grid-template-columns: 1fr; }
      .analysis-row { grid-template-columns: 72px 1fr 70px 70px 72px; }
      .pattern-summary { grid-template-columns: 1fr 1fr; }
      .pattern-message { grid-column: 1 / -1; }
      .pattern-row { grid-template-columns: 92px 1fr 70px 70px; }
      .pattern-row span:nth-child(5),
      .pattern-row span:nth-child(6),
      .pattern-row span:nth-child(7) { display: none; }
      .incident-row { grid-template-columns: 70px 1fr 78px 78px; }
    }
  </style>
</head>
<body class="normal">
  <main>
    <header>
      <div>RF monitor</div>
      <div class="mode-switch" role="tablist" aria-label="Dashboard mode">
        <button id="detector-tab" class="mode-tab active" type="button">Signal detector</button>
        <button id="analysis-tab" class="mode-tab" type="button">Drive analysis</button>
      </div>
      <div id="updated">waiting</div>
    </header>
    <section id="detector-mode" class="hero mode-panel">
      <div class="summary">
        <div class="primary">
          <div id="status" class="status">warming</div>
          <div id="power-main" class="power-main">---</div>
          <div class="subline">
            <span id="delta">+0.0 dB above baseline</span>
            <span id="freq">---</span>
          </div>
        </div>
        <div class="side">
          <div class="metric peak">
            <div class="label">Last 5 peaks</div>
            <div id="recent-peaks" class="recent-peaks">
              <div class="muted-line">no peaks yet</div>
            </div>
          </div>
          <div class="metric">
            <div class="label">Mode</div>
            <div id="mode" class="value">mobile</div>
            <div id="mode-meta" class="muted-line">380-385 MHz</div>
          </div>
        </div>
      </div>
      <div class="details">
        <div class="metric">
          <div class="label">Baseline</div>
          <div id="baseline" class="value">---</div>
        </div>
        <div class="metric">
          <div class="label">Delta</div>
          <div id="delta-card" class="value">---</div>
        </div>
        <div class="metric">
          <div class="label">Active clusters</div>
          <div id="incidents" class="value">0</div>
        </div>
      </div>
      <div class="chart-wrap">
        <div class="chart-head">
          <div>Signal over time</div>
          <div id="chart-meta">power / baseline</div>
        </div>
        <canvas id="chart" width="1600" height="700"></canvas>
        <div id="time-axis" class="time-axis">
          <span>--:--:--</span>
          <span>--:--:--</span>
          <span>--:--:--</span>
          <span>--:--:--</span>
          <span>--:--:--</span>
        </div>
      </div>
      <div class="marker-row">
        <button class="mark-button" data-label="vehicle_in_sight">Vehicle in sight</button>
      </div>
      <div id="label-prompt" class="label-prompt">No vehicle label active</div>
      <div id="marker-status" class="marker-status">No field marker yet</div>
      <div class="info-grid">
        <div class="info-panel">
          <div class="info-title">Tune profile</div>
          <div class="tune-control">
            <select id="tune-select" aria-label="Tune profile"></select>
            <button id="apply-tune" type="button">Set</button>
          </div>
          <div id="tuning" class="tune-list"></div>
        </div>
        <div class="info-panel">
          <div class="info-title">Last 5 strongest incidents</div>
          <div id="strongest-incidents" class="incident-list"></div>
        </div>
      </div>
    </section>
    <section id="analysis-mode" class="hero mode-panel hidden">
      <div class="analysis-grid">
        <div class="metric">
          <div class="label">Session length</div>
          <div id="analysis-duration" class="value">---</div>
        </div>
        <div class="metric">
          <div class="label">Burst events</div>
          <div id="analysis-events" class="value">---</div>
        </div>
        <div class="metric">
          <div class="label">Vehicle labels</div>
          <div id="analysis-labels" class="value">---</div>
        </div>
        <div class="metric">
          <div class="label">Strict confirmed</div>
          <div id="analysis-strict" class="value">---</div>
        </div>
        <div class="info-panel analysis-wide">
          <div class="info-title">Session health</div>
          <div id="analysis-health" class="quality-card"></div>
        </div>
        <div class="info-panel analysis-wide">
          <div class="info-title">Burst pattern classifier</div>
          <div id="analysis-pattern-summary"></div>
          <div id="analysis-pattern-events" class="analysis-list" style="margin-top:12px"></div>
        </div>
        <div class="info-panel analysis-wide">
          <div class="info-title">What happened in this session</div>
          <div id="analysis-pattern-timeline" class="analysis-list"></div>
        </div>
        <div class="info-panel analysis-wide">
          <div class="info-title">Labelled intervals</div>
          <div id="analysis-intervals" class="analysis-list"></div>
        </div>
        <div class="info-panel analysis-wide">
          <div class="info-title">Strongest burst events</div>
          <div id="analysis-top-events" class="analysis-list"></div>
        </div>
        <div class="info-panel analysis-wide">
          <div class="info-title">Similar windows</div>
          <div id="analysis-similar" class="analysis-list"></div>
        </div>
        <div class="info-panel analysis-wide">
          <div class="info-title">Minute summary</div>
          <div id="analysis-minutes" class="analysis-list"></div>
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
    const chart = $("chart");
    const ctx = chart.getContext("2d");
    let tuneSelectionDirty = false;
    let lastActiveTune = "";
    let currentMode = "detector";
    let lastAnalysisAt = 0;
    function level(delta, threshold) {
      if (delta >= threshold + 10) return ["extreme", "strong"];
      if (delta >= threshold + 5) return ["strong", "burst"];
      if (delta >= threshold) return ["elevated", "watch"];
      return ["normal", "quiet"];
    }
    function smooth(series, key) {
      return series.map((point, index) => {
        const start = Math.max(0, index - 2);
        const window = series.slice(start, index + 1)
          .map((item) => item[key])
          .filter((value) => Number.isFinite(value));
        return window.reduce((sum, value) => sum + value, 0) / window.length;
      });
    }
    function drawChart(series) {
      const width = chart.width;
      const height = chart.height;
      const timeAxis = $("time-axis");
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "rgba(0,0,0,.18)";
      ctx.fillRect(0, 0, width, height);

      if (!series || series.length < 2) {
        timeAxis.innerHTML = Array.from({ length: 5 }, () => "<span>--:--:--</span>").join("");
        ctx.fillStyle = "rgba(255,255,255,.55)";
        ctx.font = "28px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
        ctx.fillText("waiting for readings", 24, height / 2);
        return;
      }

      const powerLine = smooth(series, "power_db");
      const baselineLine = smooth(series, "baseline_db");
      const values = [...powerLine, ...baselineLine]
        .filter((value) => Number.isFinite(value));
      const min = Math.min(...values) - 2;
      const max = Math.max(...values) + 2;
      const span = Math.max(1, max - min);
      const plotLeft = 56;
      const plotRight = width - 18;
      const plotTop = 18;
      const plotBottom = height - 36;
      const x = (index) => plotLeft + (index / Math.max(1, series.length - 1)) * (plotRight - plotLeft);
      const y = (value) => plotBottom - ((value - min) / span) * (plotBottom - plotTop);

      ctx.strokeStyle = "rgba(255,255,255,.16)";
      ctx.lineWidth = 1;
      ctx.fillStyle = "rgba(255,255,255,.55)";
      ctx.font = "18px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      for (let i = 0; i < 4; i += 1) {
        const gy = plotTop + i * ((plotBottom - plotTop) / 3);
        const label = max - i * (span / 3);
        ctx.beginPath();
        ctx.moveTo(plotLeft, gy);
        ctx.lineTo(plotRight, gy);
        ctx.stroke();
        ctx.fillText(`${label.toFixed(0)} dB`, 6, gy + 6);
      }

      ctx.fillStyle = "rgba(255,255,255,.58)";
      ctx.font = "16px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      ctx.textAlign = "center";
      const tickCount = Math.min(5, series.length);
      const timeLabels = [];
      for (let i = 0; i < tickCount; i += 1) {
        const index = Math.round((i / Math.max(1, tickCount - 1)) * (series.length - 1));
        const tx = x(index);
        const label = String(series[index].timestamp || "");
        timeLabels.push(label);
        ctx.beginPath();
        ctx.moveTo(tx, plotBottom);
        ctx.lineTo(tx, plotBottom + 6);
        ctx.stroke();
        ctx.fillText(label, tx, height - 10);
      }
      ctx.textAlign = "start";
      timeAxis.innerHTML = timeLabels.map((label) => `<span>${label}</span>`).join("");

      function line(values, color, widthPx) {
        ctx.strokeStyle = color;
        ctx.lineWidth = widthPx;
        ctx.beginPath();
        values.forEach((value, index) => {
          const px = x(index);
          const py = y(value);
          if (index === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        });
        ctx.stroke();
      }

      line(baselineLine, "rgba(255,255,255,.38)", 3);
      line(powerLine, "#fff4d6", 7);

      const last = series[series.length - 1];
      ctx.fillStyle = "#fff4d6";
      ctx.beginPath();
      ctx.arc(x(series.length - 1), y(powerLine[powerLine.length - 1]), 7, 0, Math.PI * 2);
      ctx.fill();

      $("chart-meta").textContent =
        `${series.length} samples | ${min.toFixed(1)} to ${max.toFixed(1)} dB`;
    }
    async function markObservation(label) {
      const response = await fetch(`/mark?label=${encodeURIComponent(label)}`, {
        cache: "no-store"
      });
      const result = await response.json();
      if (result.event_type === "interval_start") {
        $("marker-status").textContent = `vehicle interval started at ${result.timestamp}`;
      } else if (result.event_type === "interval_end") {
        $("marker-status").textContent =
          `vehicle interval ended at ${result.timestamp} (${result.duration_seconds}s)`;
      } else {
        $("marker-status").textContent =
          `marked ${result.label.replaceAll("_", " ")} at ${result.timestamp}`;
      }
    }
    async function applyTune() {
      const tune = $("tune-select").value;
      if (!tune) return;
      $("marker-status").textContent = `switching to ${tune.replaceAll("_", " ")} tune...`;
      const response = await fetch(`/select-tune?tune=${encodeURIComponent(tune)}`, {
        cache: "no-store"
      });
      const result = await response.json();
      $("marker-status").textContent = result.ok
        ? `selected ${result.tune_label}; scanner restarting`
        : result.error || "could not change tune";
      if (result.ok) tuneSelectionDirty = false;
      setTimeout(() => refresh().catch(() => {}), 1200);
    }
    function renderTuning(tuning) {
      const select = $("tune-select");
      const profiles = tuning.profiles || [];
      const selectedTune = tuning.tune || "";
      if (profiles.length && select.dataset.loaded !== "1") {
        select.innerHTML = profiles.map((profile) =>
          `<option value="${profile.name}">${profile.label}</option>`
        ).join("");
        select.dataset.loaded = "1";
      }
      const activeTuneChanged = selectedTune && selectedTune !== lastActiveTune;
      if (selectedTune && (!tuneSelectionDirty || activeTuneChanged) && select.value !== selectedTune) {
        select.value = selectedTune;
      }
      if (activeTuneChanged) {
        lastActiveTune = selectedTune;
        tuneSelectionDirty = false;
      }
      const rows = [
        ["active tune", tuning.tune_label || tuning.tune],
        ["range", tuning.range],
        ["interval", tuning.interval],
        ["gain", tuning.gain],
        ["threshold", `${Number(tuning.threshold_db).toFixed(1)} dB`],
        ["min power", `${Number(tuning.incident_min_power_db).toFixed(1)} dB`],
        ["confirm", `${tuning.min_incident_samples} samples`],
        ["hold", `${tuning.hold_samples} samples`],
        ["cluster", `${Number(tuning.cluster_khz).toFixed(0)} kHz`],
        ["session", tuning.session_id]
      ];
      $("tuning").innerHTML = rows.map(([key, value]) =>
        `<div class="tune-row"><span>${key}</span><span>${value ?? "---"}</span></div>`
      ).join("");
    }
    function renderStrongestIncidents(incidents) {
      if (!incidents || !incidents.length) {
        $("strongest-incidents").innerHTML =
          `<div class="muted-line">No incidents yet</div>`;
        return;
      }
      $("strongest-incidents").innerHTML = incidents.slice(0, 5).map((incident) =>
        `<div class="incident-row">
          <span>${incident.time}</span>
          <span>${incident.frequency_mhz.toFixed(6)} MHz</span>
          <span>${incident.peak_power_db.toFixed(1)} dB</span>
          <span>${incident.peak_delta_db >= 0 ? "+" : ""}${incident.peak_delta_db.toFixed(1)}</span>
        </div>`
      ).join("");
    }
    function renderRecentPeaks(peaks) {
      if (!peaks || !peaks.length) {
        $("recent-peaks").innerHTML = `<div class="muted-line">no peaks yet</div>`;
        return;
      }
      $("recent-peaks").innerHTML = peaks.slice(0, 5).map((peak) =>
        `<div class="peak-row">
          <span class="peak-time">${peak.time || shortTime(peak.timestamp)}</span>
          <span class="peak-age">${Number(peak.age_seconds || 0).toFixed(0)}s ago</span>
          <span class="peak-db">${Number(peak.power_db).toFixed(1)} dB</span>
          <span class="peak-detail">${Number(peak.frequency_mhz).toFixed(6)} MHz | ${Number(peak.delta_db) >= 0 ? "+" : ""}${Number(peak.delta_db).toFixed(1)} dB delta</span>
        </div>`
      ).join("");
    }
    function setMode(mode) {
      currentMode = mode;
      $("detector-mode").classList.toggle("hidden", mode !== "detector");
      $("analysis-mode").classList.toggle("hidden", mode !== "analysis");
      $("detector-tab").classList.toggle("active", mode === "detector");
      $("analysis-tab").classList.toggle("active", mode === "analysis");
      if (mode === "analysis") loadAnalysis().catch(() => {});
    }
    function fmtDb(value) {
      return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} dB` : "---";
    }
    function fmtDuration(seconds) {
      if (!Number.isFinite(Number(seconds))) return "---";
      const total = Math.max(0, Math.round(Number(seconds)));
      const minutes = Math.floor(total / 60);
      const remainder = total % 60;
      return `${minutes}m ${remainder}s`;
    }
    function shortTime(timestamp) {
      return timestamp ? String(timestamp).slice(11, 19) : "---";
    }
    function fmtSeconds(value) {
      return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}s` : "---";
    }
    function renderReasons(reasons) {
      return (reasons || []).length ? reasons.join(", ") : "no repeating pattern";
    }
    function renderAnalysis(analysis) {
      if (!analysis || !analysis.ok || analysis.empty) {
        $("analysis-duration").textContent = "---";
        $("analysis-events").textContent = "---";
        $("analysis-labels").textContent = "---";
        $("analysis-strict").textContent = "---";
        $("analysis-health").className = "quality-card";
        $("analysis-health").innerHTML = `<div class="muted-line">${analysis?.message || analysis?.error || "No analysis yet"}</div>`;
        $("analysis-pattern-summary").innerHTML = "";
        $("analysis-pattern-events").innerHTML = "";
        $("analysis-pattern-timeline").innerHTML = "";
        $("analysis-intervals").innerHTML = `<div class="muted-line">${analysis?.message || analysis?.error || "No analysis yet"}</div>`;
        $("analysis-top-events").innerHTML = "";
        $("analysis-similar").innerHTML = "";
        $("analysis-minutes").innerHTML = "";
        return;
      }
      $("analysis-duration").textContent = fmtDuration(analysis.duration_seconds);
      $("analysis-events").textContent = analysis.analysis_burst_event_count || analysis.burst_event_count || 0;
      $("analysis-labels").textContent = analysis.labelled_intervals?.length || 0;
      $("analysis-strict").textContent = analysis.strict_confirmed_incident_count || 0;

      const quality = analysis.label_quality || {};
      $("analysis-health").className = `quality-card ${quality.level || ""}`;
      $("analysis-health").innerHTML =
        `<div class="quality-title">${quality.label || "Session health"}</div>
        <div class="muted-line">${quality.message || ""}</div>
        <div class="analysis-list" style="margin-top:12px">
          <div class="analysis-row wide">
            <span>analysis</span>
            <span>power >= ${fmtDb(analysis.analysis_thresholds?.power_db)} or delta >= +${Number(analysis.analysis_thresholds?.delta_db || 0).toFixed(1)} dB</span>
            <span>${analysis.analysis_burst_event_count || analysis.burst_event_count || 0} bursts</span>
            <span>${analysis.strict_confirmed_incident_count || 0} strict</span>
            <span>${fmtDb(analysis.summary?.max_power_db)}</span>
            <span>+${Number(analysis.summary?.max_delta_db || 0).toFixed(1)}</span>
          </div>
        </div>`;

      const pattern = analysis.pattern_summary || {};
      const counts = pattern.counts || {};
      $("analysis-pattern-summary").innerHTML =
        `<div class="pattern-summary">
          <div class="pattern-message">${pattern.message || "No classifier result yet."}</div>
          <div class="pattern-count"><strong>${counts.high || 0}</strong><span>high</span></div>
          <div class="pattern-count"><strong>${counts.medium || 0}</strong><span>medium</span></div>
          <div class="pattern-count"><strong>${counts.low || 0}</strong><span>low</span></div>
          <div class="pattern-count"><strong>${counts.noise || 0}</strong><span>one-off</span></div>
        </div>`;

      const patternEvents = analysis.pattern_events || [];
      $("analysis-pattern-events").innerHTML = patternEvents.length ? patternEvents.map((event) => {
        const cls = event.classification || {};
        return `<div class="pattern-row">
          <span>${shortTime(event.start)}-${shortTime(event.end)}</span>
          <span><span class="pattern-chip ${cls.level || "noise"}">${cls.label || "unclassified"}</span></span>
          <span>${cls.score ?? 0}/100</span>
          <span>${fmtDb(event.peak_power_db)}</span>
          <span>+${Number(event.max_delta_db || event.peak_delta_db || 0).toFixed(1)}</span>
          <span>${fmtSeconds(event.seconds_since_previous_burst)}</span>
          <span>${renderReasons(cls.reasons)}</span>
        </div>`;
      }).join("") : `<div class="muted-line">No burst-pattern events to review.</div>`;

      const timeline = analysis.pattern_timeline || [];
      $("analysis-pattern-timeline").innerHTML = timeline.length ? timeline.map((window) =>
        `<div class="pattern-row">
          <span>${shortTime(window.start)}-${shortTime(window.end)}</span>
          <span><span class="pattern-chip ${window.dominant_level || "noise"}">${window.dominant_level || "noise"}</span></span>
          <span>${window.best_score ?? 0}/100</span>
          <span>${fmtDb(window.strongest_power_db)}</span>
          <span>+${Number(window.max_delta_db || 0).toFixed(1)}</span>
          <span>${window.count || 0} bursts</span>
          <span>${Number(window.top_frequency_mhz || 0).toFixed(6)} MHz</span>
        </div>`
      ).join("") : `<div class="muted-line">No notable activity windows found.</div>`;

      const intervals = analysis.labelled_intervals || [];
      $("analysis-intervals").innerHTML = intervals.length ? intervals.map((item) =>
        `<div class="analysis-row wide">
          <span>${shortTime(item.start)}</span>
          <span>${item.end ? shortTime(item.end) : "active"}</span>
          <span>${fmtDuration(item.duration_seconds)}</span>
          <span>${fmtDb(item.summary?.max_power_db)}</span>
          <span>+${Number(item.summary?.max_delta_db || 0).toFixed(1)}</span>
          <span>${item.summary?.count_ge_minus_20_db || 0} hits</span>
        </div>`
      ).join("") : `<div class="muted-line">No labels captured. Use Vehicle in sight in detector mode when a relevant vehicle is visible.</div>`;

      const events = analysis.top_events || [];
      $("analysis-top-events").innerHTML = events.length ? events.slice(0, 10).map((event) =>
        `<div class="analysis-row wide">
          <span>${shortTime(event.peak_time)}</span>
          <span>${event.peak_frequency_mhz.toFixed(6)} MHz</span>
          <span>${fmtDb(event.peak_power_db)}</span>
          <span>+${Number(event.max_delta_db || event.peak_delta_db).toFixed(1)}</span>
          <span>${fmtDuration(event.duration_seconds)}</span>
          <span>${event.count_ge_minus_20_db || 0}/${event.count_ge_plus_26_delta || 0}</span>
        </div>`
      ).join("") : `<div class="muted-line">No burst events yet</div>`;

      const similar = analysis.similar_events || [];
      $("analysis-similar").innerHTML = intervals.length
        ? (similar.length ? similar.map((event) =>
          `<div class="analysis-row wide">
            <span>${shortTime(event.peak_time)}</span>
            <span>${event.peak_frequency_mhz.toFixed(6)} MHz</span>
            <span>${fmtDb(event.peak_power_db)}</span>
            <span>+${Number(event.max_delta_db || event.peak_delta_db).toFixed(1)}</span>
            <span>${fmtDuration(event.duration_seconds)}</span>
            <span>${event.count_ge_minus_20_db || 0}/${event.count_ge_plus_26_delta || 0}</span>
          </div>`
        ).join("") : `<div class="muted-line">No unlabelled windows were similar to the labelled interval.</div>`)
        : `<div class="muted-line">Similar-window comparison needs at least one labelled vehicle interval.</div>`;

      const minutes = analysis.minute_summary || [];
      $("analysis-minutes").innerHTML = minutes.slice(-20).map((minute) =>
        `<div class="analysis-row wide">
          <span>${minute.minute}</span>
          <span>${minute.samples} samples</span>
          <span>${fmtDb(minute.max_power_db)}</span>
          <span>+${Number(minute.max_delta_db || 0).toFixed(1)}</span>
          <span>${minute.count_ge_minus_20_db || 0} hits</span>
          <span>${(minute.top_frequencies || [])[0]?.frequency_mhz || "---"}</span>
        </div>`
      ).join("");
    }
    async function loadAnalysis() {
      const now = Date.now();
      if (now - lastAnalysisAt < 4000) return;
      lastAnalysisAt = now;
      const response = await fetch("/analysis", { cache: "no-store" });
      renderAnalysis(await response.json());
    }
    $("detector-tab").addEventListener("click", () => setMode("detector"));
    $("analysis-tab").addEventListener("click", () => setMode("analysis"));
    document.querySelectorAll(".mark-button").forEach((button) => {
      button.addEventListener("click", () => markObservation(button.dataset.label));
    });
    $("tune-select").addEventListener("change", () => {
      tuneSelectionDirty = true;
    });
    $("apply-tune").addEventListener("click", () => applyTune().catch(() => {
      $("marker-status").textContent = "could not change tune";
    }));
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
      $("power-main").textContent = main ? `${main.power_db.toFixed(1)} dB` : "---";
      $("delta").textContent = `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} dB above baseline`;
      $("delta-card").textContent = `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} dB`;
      $("freq").textContent = main ? `${main.frequency_mhz.toFixed(6)} MHz` : "---";
      $("baseline").textContent = main && Number.isFinite(main.baseline_db) ? `${main.baseline_db.toFixed(1)} dB` : "---";
      $("incidents").textContent = active.length;
      renderRecentPeaks(state.recent_peaks || []);
      const tuning = state.tuning || {};
      $("mode").textContent = tuning.tune_label || (state.range === "380M:385M:25k" ? "mobile" : "custom");
      $("mode-meta").textContent = `${state.range || ""} | gain ${tuning.gain ?? "---"} | threshold ${Number(tuning.threshold_db || 0).toFixed(1)} dB`;
      $("updated").textContent = state.updated_at || "waiting";
      $("range").textContent = state.range || "";
      $("samples").textContent = `samples ${state.sample_count || 0}`;
      const markerButton = document.querySelector(".mark-button");
      if (markerButton) {
        markerButton.textContent = state.vehicle_interval_active
          ? "Vehicle no longer in sight"
          : "Vehicle in sight";
      }
      const prompt = $("label-prompt");
      if (prompt) {
        if (state.vehicle_interval_active) {
          prompt.className = "label-prompt active";
          prompt.textContent = `Vehicle interval recording since ${shortTime(state.vehicle_interval_started_at)}`;
        } else if (state.label_prompt) {
          prompt.className = "label-prompt warning";
          prompt.textContent = state.label_prompt;
        } else {
          prompt.className = "label-prompt";
          prompt.textContent = "No vehicle label active";
        }
      }
      renderTuning(tuning);
      renderStrongestIncidents(state.strongest_incidents || []);
      drawChart(state.series || []);
      if (currentMode === "analysis") loadAnalysis().catch(() => {});
    }
    refresh().catch(() => {});
    setInterval(() => refresh().catch(() => {}), 1000);
  </script>
</body>
</html>
"""


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


def open_cluster_activity_log(path: Path) -> tuple[object, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    fp = path.open("a", newline="")
    writer = csv.DictWriter(
        fp,
        fieldnames=[
            "timestamp",
            "event",
            "cluster_id",
            "center_frequency_mhz",
            "low_frequency_mhz",
            "high_frequency_mhz",
            "cluster_width_khz",
            "bin_count",
            "power_db",
            "baseline_db",
            "delta_db",
            "threshold_db",
            "incident_min_power_db",
            "incident_start",
            "incident_duration_seconds",
            "peak_power_db",
            "peak_delta_db",
            "peak_frequency_mhz",
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


def open_observations_log(path: Path) -> tuple[object, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    fp = path.open("a", newline="")
    writer = csv.DictWriter(
        fp,
        fieldnames=[
            "timestamp",
            "label",
            "event_type",
            "interval_id",
            "interval_start",
            "interval_end",
            "duration_seconds",
            "note",
            "range",
            "sample",
            "current_frequency_mhz",
            "current_power_db",
            "current_baseline_db",
            "current_delta_db",
            "recent_peak_frequency_mhz",
            "recent_peak_power_db",
            "recent_peak_delta_db",
        ],
    )
    if needs_header:
        writer.writeheader()
        fp.flush()
    return fp, writer


def sanitize_label(label: str) -> str:
    cleaned = []
    for char in label.strip().lower():
        if char.isalnum() or char in {"_", "-"}:
            cleaned.append(char)
        elif char.isspace():
            cleaned.append("_")
    return "".join(cleaned)[:64] or "manual_marker"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def summarize_rank1_window(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        return {
            "samples": 0,
            "avg_power_db": None,
            "median_power_db": None,
            "p95_power_db": None,
            "max_power_db": None,
            "max_delta_db": None,
            "count_ge_minus_20_db": 0,
            "count_ge_plus_26_delta": 0,
            "top_frequencies": [],
        }

    powers = [float(row["power_db"]) for row in rows]
    deltas = [float(row["delta_db"]) for row in rows]
    freq_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        freq_counts[f"{float(row['frequency_mhz']):.6f}"] += 1

    return {
        "samples": len(rows),
        "avg_power_db": statistics.mean(powers),
        "median_power_db": statistics.median(powers),
        "p95_power_db": percentile(powers, 0.95),
        "max_power_db": max(powers),
        "max_delta_db": max(deltas),
        "count_ge_minus_20_db": sum(1 for value in powers if value >= -20),
        "count_ge_plus_26_delta": sum(1 for value in deltas if value >= 26),
        "top_frequencies": sorted(
            [{"frequency_mhz": freq, "count": count} for freq, count in freq_counts.items()],
            key=lambda item: int(item["count"]),
            reverse=True,
        )[:5],
    }


def count_strict_confirmed_incidents(cluster_path: Path) -> int:
    if not cluster_path.exists():
        return 0
    with cluster_path.open(newline="") as fp:
        return sum(1 for row in csv.DictReader(fp) if row.get("event") == "start")


def event_overlaps_intervals(
    event: dict[str, object],
    intervals: list[dict[str, object]],
) -> bool:
    event_start = dt.datetime.fromisoformat(str(event["start"]))
    event_end = dt.datetime.fromisoformat(str(event["end"]))
    for interval in intervals:
        start = dt.datetime.fromisoformat(str(interval["start"]))
        end_text = interval.get("end")
        end = dt.datetime.fromisoformat(str(end_text)) if end_text else event_end
        if event_start <= end and event_end >= start:
            return True
    return False


def event_overlap_label(
    event: dict[str, object],
    intervals: list[dict[str, object]],
) -> str | None:
    event_start = dt.datetime.fromisoformat(str(event["start"]))
    event_end = dt.datetime.fromisoformat(str(event["end"]))
    for interval in intervals:
        start = dt.datetime.fromisoformat(str(interval["start"]))
        end_text = interval.get("end")
        end = dt.datetime.fromisoformat(str(end_text)) if end_text else event_end
        if event_start <= end and event_end >= start:
            return str(interval.get("label") or "vehicle_nearby")
    return None


def similar_events_to_labels(
    events: list[dict[str, object]],
    intervals: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not events or not intervals:
        return []

    labelled_max_power = max(
        (
            float(interval["summary"]["max_power_db"])
            for interval in intervals
            if interval.get("summary")
            and interval["summary"].get("max_power_db") is not None
        ),
        default=None,
    )
    labelled_max_delta = max(
        (
            float(interval["summary"]["max_delta_db"])
            for interval in intervals
            if interval.get("summary")
            and interval["summary"].get("max_delta_db") is not None
        ),
        default=None,
    )
    if labelled_max_power is None and labelled_max_delta is None:
        return []

    similar: list[dict[str, object]] = []
    for event in events:
        if event_overlaps_intervals(event, intervals):
            continue
        power_match = (
            labelled_max_power is not None
            and float(event["peak_power_db"]) >= labelled_max_power - 3.0
        )
        delta_match = (
            labelled_max_delta is not None
            and float(event["max_delta_db"]) >= labelled_max_delta - 3.0
        )
        if power_match or delta_match:
            similar.append(
                {
                    **event,
                    "comparison": "similar_or_stronger_than_label",
                }
            )

    return sorted(
        similar,
        key=lambda item: (
            float(item["peak_power_db"]),
            float(item["max_delta_db"]),
        ),
        reverse=True,
    )[:10]


def classify_burst_pattern(event: dict[str, object]) -> dict[str, object]:
    duration = float(event.get("duration_seconds") or 0)
    peak_power = float(event.get("peak_power_db") or -999)
    max_delta = float(event.get("max_delta_db") or event.get("peak_delta_db") or 0)
    freq_stability = float(event.get("frequency_stability_khz") or 999)
    prev_interval = event.get("seconds_since_previous_burst")
    next_interval = event.get("seconds_to_next_burst")
    repeated_near_4s = bool(event.get("repeat_near_4s"))
    double_within_1s = bool(event.get("double_burst_within_1s"))
    labelled = bool(event.get("label_overlap"))
    samples = int(event.get("samples") or 0)
    count_strong = int(event.get("count_ge_minus_20_db") or 0)
    count_delta = int(event.get("count_ge_plus_26_delta") or 0)

    score = 0
    reasons: list[str] = []

    if labelled:
        score += 35
        reasons.append("inside labelled vehicle window")
    if repeated_near_4s:
        score += 30
        reasons.append("repeat interval near 4s")
    if double_within_1s:
        score += 18
        reasons.append("double burst within 1s")
    if freq_stability <= 30:
        score += 12
        reasons.append("stable frequency")
    elif freq_stability <= 80:
        score += 6
        reasons.append("moderately stable frequency")
    if peak_power >= -20:
        score += 12
        reasons.append("strong absolute power")
    if max_delta >= 26:
        score += 10
        reasons.append("large delta")
    if samples >= 3:
        score += 6
    if count_strong >= 2:
        score += 6
    if count_delta >= 2:
        score += 6

    isolated = (
        not repeated_near_4s
        and not double_within_1s
        and not labelled
        and (prev_interval is None or float(prev_interval) > 12)
        and (next_interval is None or float(next_interval) > 12)
    )
    if isolated and duration <= 3 and peak_power < -18:
        score -= 20
        reasons.append("isolated short burst")

    if score >= 65:
        level = "high"
        label = "TETRA-like candidate"
    elif score >= 42:
        level = "medium"
        label = "Possible patterned RF"
    elif score >= 20:
        level = "low"
        label = "Interesting burst"
    else:
        level = "noise"
        label = "Likely noise or one-off"

    timing = "isolated"
    if repeated_near_4s:
        timing = "near 4s repeat"
    elif double_within_1s:
        timing = "double burst"
    elif prev_interval is not None:
        timing = f"{float(prev_interval):.1f}s after previous"

    return {
        "score": max(0, min(100, score)),
        "level": level,
        "label": label,
        "timing": timing,
        "reasons": reasons[:5],
    }


def enrich_burst_patterns(
    events: list[dict[str, object]],
    intervals: list[dict[str, object]],
) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    ordered = sorted(events, key=lambda item: str(item["start"]))
    for index, event in enumerate(ordered):
        previous_event = ordered[index - 1] if index > 0 else None
        next_event = ordered[index + 1] if index + 1 < len(ordered) else None
        start_dt = dt.datetime.fromisoformat(str(event["start"]))
        previous_interval = (
            (start_dt - dt.datetime.fromisoformat(str(previous_event["start"]))).total_seconds()
            if previous_event is not None
            else None
        )
        next_interval = (
            (dt.datetime.fromisoformat(str(next_event["start"])) - start_dt).total_seconds()
            if next_event is not None
            else None
        )
        repeat_near_4s = any(
            value is not None and 3.0 <= float(value) <= 5.5
            for value in [previous_interval, next_interval]
        )
        double_within_1s = any(
            value is not None and 0.2 <= float(value) <= 1.4
            for value in [previous_interval, next_interval]
        )
        labelled = event_overlap_label(event, intervals)
        enriched_event = {
            **event,
            "seconds_since_previous_burst": previous_interval,
            "seconds_to_next_burst": next_interval,
            "repeat_near_4s": repeat_near_4s,
            "double_burst_within_1s": double_within_1s,
            "label_overlap": labelled,
        }
        enriched_event["classification"] = classify_burst_pattern(enriched_event)
        enriched.append(enriched_event)
    return enriched


def summarize_burst_patterns(events: list[dict[str, object]]) -> dict[str, object]:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        counts[str(event["classification"]["level"])] += 1

    high_events = [
        event
        for event in events
        if event["classification"]["level"] in {"high", "medium"}
    ]
    if high_events:
        strongest = max(
            high_events,
            key=lambda item: (
                int(item["classification"]["score"]),
                float(item["peak_power_db"]),
            ),
        )
        message = (
            f"Most pattern-like activity was {strongest['start'][11:19]}-"
            f"{strongest['end'][11:19]} at "
            f"{float(strongest['peak_frequency_mhz']):.6f} MHz."
        )
    elif events:
        strongest = max(events, key=lambda item: float(item["peak_power_db"]))
        message = (
            f"No strong periodic pattern found. Strongest one-off burst was "
            f"{strongest['peak_time'][11:19]} at "
            f"{float(strongest['peak_frequency_mhz']):.6f} MHz."
        )
    else:
        message = "No burst events were found in this session."

    return {
        "message": message,
        "counts": dict(counts),
        "high_or_medium_count": len(high_events),
    }


def build_pattern_timeline(events: list[dict[str, object]]) -> list[dict[str, object]]:
    if not events:
        return []

    level_rank = {"noise": 0, "low": 1, "medium": 2, "high": 3}
    windows: list[dict[str, object]] = []
    current: dict[str, object] | None = None

    for event in sorted(events, key=lambda item: str(item["start"])):
        level = str(event["classification"]["level"])
        event_start = dt.datetime.fromisoformat(str(event["start"]))
        event_end = dt.datetime.fromisoformat(str(event["end"]))
        if current is None:
            current = {
                "start": event["start"],
                "end": event["end"],
                "end_dt": event_end,
                "count": 1,
                "strongest_power_db": float(event["peak_power_db"]),
                "max_delta_db": float(event["max_delta_db"]),
                "best_score": int(event["classification"]["score"]),
                "dominant_level": level,
                "levels": defaultdict(int),
                "top_frequency_mhz": float(event["peak_frequency_mhz"]),
            }
            current["levels"][level] += 1
            continue

        gap = (event_start - current["end_dt"]).total_seconds()
        dominant_level = str(current["dominant_level"])
        should_extend = gap <= 90 or (
            level_rank.get(level, 0) >= 2
            and level_rank.get(dominant_level, 0) >= 2
            and gap <= 180
        )
        if not should_extend:
            levels = dict(current.pop("levels"))
            current.pop("end_dt")
            current["level_counts"] = levels
            windows.append(current)
            current = {
                "start": event["start"],
                "end": event["end"],
                "end_dt": event_end,
                "count": 1,
                "strongest_power_db": float(event["peak_power_db"]),
                "max_delta_db": float(event["max_delta_db"]),
                "best_score": int(event["classification"]["score"]),
                "dominant_level": level,
                "levels": defaultdict(int),
                "top_frequency_mhz": float(event["peak_frequency_mhz"]),
            }
            current["levels"][level] += 1
            continue

        current["end"] = event["end"]
        current["end_dt"] = event_end
        current["count"] = int(current["count"]) + 1
        current["strongest_power_db"] = max(
            float(current["strongest_power_db"]),
            float(event["peak_power_db"]),
        )
        current["max_delta_db"] = max(
            float(current["max_delta_db"]),
            float(event["max_delta_db"]),
        )
        if int(event["classification"]["score"]) > int(current["best_score"]):
            current["best_score"] = int(event["classification"]["score"])
            current["dominant_level"] = level
            current["top_frequency_mhz"] = float(event["peak_frequency_mhz"])
        current["levels"][level] += 1

    if current is not None:
        levels = dict(current.pop("levels"))
        current.pop("end_dt")
        current["level_counts"] = levels
        windows.append(current)

    return sorted(
        windows,
        key=lambda item: (
            int(item["best_score"]),
            int(item["count"]),
            float(item["strongest_power_db"]),
        ),
        reverse=True,
    )[:12]


def label_quality_payload(
    *,
    duration_seconds: float,
    burst_event_count: int,
    labelled_interval_count: int,
) -> dict[str, object]:
    if labelled_interval_count:
        return {
            "level": "green",
            "label": "Labelled",
            "message": f"{labelled_interval_count} labelled vehicle window"
            f"{'' if labelled_interval_count == 1 else 's'} captured",
        }
    if burst_event_count and duration_seconds >= 20 * 60:
        return {
            "level": "red",
            "label": "Unlabelled drive",
            "message": (
                f"{burst_event_count} RF burst events detected, "
                "0 labelled vehicle windows"
            ),
        }
    if burst_event_count:
        return {
            "level": "amber",
            "label": "Needs labels",
            "message": (
                f"{burst_event_count} RF burst events detected, "
                "0 labelled vehicle windows"
            ),
        }
    return {
        "level": "green",
        "label": "Quiet",
        "message": "No RF burst events or labelled vehicle windows in this session",
    }


def collapse_burst_events(
    rows: list[dict[str, object]],
    *,
    power_threshold: float = -25.0,
    delta_threshold: float = 8.0,
    gap_seconds: float = 6.0,
) -> list[dict[str, object]]:
    candidates = [
        row
        for row in rows
        if float(row["power_db"]) >= power_threshold
        or float(row["delta_db"]) >= delta_threshold
    ]
    grouped: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []

    for row in candidates:
        if current:
            gap = (row["dt"] - current[-1]["dt"]).total_seconds()
            if gap > gap_seconds:
                grouped.append(current)
                current = []
        current.append(row)
    if current:
        grouped.append(current)

    events: list[dict[str, object]] = []
    for group in grouped:
        peak_power = max(group, key=lambda item: float(item["power_db"]))
        peak_delta = max(group, key=lambda item: float(item["delta_db"]))
        freq_counts: dict[str, int] = defaultdict(int)
        for row in group:
            freq_counts[f"{float(row['frequency_mhz']):.6f}"] += 1
        events.append(
            {
                "start": group[0]["timestamp"],
                "end": group[-1]["timestamp"],
                "duration_seconds": (
                    group[-1]["dt"] - group[0]["dt"]
                ).total_seconds()
                + 1,
                "peak_time": peak_power["timestamp"],
                "peak_frequency_mhz": float(peak_power["frequency_mhz"]),
                "peak_power_db": float(peak_power["power_db"]),
                "peak_delta_db": float(peak_power["delta_db"]),
                "max_delta_time": peak_delta["timestamp"],
                "max_delta_db": float(peak_delta["delta_db"]),
                "samples": len(group),
                "count_ge_minus_20_db": sum(
                    1 for row in group if float(row["power_db"]) >= -20
                ),
                "count_ge_plus_26_delta": sum(
                    1 for row in group if float(row["delta_db"]) >= 26
                ),
                "frequency_stability_khz": (
                    statistics.pstdev(
                        [float(row["frequency_mhz"]) for row in group]
                    )
                    * 1000
                    if len(group) > 1
                    else 0.0
                ),
                "top_frequencies": sorted(
                    [
                        {"frequency_mhz": freq, "count": count}
                        for freq, count in freq_counts.items()
                    ],
                    key=lambda item: int(item["count"]),
                    reverse=True,
                )[:3],
            }
        )
    return events


def load_drive_analysis(
    readings_path: Path,
    observations_path: Path,
) -> dict[str, object]:
    if not readings_path.exists():
        return {"ok": False, "error": f"readings file not found: {readings_path}"}

    rank1: list[dict[str, object]] = []
    with readings_path.open(newline="") as fp:
        for row in csv.DictReader(fp):
            if row.get("rank") != "1":
                continue
            parsed = dict(row)
            parsed["dt"] = dt.datetime.fromisoformat(row["timestamp"])
            parsed["sample"] = int(row["sample"])
            parsed["power_db"] = float(row["power_db"])
            parsed["baseline_db"] = float(row["baseline_db"])
            parsed["delta_db"] = float(row["delta_db"])
            parsed["frequency_mhz"] = float(row["frequency_mhz"])
            rank1.append(parsed)

    if not rank1:
        return {"ok": True, "empty": True, "message": "No readings yet"}

    observations: list[dict[str, object]] = []
    if observations_path.exists():
        with observations_path.open(newline="") as fp:
            for row in csv.DictReader(fp):
                if not row.get("timestamp"):
                    continue
                parsed = dict(row)
                parsed["dt"] = dt.datetime.fromisoformat(row["timestamp"])
                observations.append(parsed)

    intervals: list[dict[str, object]] = []
    open_intervals: dict[str, dict[str, object]] = {}
    for obs in observations:
        event_type = obs.get("event_type") or "point"
        interval_id = str(obs.get("interval_id") or "")
        if event_type == "interval_start" and interval_id:
            open_intervals[interval_id] = obs
        elif event_type == "interval_end" and interval_id:
            start = open_intervals.pop(interval_id, None)
            if start is not None:
                start_dt = start["dt"]
                end_dt = obs["dt"]
                rows = [row for row in rank1 if start_dt <= row["dt"] <= end_dt]
                intervals.append(
                    {
                        "id": interval_id,
                        "label": start.get("label", "vehicle_nearby"),
                        "start": start["timestamp"],
                        "end": obs["timestamp"],
                        "duration_seconds": (end_dt - start_dt).total_seconds(),
                        "summary": summarize_rank1_window(rows),
                    }
                )
    for interval_id, start in open_intervals.items():
        start_dt = start["dt"]
        end_dt = rank1[-1]["dt"]
        rows = [row for row in rank1 if start_dt <= row["dt"] <= end_dt]
        intervals.append(
            {
                "id": interval_id,
                "label": start.get("label", "vehicle_nearby"),
                "start": start["timestamp"],
                "end": None,
                "duration_seconds": (end_dt - start_dt).total_seconds(),
                "active": True,
                "summary": summarize_rank1_window(rows),
            }
        )

    session_start_dt = rank1[0]["dt"]
    events = [
        event
        for event in collapse_burst_events(rank1)
        if (
            dt.datetime.fromisoformat(str(event["start"])) - session_start_dt
        ).total_seconds()
        >= 2
    ]
    events = enrich_burst_patterns(events, intervals)
    pattern_summary = summarize_burst_patterns(events)
    top_events = sorted(
        events,
        key=lambda item: (
            int(item["classification"]["score"]),
            float(item["peak_power_db"]),
            int(item["count_ge_minus_20_db"]),
            int(item["count_ge_plus_26_delta"]),
            float(item["duration_seconds"]),
        ),
        reverse=True,
    )[:10]
    sustained_events = sorted(
        events,
        key=lambda item: (
            int(item["count_ge_minus_20_db"]),
            int(item["count_ge_plus_26_delta"]),
            float(item["duration_seconds"]),
            float(item["peak_power_db"]),
        ),
        reverse=True,
    )[:10]

    minute_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rank1:
        minute_rows[row["dt"].strftime("%H:%M")].append(row)
    minute_summary = [
        {
            "minute": minute,
            **summarize_rank1_window(rows),
        }
        for minute, rows in sorted(minute_rows.items())
    ]

    cluster_path = readings_path.with_name("activity_clusters.csv")
    duration_seconds = (rank1[-1]["dt"] - rank1[0]["dt"]).total_seconds()
    strict_confirmed_incident_count = count_strict_confirmed_incidents(cluster_path)
    label_quality = label_quality_payload(
        duration_seconds=duration_seconds,
        burst_event_count=len(events),
        labelled_interval_count=len(intervals),
    )

    return {
        "ok": True,
        "readings_log": str(readings_path),
        "observations_log": str(observations_path),
        "cluster_log": str(cluster_path),
        "session_start": rank1[0]["timestamp"],
        "session_end": rank1[-1]["timestamp"],
        "duration_seconds": duration_seconds,
        "rank1_samples": len(rank1),
        "summary": summarize_rank1_window(rank1),
        "analysis_thresholds": {
            "power_db": -25.0,
            "delta_db": 8.0,
        },
        "strict_confirmed_incident_count": strict_confirmed_incident_count,
        "strict_confirmed_incidents": strict_confirmed_incident_count,
        "analysis_burst_event_count": len(events),
        "analysis_burst_events": len(events),
        "labelled_intervals": intervals,
        "labelled_vehicle_intervals": len(intervals),
        "label_quality": label_quality,
        "session_health": {
            "level": label_quality["level"],
            "message": label_quality["message"],
            "duration_minutes": duration_seconds / 60,
            "strict_confirmed_incidents": strict_confirmed_incident_count,
            "analysis_burst_events": len(events),
            "labelled_vehicle_intervals": len(intervals),
        },
        "burst_event_count": len(events),
        "top_events": top_events,
        "pattern_summary": pattern_summary,
        "pattern_events": sorted(
            events,
            key=lambda item: (
                int(item["classification"]["score"]),
                float(item["peak_power_db"]),
                float(item["max_delta_db"]),
            ),
            reverse=True,
        )[:20],
        "pattern_timeline": build_pattern_timeline(events),
        "sustained_events": sustained_events,
        "similar_events": similar_events_to_labels(events, intervals),
        "minute_summary": minute_summary,
    }


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


def write_cluster_activity(
    writer: csv.DictWriter,
    *,
    event: str,
    timestamp: dt.datetime,
    cluster: dict[str, object],
    track: dict[str, object],
    threshold_db: float,
    incident_min_power_db: float,
) -> None:
    incident_start = track["start"]
    writer.writerow(
        {
            "timestamp": timestamp.isoformat(timespec="seconds"),
            "event": event,
            "cluster_id": track["id"],
            "center_frequency_mhz": f"{float(cluster['center_frequency_mhz']):.6f}",
            "low_frequency_mhz": f"{float(cluster['cluster_low_mhz']):.6f}",
            "high_frequency_mhz": f"{float(cluster['cluster_high_mhz']):.6f}",
            "cluster_width_khz": f"{float(cluster['cluster_width_khz']):.1f}",
            "bin_count": int(cluster["bin_count"]),
            "power_db": f"{float(cluster['power_db']):.2f}",
            "baseline_db": f"{float(cluster['baseline_db']):.2f}",
            "delta_db": f"{float(cluster['delta_db']):.2f}",
            "threshold_db": f"{threshold_db:.2f}",
            "incident_min_power_db": f"{incident_min_power_db:.2f}",
            "incident_start": incident_start.isoformat(timespec="seconds"),
            "incident_duration_seconds": f"{(timestamp - incident_start).total_seconds():.0f}",
            "peak_power_db": f"{float(track['peak_power']):.2f}",
            "peak_delta_db": f"{float(track['peak_delta']):.2f}",
            "peak_frequency_mhz": f"{float(track['peak_frequency_mhz']):.6f}",
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


def peak_payload(
    reading: dict[str, float | int],
    timestamp: dt.datetime,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "time": timestamp.strftime("%H:%M:%S"),
        "frequency_mhz": float(reading["frequency_mhz"]),
        "power_db": float(reading["power_db"]),
        "baseline_db": float(reading["baseline_db"]),
        "delta_db": float(reading["delta_db"]),
    }


def update_recent_peaks(
    recent_peaks: deque[dict[str, object]],
    peak_sample: dict[str, object],
    *,
    merge_seconds: float = 2.0,
    merge_frequency_mhz: float = 0.15,
) -> None:
    if not recent_peaks:
        recent_peaks.appendleft(peak_sample)
        return

    previous_peak = recent_peaks[0]
    gap = (
        peak_sample["timestamp"] - previous_peak["timestamp"]
    ).total_seconds()
    same_burst = (
        gap <= merge_seconds
        and abs(
            float(peak_sample["frequency_mhz"])
            - float(previous_peak["frequency_mhz"])
        )
        <= merge_frequency_mhz
    )
    if same_burst:
        if float(peak_sample["power_db"]) > float(previous_peak["power_db"]):
            recent_peaks[0] = peak_sample
        return

    recent_peaks.appendleft(peak_sample)


def recent_peaks_payload(
    recent_peaks: deque[dict[str, object]],
    now: dt.datetime,
) -> list[dict[str, object]]:
    return [
        {
            **peak,
            "timestamp": peak["timestamp"].isoformat(timespec="seconds"),
            "age_seconds": (now - peak["timestamp"]).total_seconds(),
        }
        for peak in list(recent_peaks)
    ]


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


def cluster_candidate_bins(
    readings: list[dict[str, float | int]],
    cluster_hz: float,
) -> list[dict[str, object]]:
    if not readings:
        return []

    sorted_readings = sorted(readings, key=lambda item: int(item["frequency_hz"]))
    clusters: list[list[dict[str, float | int]]] = []

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
        center = sum(float(item["frequency_mhz"]) for item in cluster) / len(cluster)
        low = int(cluster[0]["frequency_hz"]) / 1_000_000
        high = int(cluster[-1]["frequency_hz"]) / 1_000_000
        grouped.append(
            {
                "center_frequency_mhz": center,
                "frequency_mhz": float(strongest["frequency_mhz"]),
                "cluster_low_mhz": low,
                "cluster_high_mhz": high,
                "cluster_width_khz": max(0.0, (high - low) * 1000),
                "bin_count": len(cluster),
                "power_db": float(strongest["power_db"]),
                "baseline_db": float(strongest["baseline_db"]),
                "delta_db": max(float(item["delta_db"]) for item in cluster),
            }
        )

    grouped.sort(key=lambda item: float(item["power_db"]), reverse=True)
    return grouped


def update_cluster_tracks(
    *,
    tracks: dict[int, dict[str, object]],
    candidate_clusters: list[dict[str, object]],
    now: dt.datetime,
    args: argparse.Namespace,
    next_track_id: int,
    cluster_writer: csv.DictWriter,
    cluster_fp,
    strongest_incidents: list[dict[str, object]],
) -> tuple[int, list[dict[str, object]], list[dict[str, object]]]:
    matched_track_ids: set[int] = set()
    active_clusters: list[dict[str, object]] = []
    cluster_hz = args.cluster_khz * 1_000

    for cluster in candidate_clusters:
        best_id: int | None = None
        best_distance_hz = float("inf")
        cluster_center_hz = float(cluster["center_frequency_mhz"]) * 1_000_000

        for track_id, track in tracks.items():
            if track_id in matched_track_ids:
                continue
            distance_hz = abs(cluster_center_hz - float(track["center_hz"]))
            if distance_hz <= cluster_hz and distance_hz < best_distance_hz:
                best_id = track_id
                best_distance_hz = distance_hz

        if best_id is None:
            best_id = next_track_id
            next_track_id += 1
            tracks[best_id] = {
                "id": best_id,
                "start": now,
                "last_seen": now,
                "center_hz": cluster_center_hz,
                "candidate_count": 0,
                "below_count": 0,
                "confirmed": False,
                "peak_power": float(cluster["power_db"]),
                "peak_delta": float(cluster["delta_db"]),
                "peak_frequency_mhz": float(cluster["frequency_mhz"]),
            }

        track = tracks[best_id]
        matched_track_ids.add(best_id)
        track["last_seen"] = now
        track["center_hz"] = (float(track["center_hz"]) + cluster_center_hz) / 2
        track["candidate_count"] = int(track["candidate_count"]) + 1
        track["below_count"] = 0

        if float(cluster["power_db"]) > float(track["peak_power"]):
            track["peak_power"] = float(cluster["power_db"])
            track["peak_frequency_mhz"] = float(cluster["frequency_mhz"])
        track["peak_delta"] = max(float(track["peak_delta"]), float(cluster["delta_db"]))

        if not bool(track["confirmed"]) and int(track["candidate_count"]) >= args.min_incident_samples:
            track["confirmed"] = True
            write_cluster_activity(
                cluster_writer,
                event="start",
                timestamp=now,
                cluster=cluster,
                track=track,
                threshold_db=args.threshold_db,
                incident_min_power_db=args.incident_min_power_db,
            )
            cluster_fp.flush()

        if bool(track["confirmed"]):
            write_cluster_activity(
                cluster_writer,
                event="active",
                timestamp=now,
                cluster=cluster,
                track=track,
                threshold_db=args.threshold_db,
                incident_min_power_db=args.incident_min_power_db,
            )
            cluster_fp.flush()
            active_clusters.append(
                {
                    **cluster,
                    "started_at": track["start"].isoformat(timespec="seconds"),
                    "duration_seconds": (now - track["start"]).total_seconds(),
                    "peak_power_db": float(track["peak_power"]),
                    "peak_delta_db": float(track["peak_delta"]),
                    "track_id": best_id,
                }
            )

    ended: list[int] = []
    for track_id, track in tracks.items():
        if track_id in matched_track_ids:
            continue
        track["below_count"] = int(track["below_count"]) + 1
        if int(track["below_count"]) >= args.hold_samples:
            if bool(track["confirmed"]):
                fallback_cluster = {
                    "center_frequency_mhz": float(track["center_hz"]) / 1_000_000,
                    "frequency_mhz": float(track["peak_frequency_mhz"]),
                    "cluster_low_mhz": float(track["center_hz"]) / 1_000_000,
                    "cluster_high_mhz": float(track["center_hz"]) / 1_000_000,
                    "cluster_width_khz": 0.0,
                    "bin_count": 0,
                    "power_db": float("nan"),
                    "baseline_db": float("nan"),
                    "delta_db": float("nan"),
                }
                write_cluster_activity(
                    cluster_writer,
                    event="end",
                    timestamp=now,
                    cluster=fallback_cluster,
                    track=track,
                    threshold_db=args.threshold_db,
                    incident_min_power_db=args.incident_min_power_db,
                )
                cluster_fp.flush()
                strongest_incidents.append(
                    {
                        "time": now.strftime("%H:%M:%S"),
                        "timestamp": now.isoformat(timespec="seconds"),
                        "frequency_mhz": float(track["peak_frequency_mhz"]),
                        "duration_seconds": (now - track["start"]).total_seconds(),
                        "peak_power_db": float(track["peak_power"]),
                        "peak_delta_db": float(track["peak_delta"]),
                    }
                )
            ended.append(track_id)

    for track_id in ended:
        del tracks[track_id]

    strongest_incidents = sorted(
        strongest_incidents,
        key=lambda item: float(item["peak_power_db"]),
        reverse=True,
    )[:5]
    active_clusters.sort(key=lambda item: float(item["power_db"]), reverse=True)
    return next_track_id, active_clusters, strongest_incidents


def monitor(args: argparse.Namespace) -> int:
    if not args.demo:
        require_rtl_power()

    baselines: dict[int, deque[float]] = defaultdict(
        lambda: deque(maxlen=args.baseline_samples)
    )
    incidents: dict[int, dict[str, object]] = {}
    sample_count = 0
    activity_fp, activity_writer = open_activity_log(Path(args.activity_log))
    cluster_activity_path = Path(args.activity_log).with_name(
        f"{Path(args.activity_log).stem}_clusters.csv"
    )
    cluster_activity_fp, cluster_activity_writer = open_cluster_activity_log(
        cluster_activity_path
    )
    readings_fp, readings_writer = open_readings_log(Path(args.readings_log))
    observations_fp, observations_writer = open_observations_log(
        Path(args.observations_log)
    )
    recent_events: deque[dict[str, object]] = deque(maxlen=20)
    recent_observations: deque[dict[str, object]] = deque(maxlen=20)
    series: deque[dict[str, object]] = deque(maxlen=180)
    strongest_incidents: list[dict[str, object]] = []
    cluster_tracks: dict[int, dict[str, object]] = {}
    next_cluster_track_id = 1
    recent_peak: dict[str, object] | None = None
    recent_peaks: deque[dict[str, object]] = deque(maxlen=5)
    latest_top_reading: dict[str, object] | None = None
    pending_tune: dict[str, str | None] = {"name": None}
    pending_tune_lock = threading.Lock()
    active_vehicle_interval: dict[str, object] | None = None

    def mark_observation(label: str) -> dict[str, object]:
        nonlocal latest_top_reading, recent_peak, sample_count, active_vehicle_interval
        now = dt.datetime.now().astimezone()
        safe_label = sanitize_label(label)
        current = latest_top_reading or {}
        peak = recent_peak or {}
        event_type = "point"
        interval_id = ""
        interval_start = ""
        interval_end = ""
        duration_seconds = ""
        display_label = safe_label

        if safe_label == "vehicle_in_sight":
            if active_vehicle_interval is None:
                interval_id = now.strftime("%Y%m%d_%H%M%S")
                interval_start = now.isoformat(timespec="seconds")
                active_vehicle_interval = {
                    "id": interval_id,
                    "start": now,
                    "label": "vehicle_nearby",
                }
                event_type = "interval_start"
                display_label = "vehicle_nearby"
            else:
                interval_id = str(active_vehicle_interval["id"])
                start = active_vehicle_interval["start"]
                interval_start = start.isoformat(timespec="seconds")
                interval_end = now.isoformat(timespec="seconds")
                duration_seconds = f"{(now - start).total_seconds():.0f}"
                event_type = "interval_end"
                display_label = str(active_vehicle_interval["label"])
                active_vehicle_interval = None

        marker = {
            "timestamp": now.isoformat(timespec="seconds"),
            "label": display_label,
            "event_type": event_type,
            "interval_id": interval_id,
            "interval_start": interval_start,
            "interval_end": interval_end,
            "duration_seconds": duration_seconds,
            "note": "",
            "range": args.range,
            "sample": sample_count,
            "current_frequency_mhz": current.get("frequency_mhz", ""),
            "current_power_db": current.get("power_db", ""),
            "current_baseline_db": current.get("baseline_db", ""),
            "current_delta_db": current.get("delta_db", ""),
            "recent_peak_frequency_mhz": peak.get("frequency_mhz", ""),
            "recent_peak_power_db": peak.get("power_db", ""),
            "recent_peak_delta_db": peak.get("delta_db", ""),
        }
        observations_writer.writerow(marker)
        observations_fp.flush()
        recent_observations.appendleft(marker)
        dashboard_state.update(
            recent_observations=list(recent_observations),
            vehicle_interval_active=active_vehicle_interval is not None,
            vehicle_interval_started_at=(
                active_vehicle_interval["start"].isoformat(timespec="seconds")
                if active_vehicle_interval is not None
                else None
            ),
        )
        print(f"MARK {marker['timestamp']} {display_label} {event_type}")
        return marker

    def select_tune(tune_name: str) -> dict[str, object]:
        if tune_name not in TUNE_PROFILES:
            return {"ok": False, "error": f"unknown tune: {tune_name}"}
        with pending_tune_lock:
            pending_tune["name"] = tune_name
        profile = TUNE_PROFILES[tune_name]
        dashboard_state.update(
            message=f"Switching to {profile['label']}",
            pending_tune=tune_name,
        )
        return {
            "ok": True,
            "tune": tune_name,
            "tune_label": profile["label"],
        }

    def load_analysis() -> dict[str, object]:
        try:
            return load_drive_analysis(
                Path(args.readings_log),
                Path(args.observations_log),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    dashboard_state = DashboardState()
    dashboard_state.update(
        threshold_db=args.threshold_db,
        incident_min_power_db=args.incident_min_power_db,
        warmup_samples=args.warmup_samples,
        range=args.range,
        absolute_strong_db=args.absolute_strong_db,
        absolute_extreme_db=args.absolute_extreme_db,
        cluster_khz=args.cluster_khz,
        tuning=tuning_payload(args),
        demo=args.demo,
        vehicle_interval_active=False,
        vehicle_interval_started_at=None,
        label_prompt="",
    )

    dashboard_server = None
    if args.dashboard:
        dashboard_server = start_dashboard(
            dashboard_state,
            args.dashboard_host,
            args.dashboard_port,
            mark_observation,
            select_tune,
            load_analysis,
        )
        print(f"Dashboard: http://{args.dashboard_host}:{args.dashboard_port}")

    with tempfile.NamedTemporaryFile(prefix="rf_power_", suffix=".csv", delete=False) as fp:
        output_path = Path(fp.name)

    if args.demo:
        print(f"Demo mode: simulating {args.range}; no SDR hardware is required.")
    else:
        print(f"Monitoring {args.range}; writing temporary rtl_power CSV to {output_path}")
    print(f"Session: {args.session_id}")
    print(f"Session directory: {Path(args.session_dir).resolve()}")
    print(f"Recording incidents to {Path(args.activity_log).resolve()}")
    print(f"Recording cluster incidents to {cluster_activity_path.resolve()}")
    print(f"Recording strongest readings to {Path(args.readings_log).resolve()}")
    print(f"Recording field markers to {Path(args.observations_log).resolve()}")
    print("Press Ctrl-C to stop.")

    process = None if args.demo else start_rtl_power(args, output_path)
    last_size = 0

    try:
        while args.demo or process.poll() is None:
            time.sleep(1)
            with pending_tune_lock:
                tune_to_apply = pending_tune["name"]
                pending_tune["name"] = None

            if tune_to_apply:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                try:
                    os.unlink(output_path)
                except FileNotFoundError:
                    pass

                apply_tune(args, tune_to_apply)
                baselines.clear()
                incidents.clear()
                cluster_tracks.clear()
                next_cluster_track_id = 1
                sample_count = 0
                recent_peak = None
                recent_peaks.clear()
                latest_top_reading = None
                series.clear()
                strongest_incidents.clear()
                recent_events.clear()

                with tempfile.NamedTemporaryFile(
                    prefix="rf_power_", suffix=".csv", delete=False
                ) as fp:
                    output_path = Path(fp.name)
                process = None if args.demo else start_rtl_power(args, output_path)
                last_size = 0
                dashboard_state.update(
                    threshold_db=args.threshold_db,
                    incident_min_power_db=args.incident_min_power_db,
                    warmup_samples=args.warmup_samples,
                    range=args.range,
                    absolute_strong_db=args.absolute_strong_db,
                    absolute_extreme_db=args.absolute_extreme_db,
                    cluster_khz=args.cluster_khz,
                    tuning=tuning_payload(args),
                    pending_tune=None,
                    sample_count=0,
                    strongest=[],
                    active_incidents=[],
                    active_bins=[],
                    recent_events=[],
                    strongest_incidents=[],
                    series=[],
                    recent_peak=None,
                    recent_peaks=[],
                    status="warming",
                    message="Building baseline",
                    demo=args.demo,
                    label_prompt="",
                )
                print(
                    f"Switched tune to {TUNE_PROFILES[tune_to_apply]['label']} "
                    f"({tune_to_apply}); monitoring {args.range}"
                )
                continue

            if args.demo:
                rows = [demo_power_row(args, sample_count)]
            else:
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
                candidate_bins: list[dict[str, float | int]] = []

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
                    if is_high:
                        candidate_bins.append(
                            reading_payload(freq_hz, power_db, baseline, delta)
                        )
                    history.append(power_db)
                    continue
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
                            ended_incident = {
                                "time": now.strftime("%H:%M:%S"),
                                "timestamp": now.isoformat(timespec="seconds"),
                                "frequency_mhz": freq_hz / 1_000_000,
                                "duration_seconds": (
                                    now - incident["start"]
                                ).total_seconds(),
                                "peak_power_db": float(incident["peak_power"]),
                                "peak_delta_db": float(incident["peak_delta"]),
                            }
                            strongest_incidents.append(ended_incident)
                            strongest_incidents = sorted(
                                strongest_incidents,
                                key=lambda item: float(item["peak_power_db"]),
                                reverse=True,
                            )[:5]
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
                if strongest:
                    top_reading = strongest[0]
                    latest_top_reading = {
                        "frequency_mhz": float(top_reading["frequency_mhz"]),
                        "power_db": float(top_reading["power_db"]),
                        "baseline_db": float(top_reading["baseline_db"]),
                        "delta_db": float(top_reading["delta_db"]),
                    }
                    if (
                        recent_peak is None
                        or float(top_reading["power_db"]) > float(recent_peak["power_db"])
                    ):
                        recent_peak = {
                            "timestamp": now,
                            "frequency_mhz": float(top_reading["frequency_mhz"]),
                            "power_db": float(top_reading["power_db"]),
                            "baseline_db": float(top_reading["baseline_db"]),
                            "delta_db": float(top_reading["delta_db"]),
                        }
                    is_peak_sample = (
                        sample_count > args.warmup_samples
                        and float(top_reading["power_db"]) >= -25.0
                    )
                    if is_peak_sample:
                        update_recent_peaks(
                            recent_peaks,
                            peak_payload(top_reading, now),
                        )
                    series.append(
                        {
                            "timestamp": now.strftime("%H:%M:%S"),
                            "frequency_mhz": float(top_reading["frequency_mhz"]),
                            "power_db": float(top_reading["power_db"]),
                            "baseline_db": float(top_reading["baseline_db"]),
                            "delta_db": float(top_reading["delta_db"]),
                        }
                    )
                candidate_freqs = {
                    int(reading["frequency_hz"]) for reading in candidate_bins
                }
                for rank, reading in enumerate(strongest[: args.log_top], start=1):
                    write_reading(
                        readings_writer,
                        timestamp=now,
                        sample_count=sample_count,
                        rank=rank,
                        reading=reading,
                        threshold_db=args.threshold_db,
                        incident_min_power_db=args.incident_min_power_db,
                        is_incident=int(reading["frequency_hz"]) in candidate_freqs,
                    )
                readings_fp.flush()
                candidate_clusters = cluster_candidate_bins(
                    candidate_bins,
                    args.cluster_khz * 1_000,
                )
                label_prompt = ""
                if (
                    sample_count > args.warmup_samples
                    and active_vehicle_interval is None
                    and latest_top_reading is not None
                    and (
                        float(latest_top_reading["power_db"]) >= -20.0
                        or float(latest_top_reading["delta_db"]) >= 20.0
                    )
                ):
                    label_prompt = "Label if vehicle is visible"
                (
                    next_cluster_track_id,
                    active_clusters,
                    strongest_incidents,
                ) = update_cluster_tracks(
                    tracks=cluster_tracks,
                    candidate_clusters=candidate_clusters,
                    now=now,
                    args=args,
                    next_track_id=next_cluster_track_id,
                    cluster_writer=cluster_activity_writer,
                    cluster_fp=cluster_activity_fp,
                    strongest_incidents=strongest_incidents,
                )
                dashboard_state.update(
                    updated_at=now.isoformat(timespec="seconds"),
                    sample_count=sample_count,
                    strongest=strongest,
                    active_incidents=active_clusters,
                    active_bins=candidate_bins,
                    recent_events=list(recent_events),
                    recent_observations=list(recent_observations),
                    strongest_incidents=strongest_incidents,
                    recent_peaks=recent_peaks_payload(recent_peaks, now),
                    label_prompt=label_prompt,
                    series=list(series),
                    recent_peak={
                        **recent_peak,
                        "timestamp": recent_peak["timestamp"].isoformat(timespec="seconds"),
                        "age_seconds": (now - recent_peak["timestamp"]).total_seconds(),
                    }
                    if recent_peak is not None
                    else None,
                    status="active" if active_clusters else "normal",
                    message="Active incident" if active_clusters else "Monitoring",
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
        cluster_activity_fp.flush()
        cluster_activity_fp.close()
        readings_fp.flush()
        readings_fp.close()
        observations_fp.flush()
        observations_fp.close()
        if process is not None and process.poll() is None:
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
