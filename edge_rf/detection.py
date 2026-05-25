"""Signal payload, peak, and cluster tracking helpers."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from collections import deque
from typing import TypedDict

from edge_rf.csv_logs import write_cluster_activity


class ReadingPayload(TypedDict):
    frequency_hz: int
    frequency_mhz: float
    power_db: float
    baseline_db: float
    delta_db: float


class CandidateCluster(TypedDict):
    center_frequency_mhz: float
    frequency_mhz: float
    cluster_low_mhz: float
    cluster_high_mhz: float
    cluster_width_khz: float
    bin_count: int
    power_db: float
    baseline_db: float
    delta_db: float


class ClusterTrack(TypedDict):
    id: int
    start: dt.datetime
    last_seen: dt.datetime
    center_hz: float
    candidate_count: int
    below_count: int
    confirmed: bool
    peak_power: float
    peak_delta: float
    peak_frequency_mhz: float


class ClusterActivityRecord(TypedDict):
    event: str
    timestamp: dt.datetime
    cluster: dict[str, object]
    track: ClusterTrack


def reading_payload(
    freq_hz: int,
    power_db: float,
    baseline_db: float,
    delta_db: float,
) -> ReadingPayload:
    return {
        "frequency_hz": freq_hz,
        "frequency_mhz": freq_hz / 1_000_000,
        "power_db": power_db,
        "baseline_db": baseline_db,
        "delta_db": delta_db,
    }


def peak_payload(
    reading: ReadingPayload,
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


def group_by_frequency_gap(
    readings: list[dict[str, object]],
    cluster_hz: float,
) -> list[list[dict[str, object]]]:
    clusters: list[list[dict[str, object]]] = []
    for reading in sorted(readings, key=lambda item: int(item["frequency_hz"])):
        if not clusters:
            clusters.append([reading])
            continue

        previous = clusters[-1][-1]
        if int(reading["frequency_hz"]) - int(previous["frequency_hz"]) <= cluster_hz:
            clusters[-1].append(reading)
        else:
            clusters.append([reading])
    return clusters


def cluster_readings(
    readings: list[dict[str, object]],
    cluster_hz: float,
) -> list[dict[str, object]]:
    if not readings:
        return []

    grouped: list[dict[str, object]] = []
    for cluster in group_by_frequency_gap(readings, cluster_hz):
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
    readings: list[ReadingPayload],
    cluster_hz: float,
) -> list[CandidateCluster]:
    if not readings:
        return []

    grouped: list[CandidateCluster] = []
    for cluster in group_by_frequency_gap(readings, cluster_hz):
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


def find_matching_track_id(
    *,
    tracks: dict[int, ClusterTrack],
    matched_track_ids: set[int],
    cluster_center_hz: float,
    cluster_hz: float,
) -> int | None:
    best_id: int | None = None
    best_distance_hz = float("inf")

    for track_id, track in tracks.items():
        if track_id in matched_track_ids:
            continue
        distance_hz = abs(cluster_center_hz - float(track["center_hz"]))
        if distance_hz <= cluster_hz and distance_hz < best_distance_hz:
            best_id = track_id
            best_distance_hz = distance_hz

    return best_id


def new_cluster_track(
    *,
    track_id: int,
    cluster: CandidateCluster,
    cluster_center_hz: float,
    now: dt.datetime,
) -> ClusterTrack:
    return ClusterTrack(
        id=track_id,
        start=now,
        last_seen=now,
        center_hz=cluster_center_hz,
        candidate_count=0,
        below_count=0,
        confirmed=False,
        peak_power=float(cluster["power_db"]),
        peak_delta=float(cluster["delta_db"]),
        peak_frequency_mhz=float(cluster["frequency_mhz"]),
    )


def update_track_from_cluster(
    track: ClusterTrack,
    cluster: CandidateCluster,
    cluster_center_hz: float,
    now: dt.datetime,
) -> None:
    track["last_seen"] = now
    track["center_hz"] = (float(track["center_hz"]) + cluster_center_hz) / 2
    track["candidate_count"] = int(track["candidate_count"]) + 1
    track["below_count"] = 0

    if float(cluster["power_db"]) > float(track["peak_power"]):
        track["peak_power"] = float(cluster["power_db"])
        track["peak_frequency_mhz"] = float(cluster["frequency_mhz"])
    track["peak_delta"] = max(float(track["peak_delta"]), float(cluster["delta_db"]))


def active_cluster_payload(
    *,
    cluster: CandidateCluster,
    track: ClusterTrack,
    track_id: int,
    now: dt.datetime,
) -> dict[str, object]:
    return {
        **cluster,
        "started_at": track["start"].isoformat(timespec="seconds"),
        "duration_seconds": (now - track["start"]).total_seconds(),
        "peak_power_db": float(track["peak_power"]),
        "peak_delta_db": float(track["peak_delta"]),
        "track_id": track_id,
    }


def fallback_cluster_for_track(track: ClusterTrack) -> dict[str, object]:
    center_mhz = float(track["center_hz"]) / 1_000_000
    return {
        "center_frequency_mhz": center_mhz,
        "frequency_mhz": float(track["peak_frequency_mhz"]),
        "cluster_low_mhz": center_mhz,
        "cluster_high_mhz": center_mhz,
        "cluster_width_khz": 0.0,
        "bin_count": 0,
        "power_db": float("nan"),
        "baseline_db": float("nan"),
        "delta_db": float("nan"),
    }


def strongest_incident_payload(
    *,
    track: ClusterTrack,
    now: dt.datetime,
) -> dict[str, object]:
    return {
        "time": now.strftime("%H:%M:%S"),
        "timestamp": now.isoformat(timespec="seconds"),
        "frequency_mhz": float(track["peak_frequency_mhz"]),
        "duration_seconds": (now - track["start"]).total_seconds(),
        "peak_power_db": float(track["peak_power"]),
        "peak_delta_db": float(track["peak_delta"]),
    }


def write_cluster_record(
    *,
    record: ClusterActivityRecord,
    cluster_writer: csv.DictWriter,
    cluster_fp,
    threshold_db: float,
    incident_min_power_db: float,
) -> None:
    write_cluster_activity(
        cluster_writer,
        event=record["event"],
        timestamp=record["timestamp"],
        cluster=record["cluster"],
        track=record["track"],
        threshold_db=threshold_db,
        incident_min_power_db=incident_min_power_db,
    )
    cluster_fp.flush()


def update_cluster_tracks(
    *,
    tracks: dict[int, ClusterTrack],
    candidate_clusters: list[CandidateCluster],
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
        cluster_center_hz = float(cluster["center_frequency_mhz"]) * 1_000_000
        best_id = find_matching_track_id(
            tracks=tracks,
            matched_track_ids=matched_track_ids,
            cluster_center_hz=cluster_center_hz,
            cluster_hz=cluster_hz,
        )

        if best_id is None:
            best_id = next_track_id
            next_track_id += 1
            tracks[best_id] = new_cluster_track(
                track_id=best_id,
                cluster=cluster,
                cluster_center_hz=cluster_center_hz,
                now=now,
            )

        track = tracks[best_id]
        matched_track_ids.add(best_id)
        update_track_from_cluster(track, cluster, cluster_center_hz, now)

        if not bool(track["confirmed"]) and int(track["candidate_count"]) >= args.min_incident_samples:
            track["confirmed"] = True
            write_cluster_record(
                record={
                    "event": "start",
                    "timestamp": now,
                    "cluster": cluster,
                    "track": track,
                },
                cluster_writer=cluster_writer,
                cluster_fp=cluster_fp,
                threshold_db=args.threshold_db,
                incident_min_power_db=args.incident_min_power_db,
            )

        if bool(track["confirmed"]):
            write_cluster_record(
                record={
                    "event": "active",
                    "timestamp": now,
                    "cluster": cluster,
                    "track": track,
                },
                cluster_writer=cluster_writer,
                cluster_fp=cluster_fp,
                threshold_db=args.threshold_db,
                incident_min_power_db=args.incident_min_power_db,
            )
            active_clusters.append(
                active_cluster_payload(
                    cluster=cluster,
                    track=track,
                    track_id=best_id,
                    now=now,
                )
            )

    ended: list[int] = []
    for track_id, track in tracks.items():
        if track_id in matched_track_ids:
            continue
        track["below_count"] = int(track["below_count"]) + 1
        if int(track["below_count"]) >= args.hold_samples:
            if bool(track["confirmed"]):
                write_cluster_record(
                    record={
                        "event": "end",
                        "timestamp": now,
                        "cluster": fallback_cluster_for_track(track),
                        "track": track,
                    },
                    cluster_writer=cluster_writer,
                    cluster_fp=cluster_fp,
                    threshold_db=args.threshold_db,
                    incident_min_power_db=args.incident_min_power_db,
                )
                strongest_incidents.append(
                    strongest_incident_payload(track=track, now=now)
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
