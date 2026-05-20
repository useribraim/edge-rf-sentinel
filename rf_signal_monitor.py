#!/usr/bin/env python3
"""Record SDR signal-strength incidents with timestamps."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from edge_rf.analysis import load_drive_analysis
from edge_rf.csv_logs import (
    open_activity_log,
    open_cluster_activity_log,
    open_observations_log,
    open_readings_log,
    sanitize_label,
    write_activity,
    write_cluster_activity,
    write_reading,
)
from edge_rf.detection import (
    cluster_candidate_bins,
    cluster_readings,
    peak_payload,
    reading_payload,
    recent_peaks_payload,
    update_cluster_tracks,
    update_recent_peaks,
)
from edge_rf.scanner import (
    demo_power_row,
    format_freq,
    parse_power_row,
    require_rtl_power,
    start_rtl_power,
)


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
        help="CSV file for timestamped incidents. Default: <session-dir>/activity.csv",
    )
    parser.add_argument(
        "--readings-log",
        default=None,
        help="CSV file for strongest readings each scan row. Default: <session-dir>/readings.csv",
    )
    parser.add_argument(
        "--observations-log",
        default=None,
        help="CSV file for manual field markers. Default: <session-dir>/observations.csv",
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
