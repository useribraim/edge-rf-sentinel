#!/usr/bin/env python3
"""Record SDR signal-strength incidents with timestamps."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from edge_rf.analysis import load_drive_analysis
from edge_rf.config import apply_preset_defaults, apply_session_paths
from edge_rf.csv_logs import (
    open_cluster_activity_log,
    open_observations_log,
    open_readings_log,
)
from edge_rf.dashboard import (
    DashboardState,
    dashboard_config_payload,
    dashboard_reset_payload,
    start_dashboard,
)
from edge_rf.detection import (
    cluster_candidate_bins,
    update_cluster_tracks,
)
from edge_rf.runtime import (
    MonitorRuntimeState,
    build_readings,
    dashboard_payload,
    label_prompt_for,
    update_signal_summary,
    write_top_readings,
)
from edge_rf.scanner import (
    demo_power_row,
    parse_power_row,
    require_rtl_power,
    start_rtl_power,
)
from edge_rf.tuning import TUNE_PROFILES, apply_tune


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
