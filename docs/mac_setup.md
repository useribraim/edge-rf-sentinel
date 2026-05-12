# RF Signal Monitor on macOS

This project uses an RTL-SDR receiver as a passive RF power monitor. It watches
a narrow frequency range, builds an adaptive baseline, records signal bursts,
and serves a local dashboard for at-a-glance field use.

It measures spectrum power only. It does not decode private, protected, or
encrypted communications.

## Install SDR Tools

```sh
brew install librtlsdr
```

Confirm the dongle is visible:

```sh
rtl_test
```

Stop `rtl_test` with `Ctrl-C` after it prints device/sample output. Only one
program can use the dongle at a time.

## Run a Fixed-Site Coverage Preset

```sh
python3 rf_signal_monitor.py --preset base --dashboard
```

This preset watches a narrow example downlink-style range and writes:

```text
logs/rf_base_activity.csv
logs/rf_base_readings.csv
```

## Run a Mobile/Burst Preset

```sh
python3 rf_signal_monitor.py --preset mobile --dashboard
```

This preset watches a narrow example uplink-style range and writes:

```text
logs/rf_mobile_activity.csv
logs/rf_mobile_readings.csv
```

Mobile or nearby transmitters are more likely to appear as short bursts, sudden
local spikes, or clusters that appear/disappear quickly. Treat detections as
probabilistic RF observations, not identity or content.

The dashboard includes a live time-series chart for the strongest reading in
the current `380-385 MHz` scan. The bright line is measured power and the faint
line is the rolling baseline.

When you pass something you want to correlate later, tap one of the field marker
buttons on the dashboard:

- `Vehicle visible`
- `Passed close`
- `Uncertain`

Markers are written to:

```text
logs/rf_mobile_observations.csv
```

After a drive, compare RF readings around each marker:

```sh
python3 analyze_drive.py \
  --readings logs/rf_mobile_readings.csv \
  --observations logs/rf_mobile_observations.csv \
  --window-seconds 30
```

## Custom Scan

Keep ranges narrow for responsive updates:

```sh
python3 rf_signal_monitor.py \
  --range 390M:395M:25k \
  --gain 30 \
  --interval 1s \
  --threshold-db 8 \
  --dashboard
```

The dashboard is available at:

```text
http://127.0.0.1:8765
```

## Logs

The activity log records incident state transitions:

- `start`
- `active`
- `end`

The readings log records the strongest bins each scan row, even when no
incident is active. This is the better file for replay, analysis, and future
model training.

Each row includes timestamp, frequency, measured power, rolling baseline,
delta above baseline, threshold settings, and incident state.

## Tuning

More sensitive incident detection:

```sh
python3 rf_signal_monitor.py --threshold-db 5
```

Fewer incidents:

```sh
python3 rf_signal_monitor.py --threshold-db 12
```

Ignore weak baseline jumps:

```sh
python3 rf_signal_monitor.py --incident-min-power-db -18
```

Adjust visual severity:

```sh
python3 rf_signal_monitor.py \
  --absolute-strong-db -10 \
  --absolute-extreme-db -5 \
  --dashboard
```

## Antenna Notes

Use a frequency-appropriate antenna, keep placement consistent, and calibrate
against your own environment. Absolute dB values from low-cost SDR scans are
best treated as relative readings unless you perform calibrated measurement.

## Legal Note

Keep usage to passive spectrum/power observation unless you have clear
authorization for anything beyond that. This tool is intended for RF sensing,
embedded systems experimentation, and field logging.
