# RF Data Pipeline

Edge RF Sentinel is built around a simple pipeline: capture RF power readings,
turn those readings into burst events, preserve field observations, and use the
recorded sessions for offline analysis.

The current system is deterministic and rule-based. It does not classify
vehicles by itself and it does not decode radio traffic. Its purpose is to
collect clean, reviewable RF event data that can later support classifier
experiments.

## Live Capture

Live capture uses `rtl_power` through an RTL-SDR receiver.

```text
RTL-SDR receiver
        |
        v
rtl_power sweep rows
        |
        v
frequency bin + measured power
```

Each scan row contains a frequency range, bin spacing, and power readings. The
Python monitor parses those rows into frequency/power bins and keeps the
strongest bins from each scan.

Keep scan ranges narrow. Low-cost SDR receivers can only observe a limited
slice of spectrum at once, so wide scans reduce the update rate for each
frequency.

## Baseline And Burst Detection

The live monitor maintains a rolling baseline per frequency bin. A reading
becomes interesting when it is high relative to its own recent baseline and
high enough in absolute power.

The main live signals are:

- `power_db`: measured signal power for a frequency bin
- `baseline_db`: rolling baseline for that bin
- `delta_db`: `power_db - baseline_db`
- `threshold_db`: required delta above baseline
- `incident_min_power_db`: absolute minimum power for incident creation

Short isolated spikes are filtered by requiring repeated samples before a
cluster is confirmed. Confirmed clusters are tracked until they remain below
threshold for the configured hold period.

## Session Logs

Each run writes structured CSV logs. These files are the source of truth for
later review.

```text
logs/sessions/<session_id>/
        readings.csv
        activity_clusters.csv
        observations.csv
```

### `readings.csv`

Strongest bins for every scan row.

Use this file for replay, burst extraction, and offline analysis. It contains
both normal and interesting samples.

Key fields:

- `timestamp`
- `sample`
- `rank`
- `frequency_hz`
- `frequency_mhz`
- `power_db`
- `baseline_db`
- `delta_db`
- `threshold_db`
- `incident_min_power_db`
- `is_incident`

### `activity_clusters.csv`

Clustered incident state changes.

Adjacent active bins are grouped into one cluster so a wide or slightly drifting
burst is easier to inspect as one event. This is the better live incident file
for field review.

### `observations.csv`

Manual field labels from the dashboard.

Labels are operator observations, not automatic truth. For example, pressing
`Vehicle in sight` creates a timestamped interval that can later be compared
against RF activity before, during, and after the visible event.

## Offline Analysis

Offline analysis loads a completed session and summarizes what happened.

```text
readings.csv + observations.csv
        |
        v
edge_rf.analysis
        |
        v
burst events, label quality, similar windows, minute summaries
```

The analysis path answers questions such as:

- how long the session lasted
- when the strongest RF bursts happened
- whether the session contains labelled vehicle intervals
- whether similar or stronger bursts occurred without labels
- whether events repeat at suspiciously regular intervals
- which frequencies dominated each burst window

This helps separate field observations from false positives caused by fixed
locations, bridges, intersections, local RF infrastructure, or receiver noise.

## C++ Feature Extraction

The C++ feature extractor converts `readings.csv` into compact burst-level
features.

Build:

```sh
make
```

Run:

```sh
build/extract_features logs/sessions/<session_id>/readings.csv features.csv
```

Feature columns:

- `start_timestamp`
- `end_timestamp`
- `duration_seconds`
- `center_frequency_mhz`
- `min_frequency_mhz`
- `max_frequency_mhz`
- `cluster_width_khz`
- `peak_power_db`
- `peak_delta_db`
- `mean_power_db`
- `mean_delta_db`
- `rise_rate_db_s`
- `fall_rate_db_s`
- `snapshots`
- `peak_bin_count`
- `event_density_hz`

This gives a deterministic, replayable path from recorded RF samples to
model-ready event rows.

## Labels And Classifier Readiness

The current labels are manual field observations. They are useful, but sparse
and imperfect.

Before a classifier would be credible, the project needs:

- more labelled sessions across different routes and RF environments
- consistent antenna placement and gain settings
- negative examples from known noisy locations
- event features generated from the same extraction path
- train/test splits by drive session, not by individual row
- evaluation against false positives, not just strong-looking bursts

Until then, the project should be described as an RF sensing and labelled data
pipeline, not a finished automatic detector.

## Current Limitations

- Signal strength alone is noisy and location-dependent.
- Absolute dB values from low-cost SDR hardware are not calibrated field
  strength measurements.
- Antenna placement, cable routing, vehicle body position, and gain settings
  can change the readings.
- Nearby fixed infrastructure can create repeatable bursts that look
  interesting but are not vehicle-related.
- Manual labels depend on what the operator noticed at the time.
- No trained classifier is included yet.

These limitations are why the project records full sessions instead of only
showing live alerts. The logs make it possible to inspect false positives,
compare labelled and unlabelled windows, and improve the detection rules with
evidence.
