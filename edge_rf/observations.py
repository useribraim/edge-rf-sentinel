"""Observation marker state for labelled RF monitoring sessions."""

from __future__ import annotations

import datetime as dt

from edge_rf.csv_logs import sanitize_label


class ObservationTracker:
    def __init__(self) -> None:
        self.active_vehicle_interval: dict[str, object] | None = None

    def mark(
        self,
        label: str,
        *,
        signal_range: str,
        sample_count: int,
        current_reading: dict[str, object] | None,
        recent_peak: dict[str, object] | None,
        now: dt.datetime | None = None,
    ) -> dict[str, object]:
        now = now or dt.datetime.now().astimezone()
        safe_label = sanitize_label(label)
        current = current_reading or {}
        peak = recent_peak or {}
        event_type = "point"
        interval_id = ""
        interval_start = ""
        interval_end = ""
        duration_seconds = ""
        display_label = safe_label

        if safe_label == "vehicle_in_sight":
            if self.active_vehicle_interval is None:
                interval_id = now.strftime("%Y%m%d_%H%M%S")
                interval_start = now.isoformat(timespec="seconds")
                self.active_vehicle_interval = {
                    "id": interval_id,
                    "start": now,
                    "label": "vehicle_nearby",
                }
                event_type = "interval_start"
                display_label = "vehicle_nearby"
            else:
                interval_id = str(self.active_vehicle_interval["id"])
                start = self.active_vehicle_interval["start"]
                interval_start = start.isoformat(timespec="seconds")
                interval_end = now.isoformat(timespec="seconds")
                duration_seconds = f"{(now - start).total_seconds():.0f}"
                event_type = "interval_end"
                display_label = str(self.active_vehicle_interval["label"])
                self.active_vehicle_interval = None

        return {
            "timestamp": now.isoformat(timespec="seconds"),
            "label": display_label,
            "event_type": event_type,
            "interval_id": interval_id,
            "interval_start": interval_start,
            "interval_end": interval_end,
            "duration_seconds": duration_seconds,
            "note": "",
            "range": signal_range,
            "sample": sample_count,
            "current_frequency_mhz": current.get("frequency_mhz", ""),
            "current_power_db": current.get("power_db", ""),
            "current_baseline_db": current.get("baseline_db", ""),
            "current_delta_db": current.get("delta_db", ""),
            "recent_peak_frequency_mhz": peak.get("frequency_mhz", ""),
            "recent_peak_power_db": peak.get("power_db", ""),
            "recent_peak_delta_db": peak.get("delta_db", ""),
        }

    def vehicle_interval_active(self) -> bool:
        return self.active_vehicle_interval is not None

    def vehicle_interval_started_at(self) -> str | None:
        if self.active_vehicle_interval is None:
            return None
        start = self.active_vehicle_interval["start"]
        return start.isoformat(timespec="seconds")
