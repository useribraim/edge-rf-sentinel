"""Runtime defaults and session path helpers for the RF monitor."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from edge_rf.tuning import tune_values


DEFAULT_CONFIG: dict[str, object] = {
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

PRESET_CONFIGS: dict[str, dict[str, object]] = {
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


def apply_preset_defaults(args: argparse.Namespace) -> argparse.Namespace:
    selected = dict(PRESET_CONFIGS.get(args.preset or "", DEFAULT_CONFIG))
    if args.tune:
        selected.update(tune_values(args.tune))

    for key, fallback in DEFAULT_CONFIG.items():
        if getattr(args, key) is None:
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
    session_dir = (
        Path(args.session_dir)
        if args.session_dir
        else Path("logs/sessions") / args.session_id
    )
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
