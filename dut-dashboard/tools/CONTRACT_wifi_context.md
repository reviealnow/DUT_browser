# Contract: offline Wi-Fi context tooling (Tracks A / B / C)

Fixed interface for parallel implementation by two cold-start agents
(Track A: GPT-5.6 Sol · Tracks B/C: Claude Opus 5). This file is the agreement
that makes parallel work possible: schemas, units, output names, invocation
convention, and file ownership are decided HERE, before any implementation.
Change this file only before implementation starts; after that, a change here
invalidates in-flight work and needs the operator's sign-off.

## 1. Why the data comes from the log, not a serial capture

sysMon saturates the 115200-baud serial line for the whole run, so any serial
capture racing it is starved. Measured on a real 40-hour AP6 420E run: the
connect-time context capture retried 11 times over 97 s (sentinel echoes
visible in the log) and never got a single round-trip; the three `context/`
snapshots in that bundle were empty containers written as if they were
measurements. Meanwhile sysMon's own `curl_hooks()` dumps the DUT management
REST API (`https://127.0.0.1:10443/ap/info/*`) into the session log on every
cycle — 1572 cycles in that same bundle, carrying connected clients, per-radio
channel utilization, temperatures, and the full per-radio client list.

The offline tools below parse that log. **Do NOT "fix" missing Wi-Fi context
by adding periodic serial polling** — it will starve exactly the same way
(`dut-dashboard/CLAUDE.md`, "On-demand serial RPC discipline").

## 2. Decisions already made (operator, 2026-08-03)

- **All output images are PNG.** No HTML report. Small tables (SSID
  capability ~9 rows, clients ~6 rows) render as PNG tables; the full
  neighbor list stays in CSV (searchable/copyable in a spreadsheet).
- **Raw data is retained**: per-kind JSON (lossless source of truth, input to
  `context_render.py`) and CSV (human-searchable flat form). No third text
  format.
- **Empty payload ⇒ no output file, in every format** (JSON, CSV, PNG). A
  header-only CSV or an empty-list JSON presented as a measurement is the bug
  this work removes. The reason a snapshot is missing goes into
  `context/capture-report.txt` (Track C1).
- **C2 (write-through) signed off 2026-08-03 by the operator; implemented in
  this PR.** `/api/wifi/clients`, `/api/wifi/capabilities`,
  `/api/wifi/capability-report`, `/api/wifi/site-survey` and
  `/api/wifi/channel-recommendation` now persist a successful result as a
  snapshot, so the scans that *do* get through the saturated line (§1) reach
  the bundle instead of living only in the frontend. Best-effort and invisible:
  the response is identical whether the write succeeds, fails, or (empty
  payload) writes nothing. No `.skip.json` markers — those stay reserved for the
  connect-time capture path, the only caller entitled to assert "a capture was
  attempted and produced nothing". No dedup, no rate limiting, no retention
  policy: deferred, deliberately. Selection and bundling are unchanged, so every
  in-window snapshot ships and `context_render.py` renders the newest per kind.
- The empty-payload rule above now also binds `survey_snapshot.write_snapshot`,
  which previously always wrote a JSON+CSV pair: a survey that observed nothing
  writes no file, and one with VAPs but no neighbours keeps its JSON and writes
  no header-only neighbour CSV (same per-artifact rule as §7).

## 3. Modules and ownership

