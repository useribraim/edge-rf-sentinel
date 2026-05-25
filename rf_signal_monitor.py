#!/usr/bin/env python3
"""Record SDR signal-strength incidents with timestamps."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import statistics
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from edge_rf.analysis import load_drive_analysis
from edge_rf.config import apply_preset_defaults, apply_session_paths
from edge_rf.csv_logs import (
    open_cluster_activity_log,
    open_observations_log,
    open_readings_log,
    write_reading,
)
from edge_rf.dashboard import (
    DashboardState,
    dashboard_config_payload,
    dashboard_reset_payload,
    start_dashboard,
)
from edge_rf.detection import (
    ClusterTrack,
    ReadingPayload,
    cluster_candidate_bins,
    peak_payload,
    reading_payload,
    recent_peaks_payload,
    update_cluster_tracks,
    update_recent_peaks,
)
from edge_rf.observations import ObservationTracker
from edge_rf.scanner import (
    demo_power_row,
    parse_power_row,
    require_rtl_power,
    start_rtl_power,
)
from edge_rf.tuning import TUNE_PROFILES, apply_tune


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use rtl_power to watch an RF frequency range, build a rolling "
            "baseline, and write timestamped signal-strength incidents."
        )
    )
    parser.add_argument(
        "--preset",
        choices=["base", "mobile"],
        default=None,
        help=(
            "Apply a preset before explicit CLI overrides: base=fixed-site "
            "example, mobile=mobile/uplink example."
        ),
    )
    parser.add_argument(
        "--tune",
        choices=sorted(TUNE_PROFILES),
        default=None,
        help="Sensitivity tune profile. Can also be changed from the dashboard.",
    )
    parser.add_argument(
        "--range",
        default=None,
        help="rtl_power frequency range. Default: 390M:395M:25k",
    )
    parser.add_argument(
        "--interval",
        default=None,
        help="rtl_power integration interval. Default: 5s",
    )
    parser.add_argument(
        "--gain",
        default=None,
        help="RTL-SDR gain passed to rtl_power. Try 20, 30, 40, or auto.",
    )
    parser.add_argument(
        "--ppm",
        type=int,
        default=0,
        help="Frequency correction in ppm. Default: 0",
    )
    parser.add_argument(
        "--baseline-samples",
        type=int,
        default=60,
        help="Rolling samples per channel used for the baseline. Default: 60",
    )
    parser.add_argument(
        "--warmup-samples",
        type=int,
        default=12,
        help="Samples before alerting starts. Default: 12",
    )
    parser.add_argument(
        "--threshold-db",
        type=float,
        default=None,
        help="Alert when channel power is this many dB above baseline. Default: 8",
    )
    parser.add_argument(
        "--incident-min-power-db",
        type=float,
        default=None,
        help=(
            "Only create incidents when absolute power is at or above this dB. "
            "Default: -20"
        ),
    )
    parser.add_argument(
        "--cluster-khz",
        type=float,
        default=None,
        help="Group active dashboard incidents within this bandwidth. Default: 60",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=8,
        help="Show this many strongest bins each interval. Default: 8",
    )
    parser.add_argument(
        "--hold-samples",
        type=int,
        default=None,
        help="End an incident after this many below-threshold samples. Default: 3",
    )
    parser.add_argument(
        "--min-incident-samples",
        type=int,
        default=None,
        help="Confirm a cluster incident after this many consecutive samples. Default: 3",
    )
    parser.add_argument(
        "--activity-log",
        default=None,
        help="CSV file for timestamped incidents. Default: <session-dir>/activity.csv",
    )
    parser.add_argument(
        "--readings-log",
        default=None,
        help="CSV file for strongest readings each scan row. Default: <session-dir>/readings.csv",
    )
    parser.add_argument(
        "--observations-log",
        default=None,
        help="CSV file for manual field markers. Default: <session-dir>/observations.csv",
    )
    parser.add_argument(
        "--session-dir",
        default=None,
        help=(
            "Directory for this run's CSV files. Default: "
            "logs/sessions/<timestamp>_<preset-or-rf>."
        ),
    )
    parser.add_argument(
        "--log-top",
        type=int,
        default=5,
        help="Write this many strongest readings per scan row. Default: 5",
    )
    parser.add_argument(
        "--absolute-strong-db",
        type=float,
        default=None,
        help=(
            "Dashboard marks absolute signal power at or above this dB as strong. "
            "Default: -10"
        ),
    )
    parser.add_argument(
        "--absolute-extreme-db",
        type=float,
        default=None,
        help=(
            "Dashboard marks absolute signal power at or above this dB as extreme. "
            "Default: -5"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print strongest-bin status lines; still writes CSV.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Serve a local visual dashboard at http://127.0.0.1:8765.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the dashboard with simulated RF data; no SDR hardware required.",
    )
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard host. Default: 127.0.0.1",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8765,
        help="Dashboard port. Default: 8765",
    )
    args = parser.parse_args()
    explicit_logs = {
        "activity_log": args.activity_log is not None,
        "readings_log": args.readings_log is not None,
        "observations_log": args.observations_log is not None,
    }
    args = apply_preset_defaults(args)
    return apply_session_paths(args, explicit_logs)


def new_power_output_path() -> Path:
    with tempfile.NamedTemporaryFile(prefix="rf_power_", suffix=".csv", delete=False) as fp:
        return Path(fp.name)


def stop_process(process) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def delete_temp_file(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


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


def monitor(args: argparse.Namespace) -> int:
    if not args.demo:
        require_rtl_power()

    state = MonitorRuntimeState(args.baseline_samples)
    cluster_activity_path = Path(args.activity_log).with_name(
        f"{Path(args.activity_log).stem}_clusters.csv"
    )
    cluster_activity_fp, cluster_activity_writer = open_cluster_activity_log(
        cluster_activity_path
    )
    readings_fp, readings_writer = open_readings_log(Path(args.readings_log))
    observations_fp, observations_writer = open_observations_log(
        Path(args.observations_log)
    )
    pending_tune: dict[str, str | None] = {"name": None}
    pending_tune_lock = threading.Lock()

    def mark_observation(label: str) -> dict[str, object]:
        marker = state.observation_tracker.mark(
            label,
            signal_range=args.range,
            sample_count=state.sample_count,
            current_reading=state.latest_top_reading,
            recent_peak=state.recent_peak,
        )
        observations_writer.writerow(marker)
        observations_fp.flush()
        state.recent_observations.appendleft(marker)
        dashboard_state.update(
            recent_observations=list(state.recent_observations),
            vehicle_interval_active=state.observation_tracker.vehicle_interval_active(),
            vehicle_interval_started_at=state.observation_tracker.vehicle_interval_started_at(),
        )
        print(f"MARK {marker['timestamp']} {marker['label']} {marker['event_type']}")
        return marker

    def select_tune(tune_name: str) -> dict[str, object]:
        if tune_name not in TUNE_PROFILES:
            return {"ok": False, "error": f"unknown tune: {tune_name}"}
        with pending_tune_lock:
            pending_tune["name"] = tune_name
        profile = TUNE_PROFILES[tune_name]
        dashboard_state.update(
            message=f"Switching to {profile['label']}",
            pending_tune=tune_name,
        )
        return {
            "ok": True,
            "tune": tune_name,
            "tune_label": profile["label"],
        }

    def load_analysis() -> dict[str, object]:
        try:
            return load_drive_analysis(
                Path(args.readings_log),
                Path(args.observations_log),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    dashboard_state = DashboardState()
    dashboard_state.update(
        **dashboard_config_payload(args),
        vehicle_interval_active=False,
        vehicle_interval_started_at=None,
        label_prompt="",
    )

    dashboard_server = None
    if args.dashboard:
        dashboard_server = start_dashboard(
            dashboard_state,
            args.dashboard_host,
            args.dashboard_port,
            mark_observation,
            select_tune,
            load_analysis,
        )
        print(f"Dashboard: http://{args.dashboard_host}:{args.dashboard_port}")

    output_path = new_power_output_path()

    if args.demo:
        print(f"Demo mode: simulating {args.range}; no SDR hardware is required.")
    else:
        print(f"Monitoring {args.range}; writing temporary rtl_power CSV to {output_path}")
    print(f"Session: {args.session_id}")
    print(f"Session directory: {Path(args.session_dir).resolve()}")
    print(f"Recording cluster incidents to {cluster_activity_path.resolve()}")
    print(f"Recording strongest readings to {Path(args.readings_log).resolve()}")
    print(f"Recording field markers to {Path(args.observations_log).resolve()}")
    print("Press Ctrl-C to stop.")

    process = None if args.demo else start_rtl_power(args, output_path)
    last_size = 0

    try:
        while args.demo or process.poll() is None:
            time.sleep(1)
            with pending_tune_lock:
                tune_to_apply = pending_tune["name"]
                pending_tune["name"] = None

            if tune_to_apply:
                stop_process(process)
                delete_temp_file(output_path)

                apply_tune(args, tune_to_apply)
                state.reset_for_tune()

                output_path = new_power_output_path()
                process = None if args.demo else start_rtl_power(args, output_path)
                last_size = 0
                dashboard_state.update(
                    **dashboard_reset_payload(args),
                )
                print(
                    f"Switched tune to {TUNE_PROFILES[tune_to_apply]['label']} "
                    f"({tune_to_apply}); monitoring {args.range}"
                )
                continue

            if args.demo:
                rows = [demo_power_row(args, state.sample_count)]
            else:
                if not output_path.exists():
                    continue

                current_size = output_path.stat().st_size
                if current_size == last_size:
                    continue

                with output_path.open("r", newline="") as fp:
                    fp.seek(last_size)
                    rows = list(csv.reader(fp))
                    last_size = fp.tell()

            for row in rows:
                bins = parse_power_row(row)
                if not bins:
                    continue

                state.sample_count += 1
                now = dt.datetime.now().astimezone()
                enriched, candidate_bins = build_readings(
                    bins=bins,
                    baselines=state.baselines,
                    sample_count=state.sample_count,
                    args=args,
                )

                strongest = sorted(
                    enriched,
                    key=lambda item: float(item["power_db"]),
                    reverse=True,
                )[: args.top]
                state.latest_top_reading, state.recent_peak = update_signal_summary(
                    strongest=strongest,
                    now=now,
                    sample_count=state.sample_count,
                    warmup_samples=args.warmup_samples,
                    recent_peak=state.recent_peak,
                    recent_peaks=state.recent_peaks,
                    series=state.series,
                )
                write_top_readings(
                    readings_writer=readings_writer,
                    readings_fp=readings_fp,
                    strongest=strongest,
                    candidate_bins=candidate_bins,
                    now=now,
                    sample_count=state.sample_count,
                    args=args,
                )
                candidate_clusters = cluster_candidate_bins(
                    candidate_bins,
                    args.cluster_khz * 1_000,
                )
                label_prompt = label_prompt_for(
                    sample_count=state.sample_count,
                    args=args,
                    observation_tracker=state.observation_tracker,
                    latest_top_reading=state.latest_top_reading,
                )
                (
                    state.next_cluster_track_id,
                    active_clusters,
                    state.strongest_incidents,
                ) = update_cluster_tracks(
                    tracks=state.cluster_tracks,
                    candidate_clusters=candidate_clusters,
                    now=now,
                    args=args,
                    next_track_id=state.next_cluster_track_id,
                    cluster_writer=cluster_activity_writer,
                    cluster_fp=cluster_activity_fp,
                    strongest_incidents=state.strongest_incidents,
                )
                dashboard_state.update(
                    **dashboard_payload(
                        now=now,
                        sample_count=state.sample_count,
                        strongest=strongest,
                        active_clusters=active_clusters,
                        candidate_bins=candidate_bins,
                        recent_observations=state.recent_observations,
                        strongest_incidents=state.strongest_incidents,
                        recent_peaks=state.recent_peaks,
                        label_prompt=label_prompt,
                        series=state.series,
                        recent_peak=state.recent_peak,
                    )
                )

                if not args.quiet:
                    timestamp = now.strftime("%H:%M:%S")
                    top_text = ", ".join(
                        f"{item['frequency_mhz']:.6f} MHz {item['power_db']:.1f} dB"
                        for item in strongest
                    )
                    print(f"[{timestamp}] strongest: {top_text}")

    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        cluster_activity_fp.flush()
        cluster_activity_fp.close()
        readings_fp.flush()
        readings_fp.close()
        observations_fp.flush()
        observations_fp.close()
        stop_process(process)
        delete_temp_file(output_path)
        if dashboard_server is not None:
            dashboard_server.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(monitor(parse_args()))
