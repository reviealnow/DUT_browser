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
| `site-survey.html` | Per-band channel charts over a real 2,438-observation scan, band filter, SSID/BSSID search, the full neighbour table |
| `wifi-clients.html` | The per-client table with row-expand deep stats, grouped by band, with Kick |
| `files.html` | The workspace file table with drag-and-drop upload, sortable columns, tags, inline preview |
| `bulletin.html` | Notes with nested replies, per-author colours, the edited marker and the unverified badge |
| `downloads.html` | A real bundle's four cards — session log with its context, analyzer outputs with an inline plot, surveys, connect-time context |
| `serial-console.html` | Monitor and Terminal, the popup command editor, and real console output |
| `cpu-memory.html` | The three trend cards over 40 hours of analyzer CSV — no anonymising needed |
| `ssid-capability.html` | 28 VAPs of hostapd config, and the all-miss state a capture with no host-side scan really has |
| `firmware.html` | The admin flash flow: transport, checksum gate, dry run, type-the-name confirm |
| `index.html` | The kit's front door — every screen, and which are measured versus synthetic |

Files and Bulletin are **two separate sections in the product**, so they are two
separate files here. Folding them into one "Workspace" page would invent a
screen the app does not have.

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

Pages whose content is synthetic in full take no bundle at all:

```bash
python3 build_demo_data.py --page files.html
python3 build_demo_data.py --page bulletin.html
```

**Ten of the eleven pages are generated. `index.html` is not** — it is the front
door, linking the screens and saying which are measured, with no capture behind
it and no `demo-data` block to fill. Edit it by hand; the generator refuses it by
name rather than failing on the missing block.

Downloads lists a bundle rather than parsing one, and embeds **every plot it
produced**, so each inline preview is the real output rather than a note
explaining its absence:

```bash
python3 build_demo_data.py --page downloads.html --bundle <session-dir>
```

**Which images may travel is an allowlist, because a PNG cannot be aliased.**
`EMBEDDABLE_PLOTS` names the plot kinds whose pixels are known to carry no
identifier, and that is knowable rather than eyeballed: `analyzer3.py` never
reads an SSID, BSSID, MAC, IP or hostname field at all, `wifi_timeseries.py`
labels its series with fixed strings, and `context_render.py`'s band charts plot
per-channel counts. A kind on neither list is **withheld** and the page says so,
so a plot added later is not published on the strength of nobody having looked.

Two artifacts are not plots but **tables rendered to pixels** —
`ssid_capability.png` holds the DUT's VAP names and `wifi_clients_table.png`
holds associated clients' MACs, SSIDs and vendor OUIs. `Anonymiser` cannot reach
a PNG and no text scan would ever flag one, so those two are **redrawn from the
same snapshot by `context_render`'s own renderer** with the identifiers replaced,
and carry a `◇ redrawn` chip saying so. Row count, columns and order are the
capture's; only the identifiers differ. That is also why the generator can import
`tools/context_render.py`: the demo's copy of a product artifact should be drawn
by the product's code, not by a lookalike maintained here.

The page comes to about 1.8 MB with thirteen images inlined — still one file you
can email, and the earlier claim that inlining them all would stop it being that
was simply wrong.

Its "peek" shows the log's real last lines, and Serial Console shows a real
run of them. A serial log is free text and cannot be aliased field by field, so
the generator **refuses** any excerpt carrying an SSID, MAC or IP rather than
shipping it (`refuse_if_identifying`). Serial Console goes further and lets the
guard *choose*: it takes the longest identifier-free run in the log, preferring
one that starts at a sysMon section header, so the excerpt is safe by
construction rather than by a lucky offset.

```bash
python3 build_demo_data.py --page serial-console.html --bundle <session-dir>
```

Two things on that screen this file cannot be, both chipped: the product's
Terminal is **xterm.js over a pty on `/ws/term`** — vi and nano really run on
the DUT — so the demo replays a recorded session instead; and the popup command
editor is **CodeMirror with Vim mode**, where the demo has a plain textarea.
Everything else there is the real interaction.

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

  This is pseudonymisation, not encryption. The page does not contain the
  original-to-alias mapping, but the hash is deterministic and unsalted, so
  anyone already holding the original capture can re-derive it by guessing over
  the candidate set. The goal is that identifiers are never published — not that
  someone with the source bundle is defeated.
* **Synthetic**, and kept in `demo-fixtures.json` away from anything measured —
  the fleet list (a one-DUT bench cannot produce a fleet), the crash lines (the
  reference capture contained none), the DUT-status tile (connection state is
  live UI no capture records), and **all** of `files.html` and `bulletin.html`.
  A file list and a note board are *content*, not measurement, so there is no
  measured claim to keep faithful; and the real ones on this bench are test
  scaffolding carrying colleagues' names, which is not something to publish.
  Both pages say so in their own provenance line.
* **Matched to an observable contract, not copied line by line.** Five review
  rounds settled where the line sits. These must match the product exactly,
  because a difference misrepresents what it can do: whether a control or
  capability is present at all; what it changes; **which inputs it accepts or
  refuses** (a demo that accepts what the product rejects has crossed the
  line); the blast radius of anything outward-facing — delete, copy, upload;
  whether a value is measured, synthetic or concept; and any ordering, limit or
  persistence a viewer would read as a product capability. These may differ:
  the shape of feedback (a toast where the product uses an icon), spinner
  durations, focus placement, animation, keyboard shortcuts that change no
  outcome, spacing and non-load-bearing microcopy.

  The failure mode runs **both ways**, and only one of them has a chip.
  Over-showing: a preview offered for a type the product will not render, a
  field accepting more characters than the product allows — by the test above,
  anything the demo accepts and the product refuses lands here. Under-showing:
  a cruder editor, a two-state toggle where the product cycles three, controls
  left off a card entirely. A `◇ concept` chip catches the first kind and is
  blind to the second. **Open the component. Every finding came from a control
  or state that was not read first.**
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

**Start at `index.html`.** It links every screen and states, per tile, whether
that page is measured or synthetic.

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
* `firmware.html` cannot reach anything, and says so — but it keeps one rule
  exactly: the management API takes the **encrypted** image and the web-UI path
  takes the **signed** `.sig`. That rule is a filename *pattern*
  (`ubi_kernel_AP6_*-encrypt_*.bin`), not a suffix; encoding it as a suffix
  matches no real filename and silently makes that path undemonstrable.
