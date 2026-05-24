#!/usr/bin/env python3
"""Record SDR signal-strength incidents with timestamps."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import statistics
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from edge_rf.analysis import load_drive_analysis
from edge_rf.csv_logs import (
    open_activity_log,
    open_cluster_activity_log,
    open_observations_log,
    open_readings_log,
    write_cluster_activity,
    write_reading,
)
from edge_rf.dashboard import (
    DashboardState,
    dashboard_config_payload,
    dashboard_reset_payload,
    start_dashboard,
)
from edge_rf.detection import (
    cluster_candidate_bins,
    cluster_readings,
    peak_payload,
    reading_payload,
    recent_peaks_payload,
    update_cluster_tracks,
    update_recent_peaks,
)
from edge_rf.observations import ObservationTracker
from edge_rf.scanner import (
    demo_power_row,
    format_freq,
    parse_power_row,
    require_rtl_power,
    start_rtl_power,
)
from edge_rf.tuning import TUNE_PROFILES, apply_tune, tune_values


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


def apply_preset_defaults(args: argparse.Namespace) -> argparse.Namespace:
    presets = {
        "base": {
            "range": "390M:395M:25k",
            "interval": "1s",
            "gain": "30",
            "threshold_db": 8.0,
            "incident_min_power_db": -20.0,
            "activity_log": "logs/rf_base_activity.csv",
            "readings_log": "logs/rf_base_readings.csv",
            "observations_log": "logs/rf_base_observations.csv",
            "absolute_strong_db": -10.0,
            "absolute_extreme_db": -5.0,
            "cluster_khz": 60.0,
            "hold_samples": 3,
            "min_incident_samples": 3,
        },
        "mobile": {
            "range": "380M:385M:25k",
            "interval": "1s",
            "gain": "8.7",
            "threshold_db": 26.0,
            "incident_min_power_db": -10.0,
            "activity_log": "logs/rf_mobile_activity.csv",
            "readings_log": "logs/rf_mobile_readings.csv",
            "observations_log": "logs/rf_mobile_observations.csv",
            "absolute_strong_db": -8.0,
            "absolute_extreme_db": -4.0,
            "cluster_khz": 100.0,
            "hold_samples": 12,
            "min_incident_samples": 12,
        },
    }
    defaults = {
        "range": "390M:395M:25k",
        "interval": "5s",
        "gain": "auto",
        "threshold_db": 8.0,
        "incident_min_power_db": -20.0,
        "activity_log": "logs/rf_activity.csv",
        "readings_log": "logs/rf_readings.csv",
        "observations_log": "logs/rf_observations.csv",
        "absolute_strong_db": -10.0,
        "absolute_extreme_db": -5.0,
        "cluster_khz": 60.0,
        "hold_samples": 3,
        "min_incident_samples": 3,
    }
    selected = dict(presets.get(args.preset or "", defaults))
    if args.tune:
        selected.update(tune_values(args.tune))

    for key, fallback in defaults.items():
        value = getattr(args, key)
        if value is None:
            setattr(args, key, selected.get(key, fallback))

    args.tune = args.tune or ("strict" if args.preset == "mobile" else "custom")
    return args


def apply_session_paths(
    args: argparse.Namespace,
    explicit_logs: dict[str, bool],
) -> argparse.Namespace:
    session_id = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    mode = args.preset or "rf"
    args.session_id = f"{session_id}_{mode}"
    session_dir = Path(args.session_dir) if args.session_dir else Path("logs/sessions") / args.session_id
    args.session_dir = str(session_dir)

    if not any(explicit_logs.values()):
        args.activity_log = str(session_dir / "activity.csv")
        args.readings_log = str(session_dir / "readings.csv")
        args.observations_log = str(session_dir / "observations.csv")
        return args

    if not explicit_logs["activity_log"]:
        args.activity_log = str(session_dir / "activity.csv")
    if not explicit_logs["readings_log"]:
        args.readings_log = str(session_dir / "readings.csv")
    if not explicit_logs["observations_log"]:
        args.observations_log = str(session_dir / "observations.csv")
    return args


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


def monitor(args: argparse.Namespace) -> int:
    if not args.demo:
        require_rtl_power()

    baselines: dict[int, deque[float]] = defaultdict(
        lambda: deque(maxlen=args.baseline_samples)
    )
    sample_count = 0
    activity_fp, activity_writer = open_activity_log(Path(args.activity_log))
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
    recent_events: deque[dict[str, object]] = deque(maxlen=20)
    recent_observations: deque[dict[str, object]] = deque(maxlen=20)
    series: deque[dict[str, object]] = deque(maxlen=180)
    strongest_incidents: list[dict[str, object]] = []
    cluster_tracks: dict[int, dict[str, object]] = {}
    next_cluster_track_id = 1
    recent_peak: dict[str, object] | None = None
    recent_peaks: deque[dict[str, object]] = deque(maxlen=5)
    latest_top_reading: dict[str, object] | None = None
    pending_tune: dict[str, str | None] = {"name": None}
    pending_tune_lock = threading.Lock()
    observation_tracker = ObservationTracker()

    def mark_observation(label: str) -> dict[str, object]:
        marker = observation_tracker.mark(
            label,
            signal_range=args.range,
            sample_count=sample_count,
            current_reading=latest_top_reading,
            recent_peak=recent_peak,
        )
        observations_writer.writerow(marker)
        observations_fp.flush()
        recent_observations.appendleft(marker)
        dashboard_state.update(
            recent_observations=list(recent_observations),
            vehicle_interval_active=observation_tracker.vehicle_interval_active(),
            vehicle_interval_started_at=observation_tracker.vehicle_interval_started_at(),
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
    print(f"Recording incidents to {Path(args.activity_log).resolve()}")
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
                baselines.clear()
                cluster_tracks.clear()
                next_cluster_track_id = 1
                sample_count = 0
                recent_peak = None
                recent_peaks.clear()
                latest_top_reading = None
                series.clear()
                strongest_incidents.clear()
                recent_events.clear()

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
                rows = [demo_power_row(args, sample_count)]
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

                sample_count += 1
                now = dt.datetime.now().astimezone()
                enriched: list[dict[str, float | int]] = []
                candidate_bins: list[dict[str, float | int]] = []

                for freq_hz, power_db in bins:
                    history = baselines[freq_hz]
                    baseline = statistics.median(history) if history else power_db
                    delta = power_db - baseline
                    enriched.append(reading_payload(freq_hz, power_db, baseline, delta))
                    is_warm = sample_count > args.warmup_samples
                    is_high = (
                        is_warm
                        and delta >= args.threshold_db
                        and power_db >= args.incident_min_power_db
                    )
                    if is_high:
                        candidate_bins.append(
                            reading_payload(freq_hz, power_db, baseline, delta)
                        )
                    history.append(power_db)

                strongest = sorted(
                    enriched,
                    key=lambda item: float(item["power_db"]),
                    reverse=True,
                )[: args.top]
                if strongest:
                    top_reading = strongest[0]
                    latest_top_reading = {
                        "frequency_mhz": float(top_reading["frequency_mhz"]),
                        "power_db": float(top_reading["power_db"]),
                        "baseline_db": float(top_reading["baseline_db"]),
                        "delta_db": float(top_reading["delta_db"]),
                    }
                    if (
                        recent_peak is None
                        or float(top_reading["power_db"]) > float(recent_peak["power_db"])
                    ):
                        recent_peak = {
                            "timestamp": now,
                            "frequency_mhz": float(top_reading["frequency_mhz"]),
                            "power_db": float(top_reading["power_db"]),
                            "baseline_db": float(top_reading["baseline_db"]),
                            "delta_db": float(top_reading["delta_db"]),
                        }
                    is_peak_sample = (
                        sample_count > args.warmup_samples
                        and float(top_reading["power_db"]) >= -25.0
                    )
                    if is_peak_sample:
                        update_recent_peaks(
                            recent_peaks,
                            peak_payload(top_reading, now),
                        )
                    series.append(
                        {
                            "timestamp": now.strftime("%H:%M:%S"),
                            "frequency_mhz": float(top_reading["frequency_mhz"]),
                            "power_db": float(top_reading["power_db"]),
                            "baseline_db": float(top_reading["baseline_db"]),
                            "delta_db": float(top_reading["delta_db"]),
                        }
                    )
                candidate_freqs = {
                    int(reading["frequency_hz"]) for reading in candidate_bins
                }
                for rank, reading in enumerate(strongest[: args.log_top], start=1):
                    write_reading(
                        readings_writer,
                        timestamp=now,
                        sample_count=sample_count,
                        rank=rank,
                        reading=reading,
                        threshold_db=args.threshold_db,
                        incident_min_power_db=args.incident_min_power_db,
                        is_incident=int(reading["frequency_hz"]) in candidate_freqs,
                    )
                readings_fp.flush()
                candidate_clusters = cluster_candidate_bins(
                    candidate_bins,
                    args.cluster_khz * 1_000,
                )
                label_prompt = ""
                if (
                    sample_count > args.warmup_samples
                    and active_vehicle_interval is None
                    and latest_top_reading is not None
                    and (
                        float(latest_top_reading["power_db"]) >= -20.0
                        or float(latest_top_reading["delta_db"]) >= 20.0
                    )
                ):
                    label_prompt = "Label if vehicle is visible"
                (
                    next_cluster_track_id,
                    active_clusters,
                    strongest_incidents,
                ) = update_cluster_tracks(
                    tracks=cluster_tracks,
                    candidate_clusters=candidate_clusters,
                    now=now,
                    args=args,
                    next_track_id=next_cluster_track_id,
                    cluster_writer=cluster_activity_writer,
                    cluster_fp=cluster_activity_fp,
                    strongest_incidents=strongest_incidents,
                )
                dashboard_state.update(
                    updated_at=now.isoformat(timespec="seconds"),
                    sample_count=sample_count,
                    strongest=strongest,
                    active_incidents=active_clusters,
                    active_bins=candidate_bins,
                    recent_events=list(recent_events),
                    recent_observations=list(recent_observations),
                    strongest_incidents=strongest_incidents,
                    recent_peaks=recent_peaks_payload(recent_peaks, now),
                    label_prompt=label_prompt,
                    series=list(series),
                    recent_peak={
                        **recent_peak,
                        "timestamp": recent_peak["timestamp"].isoformat(timespec="seconds"),
                        "age_seconds": (now - recent_peak["timestamp"]).total_seconds(),
                    }
                    if recent_peak is not None
                    else None,
                    status="active" if active_clusters else "normal",
                    message="Active incident" if active_clusters else "Monitoring",
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
        activity_fp.flush()
        activity_fp.close()
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
