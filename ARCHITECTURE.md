# Edge RF Sentinel Architecture

Edge RF Sentinel is a field-data collection and RF event analysis prototype. It
uses an RTL-SDR receiver to sample signal strength over a configured frequency
range, records structured session logs, and provides a local dashboard for live
monitoring and labelled drive-session review.

The project is intentionally split into a live sensing path and an offline
analysis path. The live path keeps the dashboard responsive and writes stable
CSV logs. The offline path turns completed sessions into burst-level summaries
that can be inspected, labelled, and used for future classifier experiments.

## Runtime Flow

```text
RTL-SDR / demo source
        |
        v
edge_rf.scanner
        |
        v
rf_signal_monitor.py
        |
        +--> edge_rf.detection
        |        |
        |        v
        |   recent peaks, clusters, incidents
        |
        +--> edge_rf.csv_logs
        |        |
        |        v
        |   readings.csv, activity.csv, activity_clusters.csv, observations.csv
        |
        +--> dashboard state
                 |
                 v
            local browser dashboard
```

## Main Modules

### `rf_signal_monitor.py`

Application entrypoint and live orchestration loop.

Responsibilities:

- parse CLI arguments and tune presets
- create per-session log paths
- start the SDR or demo scanner
- maintain rolling baselines
- call detection helpers for peaks and cluster incidents
- update dashboard state
- write session CSV logs
- handle dashboard tune changes and manual observation labels

This file should read as the top-level system loop. Lower-level scanner,
detection, logging, and analysis details live in `edge_rf/`.

### `edge_rf/scanner.py`

SDR input and row parsing.

Responsibilities:

- check that `rtl_power` is installed
- start the `rtl_power` subprocess for live SDR capture
- parse `rtl_power` CSV rows into frequency/power bins
- parse frequency range strings such as `380M:385M:25k`
- generate simulated rows for dashboard/UI debugging without SDR hardware
- format frequencies for terminal output

Real RF measurements require RTL-SDR hardware. Demo mode only exists to test the
software pipeline and dashboard when the dongle is not connected.

### `edge_rf/detection.py`

Live signal processing helpers.

Responsibilities:

- convert raw readings into normalized reading payloads
- track recent strong peaks for the dashboard
- merge nearby frequency bins into candidate clusters
- maintain cluster tracks across samples
- confirm cluster incidents only after enough repeated samples
- write cluster start, active, and end events through the CSV logging layer

This module contains deterministic signal rules. It does not decode radio
traffic and it does not classify vehicles by itself.

### `edge_rf/csv_logs.py`

Session CSV writer layer.

Responsibilities:

- create CSV files with stable headers
- write raw strongest-bin readings
- write single-bin incident activity
- write clustered incident activity
- write manual observation labels
- sanitize dashboard labels before logging

The CSV schemas are intentionally stable because `edge_rf.analysis` and
external review tools depend on them.

### `edge_rf/analysis.py`

Offline drive-session analysis.

Responsibilities:

- load completed session CSV files
- collapse readings into burst events
- calculate burst features such as duration, peak power, delta, frequency
  stability, repeat timing, and double-burst patterns
- summarize label quality
- compare labelled vehicle intervals against unlabelled RF events
- produce dashboard-ready analysis payloads

This module is the main bridge from raw RF logging toward future
model-ready datasets.

## Session Data

Each run writes a session directory under `logs/sessions/` unless explicit log
paths are provided.

Important files:

- `readings.csv`: strongest frequency bins from each scan row
- `activity.csv`: single-bin incident starts, active samples, and ends
- `activity_clusters.csv`: clustered incident starts, active samples, and ends
- `observations.csv`: manual field labels such as "vehicle in sight"

The useful analysis path is:

```text
readings.csv + observations.csv
        |
        v
edge_rf.analysis.load_drive_analysis(...)
        |
        v
burst windows, label quality, similar events, per-session summaries
```

## Interview Framing

The project is best described as a real-world sensing and data pipeline:

- collect noisy RF observations from commodity SDR hardware
- apply deterministic signal processing to find burst events
- capture human labels during field tests
- preserve reproducible session logs
- analyze completed sessions for patterns, false positives, and model-ready
  features

The current system is not a guaranteed vehicle detector. It is a working
prototype for collecting, labelling, and analysing RF events so that detection
rules or lightweight ML classifiers can be evaluated honestly.
