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
| `overview.html` | Fleet strip, KPI row, 40-hour CPU and client trends, the cached channel recommendation, crash feed |
| `site-survey.html` | Per-band channel charts over a real 2438-network scan, band filter, SSID/BSSID search, the full neighbour table |

More screens are added one file at a time; see *Adding a screen*.

Overview shows only the **cached** recommendation, as `OverviewBandReco` does;
the chart, the band filter and Re-scan live on Site Survey, where the product
puts them. Keeping that split is what stops the kit inventing an Overview the
app does not have.

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
  none of it may be committed as captured. Two properties hold, and
  `backend/tests/test_demo_anonymiser.py` pins both because both were defects
  first: the mapping is **order-independent** (aliases are assigned over the
  sorted set, so the same bundle always yields the same page) and **injective**
  (distinct networks never share an alias — collapsing two would understate how
  crowded the air is, which is the measurement these pages exist to show).
  Counts per channel, band and timestamp are untouched; only labels change.
  MACs get the `02:` locally-administered prefix and IPs land in the RFC 5737
  documentation ranges, so both are visibly not real. Model names (`AP6_840E`)
  are deliberately kept — they are the product being shown.

  This is pseudonymisation, not encryption. The page cannot be reversed, but a
  deterministic unsalted hash over a small candidate space is guessable by
  anyone already holding the original capture. The goal is that identifiers are
  never published — not that someone with the source bundle is defeated.
* **Synthetic**, and kept in `demo-fixtures.json` away from anything measured —
  the fleet list (a one-DUT bench cannot produce a fleet) and the crash lines
  (the reference capture contained none).
* **Marked `◇ concept`** — an idea shown for discussion, not shipped
  behaviour. Everything without a chip mirrors what the product actually does.
  Fleet drag-to-reorder and drag-to-filter on a channel chart are concepts;
  Console / Close serial / Connect, Re-scan, the band filter, the SSID/BSSID
  search, click-a-bar-to-preview and Copy are real.

  Re-scan **replays the one captured scan** rather than generating a second set
  of numbers. Manufacturing measurements to make a button look livelier would
  erase the line between measured and synthetic that the rest of this section
  draws.

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
3. Add a builder to `build_demo_data.py` (`PAGE = {"your-page.html": ...}` in
   `main`), calling `Anonymiser.prepare(...)` once with every identifier the
   page will emit before any of them is written out — that up-front pass is
   what makes the aliases order-independent.
4. Mark anything the product does not do with a `◇ concept` chip.
5. Add the page to the table above.

## Known limits

* The pages are hand-written, so they can drift from the product. The footer
  names the bundle the data came from, but nothing enforces that the UI still
  matches — treat a screen as stale once the real one changes.
* Copy mirrors `utils/clipboard.ts`: the Clipboard API first, then a hidden
  textarea + `execCommand`, so it also works on plain-HTTP LAN origins and
  `file://` where the API is unavailable.
* The kit ships one captured scan per screen, so Re-scan replays it — see
  *What is real and what is not*.
* No build, no dependencies, and none should be added: the value of a demo file
  is that it opens anywhere.
