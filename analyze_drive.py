#!/usr/bin/env python3
"""Summarize RF readings around manual field-observation markers."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare RF readings around manually recorded observation markers."
    )
    parser.add_argument(
        "--readings",
        default="logs/rf_mobile_readings.csv",
        help="Readings CSV. Default: logs/rf_mobile_readings.csv",
    )
    parser.add_argument(
        "--observations",
        default="logs/rf_mobile_observations.csv",
        help="Observations CSV. Default: logs/rf_mobile_observations.csv",
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=30,
        help="Seconds before/after each marker to inspect. Default: 30",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="Show this many strongest readings per marker. Default: 8",
    )
    return parser.parse_args()


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"Missing file: {path}")
    with path.open(newline="") as fp:
        return list(csv.DictReader(fp))


def main() -> int:
    args = parse_args()
    readings = load_csv(Path(args.readings))
    observations = load_csv(Path(args.observations))

    if not observations:
        print("No observation markers recorded.")
        return 0

    parsed_readings = []
    for row in readings:
        try:
            parsed_readings.append((parse_time(row["timestamp"]), row))
        except (KeyError, ValueError):
            continue

    for marker in observations:
        try:
            marker_time = parse_time(marker["timestamp"])
        except (KeyError, ValueError):
            continue
        start = marker_time - dt.timedelta(seconds=args.window_seconds)
        end = marker_time + dt.timedelta(seconds=args.window_seconds)
        nearby = [row for timestamp, row in parsed_readings if start <= timestamp <= end]
        strongest = sorted(
            nearby,
            key=lambda row: float(row.get("power_db") or -999),
            reverse=True,
        )[: args.top]
        biggest_delta = sorted(
            nearby,
            key=lambda row: float(row.get("delta_db") or -999),
            reverse=True,
        )[: args.top]

        print()
        print(f"Marker: {marker['timestamp']}  label={marker.get('label', '')}")
        print(f"Window: +/- {args.window_seconds}s  readings={len(nearby)}")
        print("Strongest power:")
        for row in strongest:
            print(
                f"  {row['timestamp']}  {float(row['frequency_mhz']):.6f} MHz  "
                f"{float(row['power_db']):.2f} dB  "
                f"delta {float(row['delta_db']):+.2f} dB"
            )
        print("Largest delta:")
        for row in biggest_delta:
            print(
                f"  {row['timestamp']}  {float(row['frequency_mhz']):.6f} MHz  "
                f"{float(row['power_db']):.2f} dB  "
                f"delta {float(row['delta_db']):+.2f} dB"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
