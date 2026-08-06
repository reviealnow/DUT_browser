# Demo Kit

Self-contained HTML pages that show what DUT_browser does, to someone who has
no DUT, no serial port and no backend. `dut-dashboard/demo/`.

Demonstrating the real app needs an AP on the bench, a free serial port, both
servers running and `sysMon` alive on the device. That is not something you can
do in a meeting room, and it is not something you can email. Each page here is
one file — markup, styles, script and data inlined — so it opens by
double-click, works offline, and survives being forwarded.

## What's in it

| Page | Shows |
|---|---|
| `overview.html` | Fleet strip, KPI row, 40-hour CPU and client trends, channel recommendation, crash feed |

More screens are added one file at a time; see *Adding a screen*.

## Usage

Open a page — no server, no build step:

```bash
open dut-dashboard/demo/overview.html
```

Rebuild the baked-in data from a real bundle after a capture worth showing:

```bash
cd dut-dashboard/demo
python3 build_demo_data.py --bundle /path/to/dut-session-<ts>
```

Only the `<script id="demo-data">` block is rewritten, so hand-edits to the
markup survive a regeneration. When one bundle lacks a usable neighbour scan,
name a second source — both end up printed on the page:

```bash
python3 build_demo_data.py --bundle <long-session> --survey-bundle <session-with-a-survey>
```

A bundle is any extracted `dut-session-<ts>` directory from the **Download DUT
Log** flow: `build_demo_data.py` reads `*_cpu_usage.csv` (analyzer3),
`*_wifi_clients.csv` (`tools/wifi_timeseries.py`) and
`context/site-survey/*.json`.

## What is real and what is not

This matters more than the visuals: a demo that quietly overstates the product
creates work for whoever has to make it true later.

* **Measured, from a real DUT session** — CPU per core, associated-client
  counts over time, per-channel neighbour counts, the channel recommendation
  and its interference score, and the roaming client. Each page prints the
  bundle it came from in its footer.
* **Anonymised before commit** — every SSID, BSSID, MAC and IP. A neighbour
  scan sweeps up the networks of everyone in radio range, not just ours, so
  none of it may be committed as captured. The mapping is deterministic (a real
  value always becomes the same fake one, so the demo reads consistently) and
  distribution-preserving (counts per channel, band and timestamp are
  untouched). MACs get the `02:` locally-administered prefix and IPs land in
  RFC 5737 `198.51.100.0/24`, so both are visibly not real. Model names
  (`AP6_840E`) are deliberately kept — they are the product being shown.
* **Synthetic**, and kept in `demo-fixtures.json` away from anything measured —
  the fleet list (a one-DUT bench cannot produce a fleet) and the crash lines
  (the reference capture contained none).
* **Marked `◇ concept`** — an idea shown for discussion, not shipped
  behaviour. Everything without a chip mirrors what the product actually does.
  Fleet drag-to-reorder is a concept; Console / Close serial / Connect,
  Re-scan, the band tabs and Copy are real.

Bar heights in the channel chart are **raw neighbour counts**, as in
`SiteSurveyCard.tsx`; the recommendation's `occupancy` is a signal-weighted
interference score and travels as a number beside the chart, never as a bar.

## Adding a screen

1. Copy `overview.html`, keep the `<style>` block and the chart helpers
   (`lineChart` / `barChart`) — pages duplicate them on purpose, because
   "one file you can email" is the whole point and a shared asset breaks it.
2. Leave an empty `<script id="demo-data" type="application/json">{}</script>`
   for the generator to fill. Charts are hand-rendered inline SVG and carry
   their source data as JSON, matching the frontend rule in
   `dut-dashboard/CLAUDE.md` — do not reach for a charting library.
3. Extend `build(...)` in `build_demo_data.py` with whatever the new page
   needs, running every identifier through `Anonymiser` on the way out.
4. Mark anything the product does not do with a `◇ concept` chip.
5. Add the page to the table above.

## Known limits

* The pages are hand-written, so they can drift from the product. The footer
  names the bundle the data came from, but nothing enforces that the UI still
  matches — treat a screen as stale once the real one changes.
* `file://` blocks the clipboard in some browsers; the Copy button says so
  instead of failing silently.
* No build, no dependencies, and none should be added: the value of a demo file
  is that it opens anywhere.
