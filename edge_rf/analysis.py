from __future__ import annotations

import csv
import datetime as dt
import statistics
from collections import defaultdict
from pathlib import Path


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


def parse_rank1_row(row: dict[str, str]) -> dict[str, object] | None:
    if row.get("rank") != "1":
        return None
    try:
        parsed = dict(row)
        parsed["dt"] = dt.datetime.fromisoformat(row["timestamp"])
        parsed["sample"] = int(row["sample"])
        parsed["power_db"] = float(row["power_db"])
        parsed["baseline_db"] = float(row["baseline_db"])
        parsed["delta_db"] = float(row["delta_db"])
        parsed["frequency_mhz"] = float(row["frequency_mhz"])
    except (KeyError, TypeError, ValueError):
        return None
    return parsed


def parse_observation_row(row: dict[str, str]) -> dict[str, object] | None:
    if not row.get("timestamp"):
        return None
    try:
        parsed = dict(row)
        parsed["dt"] = dt.datetime.fromisoformat(row["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    return parsed


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
            parsed = parse_rank1_row(row)
            if parsed is not None:
                rank1.append(parsed)

    if not rank1:
        return {"ok": True, "empty": True, "message": "No readings yet"}

    observations: list[dict[str, object]] = []
    if observations_path.exists():
        with observations_path.open(newline="") as fp:
            for row in csv.DictReader(fp):
                parsed = parse_observation_row(row)
                if parsed is not None:
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
