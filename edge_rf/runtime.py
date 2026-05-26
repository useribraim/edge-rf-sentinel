"""Runtime state and row-processing helpers for the RF monitor."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field

from edge_rf.csv_logs import write_reading
from edge_rf.detection import (
    ClusterTrack,
    ReadingPayload,
    peak_payload,
    reading_payload,
    recent_peaks_payload,
    update_recent_peaks,
)
from edge_rf.observations import ObservationTracker


@dataclass
class MonitorRuntimeState:
    baseline_samples: int
    baselines: dict[int, deque[float]] = field(init=False)
    sample_count: int = 0
    cluster_tracks: dict[int, ClusterTrack] = field(default_factory=dict)
    next_cluster_track_id: int = 1
    recent_peak: dict[str, object] | None = None
    recent_peaks: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=5)
    )
    latest_top_reading: dict[str, object] | None = None
    series: deque[dict[str, object]] = field(default_factory=lambda: deque(maxlen=180))
    strongest_incidents: list[dict[str, object]] = field(default_factory=list)
    recent_observations: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=20)
    )
    observation_tracker: ObservationTracker = field(default_factory=ObservationTracker)

    def __post_init__(self) -> None:
        self.baselines = defaultdict(lambda: deque(maxlen=self.baseline_samples))

    def reset_for_tune(self) -> None:
        self.baselines.clear()
        self.cluster_tracks.clear()
        self.next_cluster_track_id = 1
        self.sample_count = 0
        self.recent_peak = None
        self.recent_peaks.clear()
        self.latest_top_reading = None
        self.series.clear()
        self.strongest_incidents.clear()


def build_readings(
    *,
    bins: list[tuple[int, float]],
    baselines: dict[int, deque[float]],
    sample_count: int,
    args: argparse.Namespace,
) -> tuple[list[ReadingPayload], list[ReadingPayload]]:
    enriched: list[ReadingPayload] = []
    candidate_bins: list[ReadingPayload] = []
    is_warm = sample_count > args.warmup_samples

    for freq_hz, power_db in bins:
        history = baselines[freq_hz]
        baseline = statistics.median(history) if history else power_db
        delta = power_db - baseline
        reading = reading_payload(freq_hz, power_db, baseline, delta)
        enriched.append(reading)
        if (
            is_warm
            and delta >= args.threshold_db
            and power_db >= args.incident_min_power_db
        ):
            candidate_bins.append(reading)
        history.append(power_db)

    return enriched, candidate_bins


def update_signal_summary(
    *,
    strongest: list[ReadingPayload],
    now: dt.datetime,
    sample_count: int,
    warmup_samples: int,
    recent_peak: dict[str, object] | None,
    recent_peaks: deque[dict[str, object]],
    series: deque[dict[str, object]],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if not strongest:
        return None, recent_peak

    top_reading = strongest[0]
    latest_top_reading = {
        "frequency_mhz": float(top_reading["frequency_mhz"]),
        "power_db": float(top_reading["power_db"]),
        "baseline_db": float(top_reading["baseline_db"]),
        "delta_db": float(top_reading["delta_db"]),
    }
    if recent_peak is None or float(top_reading["power_db"]) > float(
        recent_peak["power_db"]
    ):
        recent_peak = {
            "timestamp": now,
            **latest_top_reading,
        }
    if sample_count > warmup_samples and float(top_reading["power_db"]) >= -25.0:
        update_recent_peaks(recent_peaks, peak_payload(top_reading, now))
    series.append(
        {
            "timestamp": now.strftime("%H:%M:%S"),
            **latest_top_reading,
        }
    )
    return latest_top_reading, recent_peak


def write_top_readings(
    *,
    readings_writer: csv.DictWriter,
    readings_fp,
    strongest: list[ReadingPayload],
    candidate_bins: list[ReadingPayload],
    now: dt.datetime,
    sample_count: int,
    args: argparse.Namespace,
) -> None:
    candidate_freqs = {reading["frequency_hz"] for reading in candidate_bins}
    for rank, reading in enumerate(strongest[: args.log_top], start=1):
        write_reading(
            readings_writer,
            timestamp=now,
            sample_count=sample_count,
            rank=rank,
            reading=reading,
            threshold_db=args.threshold_db,
            incident_min_power_db=args.incident_min_power_db,
            is_incident=reading["frequency_hz"] in candidate_freqs,
        )
    readings_fp.flush()


def label_prompt_for(
    *,
    sample_count: int,
    args: argparse.Namespace,
    observation_tracker: ObservationTracker,
    latest_top_reading: dict[str, object] | None,
) -> str:
    if (
        sample_count > args.warmup_samples
        and not observation_tracker.vehicle_interval_active()
        and latest_top_reading is not None
        and (
            float(latest_top_reading["power_db"]) >= -20.0
            or float(latest_top_reading["delta_db"]) >= 20.0
        )
    ):
        return "Label if vehicle is visible"
    return ""


def dashboard_payload(
    *,
    now: dt.datetime,
    sample_count: int,
    strongest: list[ReadingPayload],
    active_clusters: list[dict[str, object]],
    candidate_bins: list[ReadingPayload],
    recent_observations: deque[dict[str, object]],
    strongest_incidents: list[dict[str, object]],
    recent_peaks: deque[dict[str, object]],
    label_prompt: str,
    series: deque[dict[str, object]],
    recent_peak: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "updated_at": now.isoformat(timespec="seconds"),
        "sample_count": sample_count,
        "strongest": strongest,
        "active_incidents": active_clusters,
        "active_bins": candidate_bins,
        "recent_events": [],
        "recent_observations": list(recent_observations),
        "strongest_incidents": strongest_incidents,
        "recent_peaks": recent_peaks_payload(recent_peaks, now),
        "label_prompt": label_prompt,
        "series": list(series),
        "recent_peak": {
            **recent_peak,
            "timestamp": recent_peak["timestamp"].isoformat(timespec="seconds"),
            "age_seconds": (now - recent_peak["timestamp"]).total_seconds(),
        }
        if recent_peak is not None
        else None,
        "status": "active" if active_clusters else "normal",
        "message": "Active incident" if active_clusters else "Monitoring",
    }
