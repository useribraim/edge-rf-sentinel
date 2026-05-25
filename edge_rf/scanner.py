"""SDR scanner process and rtl_power row parsing helpers."""

from __future__ import annotations

import argparse
import datetime as dt
import math
import random
import subprocess
import sys
from pathlib import Path


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

    try:
        start_hz = float(row[2])
        stop_hz = float(row[3])
        step_hz = float(row[4])
        powers = [float(value) for value in row[6:] if value]
    except ValueError:
        return []

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
    try:
        start, stop, step = range_spec.split(":", 2)
        return (
            round(parse_frequency_value(start)),
            round(parse_frequency_value(stop)),
            round(parse_frequency_value(step)),
        )
    except ValueError as exc:
        raise ValueError(f"invalid range spec: {range_spec}") from exc


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
