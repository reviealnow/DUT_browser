# Wi-Fi Time-Series Extractor

`tools/wifi_timeseries.py` extracts Wi-Fi telemetry already present in sysMon
`CURL Hooks` sections. It is offline: it reads session logs and never contacts a
DUT, network endpoint, or serial port.

## What it extracts

- One summary row per complete CURL-hook cycle: clients, load, temperatures,
  radio utilization/channel, and workload byte counters.
- One row per Wi-Fi client per cycle, including radio, channel, signal,
  traffic, connection duration, and VLAN data.
- Time-series PNGs for client count, utilization, temperatures, and RSSI.

Radio markers are preferred when present. For older logs without markers, the
Nth client list is paired with the Nth `radio_name` payload in the same cycle.
If those counts differ, clients are retained with an empty radio field. A CURL
section truncated by EOF is discarded.

## Usage

Run from a session directory containing `.log` or `.txt` files:

```bash
MPLBACKEND=Agg MPLCONFIGDIR=/tmp/wifi-mpl \
  python3 /path/to/tools/wifi_timeseries.py
```

No arguments are required. Output is written to the current directory using
the same timestamp/firmware prefix convention as `analyzer3.py`.

## Output

- `{prefix}wifi_summary.csv`
- `{prefix}wifi_clients.csv` when client rows exist
- `{prefix}wifi_clients_plot.png`
- `{prefix}wifi_util_plot.png`
- `{prefix}wifi_temps_plot.png`
- `{prefix}wifi_rssi_plot.png` when client rows exist

If no complete CURL-hook section is found, the command exits successfully and
writes no files. If cycles exist but have no client rows, client CSV and RSSI
plot output are omitted.

## Extend parsing rules

Keep schemas, units, naming, empty-input behavior, and radio pairing aligned
with `tools/CONTRACT_wifi_context.md`. Add a synthetic fixture and a regression
test before accepting another producer spelling.