| File | Owner | Notes |
|---|---|---|
| `tools/wifi_timeseries.py` (new) | **A** (GPT-5.6 Sol) | Track A parser + plots |
| `tools/README_wifi_timeseries.md` (new) | **A** | follows `README_log_event_detector.md` layout |
| `backend/tests/test_wifi_timeseries.py` (new) | **A** | |
| `backend/tests/fixtures/wifi_timeseries/` (new) | **A** | synthetic log slice, see §8 |
| `backend/app/api/serial_api.py` — ONLY `run_analyzer_for_session()` + a module-level `OFFLINE_TOOLS` list | **A** | generalize single script → tool loop; wire BOTH new tools (B's filename is fixed here, so A wires it before B's tool exists). **Superseded by C2c:** the two hand-synced copies of that list (here and in `analyzer_service.py`) are now one `OFFLINE_TOOL_NAMES` in `backend/app/config.py`. `analyzer3.py` is not in it — it is `ANALYZER_SCRIPT`, the fail-hard primary and the test patch seam; as a list entry it was decoration both call sites sliced off. |
| `backend/app/services/analyzer_service.py` — ONLY the tempdir staging list | **A** | second, independent invocation point (Analyze endpoint); skipping it forks behavior between Download and Analyze flows |
| `tools/context_render.py` (new) | **B** (Opus 5) | Track B renderer, context/*.json → PNG |
| `tools/README_context_render.md` (new) | **B** | |
| `backend/tests/test_context_render.py` (new) | **B** | |
| `backend/tests/fixtures/context_render/` (new) | **B** | non-empty sample JSONs |
| `backend/app/services/context_snapshot.py` | **C** (Opus 5) | C1: empty-write guard + capture report |
| `backend/app/main.py` — ONLY the `capture_dut_context()` closures | **C** | |
| `dut-dashboard/CLAUDE.md` module map | pre-updated in the contract PR | neither A nor B touches it |
| `backend/requirements.txt` | **NOBODY** | matplotlib + pandas already present; no new dependencies |

Anything not listed: do not touch. Each agent works in its own git worktree
(`git worktree add -b <branch> ../DUT_browser-<purpose> origin/CPU_Plots`) and
opens its own PR against `CPU_Plots` (squash merge).

## 4. Invocation convention (same as analyzer3.py)

Run as a subprocess with `cwd` = the session directory; no CLI args required
(optional args for standalone use are fine); the caller provides
`MPLBACKEND=Agg` and `MPLCONFIGDIR`. Read inputs from cwd, write outputs to
cwd. Exit 0 on "no usable input" (and write NOTHING — §7); nonzero only on
crash. `zip_session_dir()` sweeps every file under the session dir, so no
ZIP-side change is needed.

**Do not `import analyzer3`** — it has no `__main__` guard; importing it
executes the whole script. Copy its three prefix helpers instead (§5); the
duplication is a known, accepted trade-off recorded here so nobody
"deduplicates" it into an unsafe import.

## 5. Output naming

Reuse analyzer3.py's prefix exactly: `<mmddHHMM>_<time_tag>_<fw_tag>_`
(run time; time tag and fw tag parsed from the log filename; `notime` /
absent-fw fallbacks identical — copy `extract_time_tag`, `extract_fw_tag`,
and the prefix assembly from `analyzer3.py:29-67`).

Track A outputs (session dir root):

    {prefix}wifi_summary.csv        {prefix}wifi_clients.csv
    {prefix}wifi_clients_plot.png   {prefix}wifi_util_plot.png
    {prefix}wifi_temps_plot.png     {prefix}wifi_rssi_plot.png

Track B outputs (session dir root):

    {prefix}survey_channels_2g4.png / _5g.png / _6g.png
    {prefix}ssid_capability.png     {prefix}wifi_clients_table.png

Track C output:

    context/capture-report.txt      (plain text, one line per snapshot kind:
                                     "<kind>: ok, <n> rows, <files>" or
                                     "<kind>: skipped — <reason>, <timestamp>")

C2b appends one further line per *failed best-effort offline tool*, after the
per-kind block (`bundle_session_context` writes the block before the analyzer
loop runs, so these are appended, never a rewrite):

    offline-tool <name>: failed — <first line of stderr, or the exception>, <ISO timestamp>

Only the Download flow produces this. `analyzer_service.py` (the Analyze
endpoint) runs the same tool loop but builds no bundle and therefore has no
capture report to append to, so a failure there stays a server-side log line —
a deliberate asymmetry, not an oversight.

## 6. CSV schemas (exact columns, exact units)

Missing value = **empty field**. Never 0, never "NA", never a unit suffix.
All values integers unless stated. Timestamps are the DUT's own clock
verbatim, format `YYYY-MM-DD HH:MM:SS` (the DUT clock has been observed ~8 h
off host time; do not convert — host-time correlation is out of scope).

### `{prefix}wifi_summary.csv` — one row per sysMon cycle

| column | unit / domain |
|---|---|
| `ts` | `YYYY-MM-DD HH:MM:SS` (DUT clock) |
| `cycle` | int, sysMon "Test Time" counter |
| `connected_clients` | int |
| `cpu_load_pct` | int 0–100 |
| `mem_load_pct` | int 0–100 |
| `cpu_temp_c` | int °C, max across cores |
| `radio_temp_c` | int °C, max across radios |
| `util_2g4_pct` / `util_5g_pct` / `util_6g_pct` | int 0–100, empty if radio block absent |
| `chan_2g4` / `chan_5g` / `chan_6g` | int channel number, empty if absent |
| `tx_bytes` / `rx_bytes` | int bytes (upstream API pre-rounds, e.g. "20.3 GBytes"; precision is inherited — good for trends, not byte accounting) |

### `{prefix}wifi_clients.csv` — one row per client per cycle

| column | unit / domain |
|---|---|
| `ts` | `YYYY-MM-DD HH:MM:SS` (DUT clock) |
| `cycle` | int |
| `radio` | `2.4G` \| `5G` \| `6G` \| empty (pairing ambiguous — row kept, radio not guessed) |
| `ssid_name` | string verbatim |
| `mac_address` | string verbatim |
| `vendor` | string verbatim |
| `ip_address` | string verbatim |
| `chan` | int |
| `chan_width_mhz` | int (from "20MHz" → 20) |
| `rssi_dbm` | int, negative |
| `snr_db` | int |
| `signal_ratio_pct` | int 0–100 (upstream JSON spells it `signale_ratio` — map it) |
| `tx_bytes` / `rx_bytes` | int bytes (pre-rounded upstream, as above) |
| `connected_secs` | int seconds (from "4 days 23 hours 47 mins 4 secs") |
| `idle_secs` | int seconds |
| `vlan_id` | int |

**Radio pairing rule:** prefer the `--- CLIENTS Radio=<band> ---` marker
(current `scripts/sysMon.sh`); when absent (older DUT builds — the reference
log is one), pair the Nth `client_list` with the Nth `radio_name` block of the
same cycle (`curl_hooks()` order is fixed: 2.4G / 5G / 6G). On count mismatch
leave `radio` empty. The tool MUST print a channel-vs-radio consistency count
on stdout (reference log: 8011/8011).

**Section discipline:** JSON lines are collected ONLY inside a
`=== CURL Hooks` section — a concurrent serial capture can interleave its own
output into it (observed in the reference log: hostapd-conf dump lines inside
the section). A cycle truncated by EOF is dropped, not partially reported.

## 7. Empty-input rule (all tracks)

If a tool finds no usable input, it writes **no output file at all** and says
why on stdout, exit 0. This applies per-artifact too: a log with CURL hooks
but no client rows produces `wifi_summary.csv` and its plots but neither
`wifi_clients.csv` nor the RSSI plot.

## 8. Test fixtures

Synthetic and small (≤20 KB), fabricated MACs/SSIDs, committed under
`backend/tests/fixtures/`. **Never commit a real DUT log or its derived
CSVs** (`logs/` and `data/` are gitignored; `git add -A` is unsafe in this
tree — stage explicit paths and re-read `git status --porcelain` after
staging). Acceptance against the real 40-hour reference log runs locally; the
PR states the numbers obtained. Reference-log baseline:

    snapshots parsed 1572 · with workload 1564 · client rows 8011
    per radio: 2.4G 3233 · 5G 4778 · 6G 0 · channel consistency 8011/8011

## 9. Gates

`pytest` (from `dut-dashboard/backend`; baseline was 439 when this file was
written, 493 after A/B/C1, 517 after C2) and
`npm run typecheck` (from `dut-dashboard/frontend`) green at every commit.
Each PR names the mutation it ran to prove its test bites (e.g. Track A:
swap the radio-pairing rule to zip-shortest → the radio-distribution
assertion must go red).
