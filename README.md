# Edge RF Sentinel

Embedded SDR-based RF monitor for real-time signal burst detection, adaptive
baseline tracking, event logging, edge visualization, and ML-ready field
observation workflows.

## Overview

Edge RF Sentinel turns an RTL-SDR receiver into a lightweight RF sensing
pipeline:

- scans a narrow RF range with `rtl_power`
- builds rolling per-frequency baselines
- detects signal bursts above baseline
- groups adjacent active bins into signal clusters
- records timestamped readings and incidents as CSV
- serves a local dashboard for field use
- keeps logs suitable for replay, labeling, and future embedded ML experiments

The detector is intentionally rule-based today. The logging format is designed
to support future supervised or tinyML classifiers once enough field-labeled RF
captures are collected.

## Hardware

- RTL-SDR compatible receiver
- frequency-appropriate antenna
- macOS or Linux host
- optional Raspberry Pi / small display for embedded deployment

## Install

macOS:

```sh
brew install librtlsdr
```

Linux packages vary by distro, but the required command-line tool is
`rtl_power` from the `rtl-sdr` tooling.

Confirm the SDR is visible:

```sh
rtl_test
```

Stop `rtl_test` before running the monitor. Only one process can claim the SDR
at a time.

## Quick Start

Fixed-site style preset:

```sh
python3 rf_signal_monitor.py --preset base --dashboard
```

Mobile/burst style preset:

```sh
python3 rf_signal_monitor.py --preset mobile --dashboard
```

Open the dashboard:

```text
http://127.0.0.1:8765
```

## Custom Scan

```sh
python3 rf_signal_monitor.py \
  --range 390M:395M:25k \
  --gain 30 \
  --interval 1s \
  --threshold-db 8 \
  --incident-min-power-db -20 \
  --dashboard
```

Keep scan ranges narrow for responsive updates. Low-cost SDR receivers only
observe a small slice of spectrum at once, so broad ranges require hopping and
reduce per-frequency update rate.

## Outputs

The monitor writes two CSV streams:

- readings log: strongest bins for every scan row
- activity log: incident `start`, `active`, and `end` rows

Preset logs are written under `logs/` and are ignored by Git. Use these logs for
offline analysis, replay, and field-label correlation.

## Project Status

Current version: `v0.0.2`

- `v0.0.1`: public repo hygiene, docs, generic SDR framing
- `v0.0.2`: monitor CLI, dashboard, adaptive baseline detector, CSV logging

## Safety And Scope

This project measures RF power and signal timing. It does not decode traffic.
Use it for passive spectrum observation, embedded systems experimentation, and
field logging within your local laws and authorizations.

## Roadmap

- split detector, dashboard, and scanner into package modules
- add offline replay from readings CSV
- add manual field-label workflow
- add Raspberry Pi boot-to-dashboard profile
- evaluate lightweight embedded ML classifiers from labeled observations
