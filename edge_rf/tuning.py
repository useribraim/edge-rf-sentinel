"""Tune profiles and dashboard payloads for RF monitoring sensitivity."""

from __future__ import annotations

import argparse


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
