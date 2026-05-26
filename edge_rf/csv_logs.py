"""CSV log writers for RF signal monitoring sessions."""

from __future__ import annotations

import csv
from pathlib import Path


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
