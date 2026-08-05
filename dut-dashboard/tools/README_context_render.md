# Context Render

`tools/context_render.py` renders a session bundle's Wi-Fi context snapshots
(`context/**/*.json`) as PNGs, so the Wi-Fi content of a downloaded ZIP is
readable without opening a JSON file.

Track B of `tools/CONTRACT_wifi_context.md`. Offline: no DUT, no serial port,
no network.

## What it renders

| Input (newest snapshot of that kind) | Output |
|---|---|
| `context/site-survey/site-survey-<dut>-<ts>.json` | `{prefix}survey_channels_2g4.png` / `_5g.png` / `_6g.png` — one bar chart per band that has a recommendation |
| `context/ssid-capability/ssid-capability-<dut>-<ts>.json` | `{prefix}ssid_capability.png` — table: iface, SSID, band, channel, security, PMF, gen |
| `context/wifi-clients/wifi-clients-<dut>-<ts>.json` | `{prefix}wifi_clients_table.png` — table: MAC, SSID, band, ch, RSSI, SNR, tx/rx rate, connected, vendor |

`{prefix}` is analyzer3.py's `<mmddHHMM>_<time_tag>_<fw_tag>_` (contract §5),
parsed from the session log's filename in the same directory. A directory with
no log still renders, using the `notime` / no-fw fallbacks.

The prefix is **adopted whole from analyzer3's own output** in the session
directory (`<prefix>cpu_usage.csv`, newest wins) — stamp and tags together —
so one bundle carries one prefix even when the tool sequence crosses a minute
boundary; without analyzer3 output (a standalone render) it is computed here
instead.

Only the **newest** snapshot per kind is rendered. `context/capture-report.txt`,
`.csv` siblings and `.skip.json` markers are not inputs and are ignored.

### Chart semantics (same as the live Site Survey card)

- Bar height = **raw count of neighbouring SSIDs on that channel**.
- Green **Best** = the recommended channel; red **Busy** = the busiest one
  (recommended wins when they are the same channel); `•` after the axis label
  = the channel the radio is on now.
- 2.4 GHz is always drawn on the full 1–13 grid; 5/6 GHz show the observed
  channels plus the current and recommended ones. The recommended channel is
  drawn even when nothing was observed on it — an empty channel is exactly
  what gets recommended, and a chart that hid it would hide the answer.
- The recommendation's `occupancy` **score** is a different number from the
  bars (signal-weighted, adjacent-channel aware on 2.4 GHz). It is printed in
  the note above the chart, never drawn as a bar.

## Usage

Run with the session directory as the working directory (how the backend
invokes it — contract §4):

```bash
cd dut-dashboard/logs/<session-dir>
MPLBACKEND=Agg python3 ../../tools/context_render.py
```

An explicit session directory works too, for standalone runs:

```bash
python3 tools/context_render.py path/to/session-dir
```

Try it on the committed fixtures:

```bash
mkdir -p /tmp/ctx-demo
cp -R dut-dashboard/backend/tests/fixtures/context_render/context /tmp/ctx-demo/
touch "/tmp/ctx-demo/dut-session-1.9.300_101500_20260803.log"
cd /tmp/ctx-demo && MPLBACKEND=Agg python3 <repo>/dut-dashboard/tools/context_render.py
```

Expected: five PNGs, one `[OK]` line each.

## No input means no file

A missing `context/` directory, a missing kind, an unreadable JSON, or a
payload whose lists are empty produces **no PNG for that kind** — no
placeholder, no empty table, no zero-bar chart. The reason goes to stdout and
the exit status stays 0 (contract §7). This is the whole point of the tool's
existence: a 40-hour run once shipped three empty snapshots that were
indistinguishable from real measurements of zero.

Per artifact, not per bundle: a `wifi-clients` capture that saw VAPs but no
associated clients renders nothing, while the site survey in the same bundle
still renders.

## Structure

Pure data prep, then a thin render layer — the tests assert the prep output,
never pixels:

| Function | Returns |
|---|---|
| `latest_snapshot(context_dir, kind)` | newest matching snapshot path, or None |
| `prepare_survey_bands(payload)` | one chart dict per band: `channels`, `counts`, `current/recommended/busiest_channel`, `occupancy` |
| `bar_roles(chart)` | `recommended` / `busiest` / `plain` per bar |
| `prepare_capability_table(payload)` / `prepare_clients_table(payload)` | `{columns, rows}` of already-formatted strings |
| `render_band_chart` / `render_table` | write one PNG |
| `render_session(session_dir)` | orchestrates; returns the paths written |

The prefix helpers (`extract_time_tag`, `fw_triplet_to_tag`, `extract_fw_tag`)
are **copied** from `analyzer3.py`, deliberately. Do not replace them with
`import analyzer3`: that module has no `__main__` guard, so importing it runs
the whole analyzer (contract §4).

## Tests

```bash
cd dut-dashboard/backend && python -m pytest tests/test_context_render.py
```

Fixtures are synthetic (fabricated MACs and SSIDs) and laid out exactly as a
bundle's `context/` directory under
`backend/tests/fixtures/context_render/context/`.
