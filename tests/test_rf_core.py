from __future__ import annotations

import csv
import datetime as dt
import tempfile
import unittest
from collections import deque
from pathlib import Path

from edge_rf.analysis import load_drive_analysis
from edge_rf.detection import (
    cluster_candidate_bins,
    reading_payload,
    update_recent_peaks,
)
from edge_rf.observations import ObservationTracker
from edge_rf.scanner import parse_power_row, parse_range_spec


READINGS_FIELDS = [
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
]


def write_readings_fixture(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=READINGS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_observations_fixture(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["timestamp", "event_type"]
    with path.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def valid_reading_row(**overrides: str) -> dict[str, str]:
    row = {
        "timestamp": "2026-05-25T12:00:00+00:00",
        "sample": "1",
        "rank": "1",
        "frequency_hz": "390000000",
        "frequency_mhz": "390.000000",
        "power_db": "-40.00",
        "baseline_db": "-42.00",
        "delta_db": "2.00",
        "threshold_db": "8.00",
        "incident_min_power_db": "-20.00",
        "is_incident": "0",
    }
    row.update(overrides)
    return row


class ObservationTrackerTests(unittest.TestCase):
    def test_vehicle_marker_toggles_interval(self) -> None:
        tracker = ObservationTracker()
        start = dt.datetime(2026, 5, 25, 12, 0, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(seconds=8)

        started = tracker.mark(
            "vehicle in sight",
            signal_range="390M:395M:25k",
            sample_count=1,
            current_reading=None,
            recent_peak=None,
            now=start,
        )
        self.assertEqual(started["event_type"], "interval_start")
        self.assertEqual(started["label"], "vehicle_nearby")
        self.assertTrue(tracker.vehicle_interval_active())

        ended = tracker.mark(
            "vehicle_in_sight",
            signal_range="390M:395M:25k",
            sample_count=2,
            current_reading=None,
            recent_peak=None,
            now=end,
        )
        self.assertEqual(ended["event_type"], "interval_end")
        self.assertEqual(ended["duration_seconds"], "8")
        self.assertFalse(tracker.vehicle_interval_active())


class DetectionTests(unittest.TestCase):
    def test_cluster_candidate_bins_groups_by_frequency_gap(self) -> None:
        readings = [
            reading_payload(390_000_000, -30.0, -40.0, 10.0),
            reading_payload(390_025_000, -28.0, -40.0, 12.0),
            reading_payload(390_200_000, -35.0, -42.0, 7.0),
        ]

        clusters = cluster_candidate_bins(readings, 60_000)

        self.assertEqual(len(clusters), 2)
        self.assertEqual(clusters[0]["bin_count"], 2)
        self.assertAlmostEqual(clusters[0]["cluster_width_khz"], 25.0)
        self.assertEqual(clusters[1]["bin_count"], 1)

    def test_update_recent_peaks_merges_nearby_stronger_peak(self) -> None:
        recent_peaks: deque[dict[str, object]] = deque(maxlen=5)
        first_time = dt.datetime(2026, 5, 25, 12, 0, tzinfo=dt.timezone.utc)
        second_time = first_time + dt.timedelta(seconds=1)

        update_recent_peaks(
            recent_peaks,
            {
                "timestamp": first_time,
                "frequency_mhz": 390.0,
                "power_db": -20.0,
            },
        )
        update_recent_peaks(
            recent_peaks,
            {
                "timestamp": second_time,
                "frequency_mhz": 390.05,
                "power_db": -18.0,
            },
        )

        self.assertEqual(len(recent_peaks), 1)
        self.assertEqual(recent_peaks[0]["power_db"], -18.0)


class ScannerTests(unittest.TestCase):
    def test_parse_power_row_and_range_spec(self) -> None:
        row = [
            "2026-05-25",
            "12:00:00",
            "390000000",
            "390100000",
            "25000",
            "4",
            "-40",
            "-38",
            "-37",
            "-39",
        ]

        bins = parse_power_row(row)

        self.assertEqual(parse_range_spec("390M:391M:25k"), (390_000_000, 391_000_000, 25_000))
        self.assertEqual(len(bins), 4)
        self.assertEqual(bins[0], (390_012_500, -40.0))
        self.assertEqual(parse_power_row(["bad"]), [])
        self.assertEqual(parse_power_row(row[:2] + ["bad"] + row[3:]), [])
        with self.assertRaises(ValueError):
            parse_range_spec("390M:391M")


class AnalysisTests(unittest.TestCase):
    def test_load_drive_analysis_skips_bad_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readings_path = tmp_path / "readings.csv"
            observations_path = tmp_path / "observations.csv"
            write_readings_fixture(
                readings_path,
                [
                    valid_reading_row(),
                    {"timestamp": "bad", "sample": "x", "rank": "1"},
                ],
            )
            write_observations_fixture(
                observations_path,
                [{"timestamp": "not-a-time", "event_type": "point"}],
            )

            result = load_drive_analysis(readings_path, observations_path)

            self.assertTrue(result["ok"])
            self.assertEqual(result["rank1_samples"], 1)


if __name__ == "__main__":
    unittest.main()
