# Host RF check and Packet Monitor — design

Status: **direction changed on 2026-11-03** (§A2). Part A is the new phase 1
and is not implemented yet. Part B is the tshark Packet Monitor, reviewed
earlier in this PR, now deferred to phase 2; its Test Modes, decoders and
findings rules are already implemented in #214 and are reused by Part A.

# Part A — Host RF check (phase 1)

## A1. Goal

Let an admin compare **the RF environment around a Host** — the Host is
usually deployed next to the DUT — with what the DUT is configured to do, and
validate the DUT against its SKU from the air. In the team's order of priority:

1. **Legacy security** around the DUT and on it: WEP, WPA (v1), TKIP, open.
2. **The DUT's country code against its SKU**: expected from the DUT,
   observed in the DUT's own beacons.
3. **Channel occupancy**: BSS count per channel, and the neighbours on the
   DUT's own channel with their signal strength.

Further SKU checks follow the same expected-vs-observed shape (§A7).

## A2. Why the direction changed

A field check on #214 ran the findings rules over a real neighbour scan from
a bench Host: a Raspberry Pi 4, one `iw dev wlan0 scan`, 86 BSSes. The result
was **0 fail, 0 warn, 4 info**. Every WPA3 BSS in the lab was configured
correctly, while the scan held 1 WEP, 10 WPA/WPA2-mixed or TKIP and 2 open
networks, and country codes from nine countries. None of those were in the
rule set. The same check found:

- **A beacon pcap needs monitor mode, and the Pi 4's built-in radio has
  none.** It supports `IBSS, managed, AP, P2P-*` only, so the tshark path
  would need a second adapter on every Host.
- **A managed-mode `iw scan` already carries every column the table needs.**
  The kernel splits Multiple BSSID into separate BSSes, and `iw` reports the
  channel the AP advertises, so neither problem fixed in #214's review arises.
- **This overlaps existing code.** SSID Capability already joins the DUT's
  config (read over serial) with an over-the-air `iw` scan on BSSID
  (`services/capability_report.py`). Building the same join again in Packet
  Monitor would be a second way of doing one thing.

Decisions (2026-11-03):

- **Phase 1 extends SSID Capability.** It runs the existing scan on the
  DUT's Host, adds country, channel and power fields, and reuses #214's
  findings rules. The tshark Packet Monitor (Part B) moves to phase 2, for
  per-beacon evidence.
- **The SKU's expected values are read from the DUT console.** This needs no
  maintenance, but it cannot catch a DUT whose own setting is wrong (§A7).
- **The legacy-security rules go into #214**, which is done:
  `legacy_security` and `open_network`.

This also settles the question raised before Part B's transport work
started. These checks extend the existing SSID Capability path (scan on the
Host, more fields) and do not live in a second pipeline in Packet Monitor.
Packet Monitor keeps only what a scan cannot give: per-beacon evidence.

## A3. What already exists

| Piece | Where | What it does today |
|---|---|---|
| Host-side scan | `services/wifi_survey.py` | `iw dev $SURVEY_WIFI_IFACE scan` **on the dashboard server**, with an `nmcli` fallback; parses BSSID, freq, signal, SSID, generation, RSN (AKM names, ciphers, PMF) and 11k/v/r |
| DUT config | `services/wifi_clients.get_ssid_capabilities` | per-VAP config over the serial console (`capture_command`) |
| Reconciliation | `services/capability_report.py`, `GET /api/wifi/capability-report` | joins the two on BSSID and diffs security, PMF, generation and 11k/v/r |
| DUT-side neighbour scan | `services/site_survey.py` | `iw dev <vap> scan` **on the DUT**, using the same parser; `channel_recommendation()` scores channels |
| DUT → Host link | `dut/registry.py` | a collector-backed DUT console records its `collector_id` |
| Command on a Host | `collector/ssh_session.CollectorSession.run` | runs one command over the held-open session; per-call timeout; serialised by an RPC lock |
| Findings | `services/packet_rules.py` (#214) | row rules over plain dicts; nothing in them is tshark-specific |

## A4. Flow

```
GET /api/wifi/capability-report?dut=<id>
  │
  ├─ Source A  DUT console (existing) + country/regulatory capture (§A7)
  │
  ├─ Source B  scan on the DUT's Host
  │     DUT has collector_id  → CollectorSession.run("iw dev <iface> scan", timeout=25)
  │     no collector          → local SURVEY_WIFI_IFACE (today's behaviour)
  │     → wifi_survey parser (extended, §A6)
  │
  ├─ join on BSSID (existing) + SKU checks on the DUT's own BSSes (§A7)
  ├─ findings on every scanned BSS: packet_rules over adapted rows (§A8)
  └─ channel occupancy (§A9)
```

Everything new in the response is **additive**; existing fields keep their
meaning, so `SsidCapabilityCard` and the demo keep working (§A10).

## A5. Scanning on the Host

- **Which interface.** A new admin-set collector property,
  `survey_iface`. It is validated as `[A-Za-z0-9_.-]{1,15}` **and** against the
  Host's own `iw dev` list when set. It is the only variable in the command,
  so `CollectorSession.run` is enough. That session `exec`s `sh`, so the
  command is parsed by `sh`, never by the login shell, and Part B's base64
  transport is not needed here.
- **Permission.** `iw … scan` needs `CAP_NET_ADMIN`. The Host probe (run on
  connect) tries `iw dev <iface> scan` once and reports one of `ok`,
  `permission denied` (with the `setcap` or `sudoers` line to fix it), or
  `no such interface`. A Host that fails is shown as such; the report falls
  back to "Source B unavailable: <reason>", which `capability_report` already
  supports.
- **Errors are not scan output.** `run()` appends `2>&1`, so a failing `iw`
  writes its message into the text that comes back. The parser runs only when
  the exit status is 0. Any other status becomes "Source B unavailable", with
  the first line of that output as the reason.
- **Monitor mode is not needed** for this part. The probe still records
  whether `iw list` shows `monitor`, since phase 2 depends on it.
- **No background polling.** A scan runs only when an engineer presses
  Refresh (§A10, Q6): never on a timer, and never just because someone opened
  the page. This is the same discipline as serial captures. The Host is
  shared, and an active scan takes its radio off-channel. Results are stored
  per DUT with the Host and the time, and the UI shows how old they are.
- **Lock time.** A scan takes about 3–10 s and holds the collector session's
  RPC lock while it runs, so a console probe on the same Host waits behind it.
  The call uses `timeout=25` rather than the default 15 s, and that wait is
  accepted.
- **Wi-Fi uplink warning.** If `survey_iface` is also the Host's default
  route, scanning interrupts the Host's own traffic. The probe detects this
  (`ip route show default`) and warns the admin when they set the interface.

## A6. Parser extension (`services/wifi_survey._parse_iw_scan`)

New fields per BSS. Each is `None` when the element is absent, and none of
them raises:

| Field | From the `iw scan` text |
|---|---|
| `privacy` | bit 4 of the `capability:` hex value; the word `Privacy` only when there is no hex |
| `wpa1` | a `WPA:` block, with its own version, group cipher, pairwise ciphers and AKMs |
| `country`, `country_environment` | `Country: XX	Environment: …` |
| `country_channels` | the `Channels [a - b] @ N dBm` triplets under Country |
| `power_constraint_db` | `Power constraint: N dB` |
| `tpc_tx_power_dbm` | `TPC report: TX power: N dBm` |
| `beacon_interval_tu` | `beacon interval: N TUs` |
| `dtim_period` | the `TIM:` line (`DTIM Period N`) |

The exact `iw` spelling of each line is fixed against **real output captured
from a bench Host** (a Pi 4, `iw` version recorded), committed as an
anonymised fixture. The current fixtures are hand-written, and §A12 shows
what that has already cost. Site Survey reuses this parser on the DUT's
busybox `iw`, so it gains the same fields, and its tests must stay green.

**Status.** #216 (merged) added `privacy`, `wpa1`, `country`, the advertised
channel, beacon interval, DTIM and scan age. Still open: `country_environment`,
`country_channels`, `power_constraint_db` and `tpc_tx_power_dbm`.

**The DUT's busybox `iw` names only `ESS`** on the capability line
(`capability: ESS (0x0431)`) and prints no WPA element. That is why `privacy`
comes from the hex value. It also means WEP and WPA1 look alike there.
Decided in #216:

- Site Survey labels "Privacy, no RSN" as `WEP` on every `iw`, the DUT's
  included. A WPA1 BSS seen by the DUT is therefore labelled `WEP`; that is
  accepted over a `WEP or WPA1` hedge. Before #216 the DUT showed these as
  `Open`.
- The `legacy_security` WEP *fail* stays silent where `iw` cannot show WPA:
  `services/scan_rows.py` leaves the WPA keys out of the row, so the rule reads
  them as "this source cannot see the field".

#216 also fixed `_freq_to_channel`: every 6 GHz channel was one too high, and
2484 MHz gave 15 instead of 14. Site Survey shows these numbers.

## A7. SKU checks (the DUT's own BSSes)

The existing BSSID join already says which scanned BSSes are the DUT's. On
those, and only those, the report compares **expected** values from the DUT
console with **observed** values from the air. Neighbours' country codes are
context, never findings: one bench tests several SKUs, so a US SKU in Taiwan
advertising `US` is correct.

| Check | Expected (DUT console) | Observed (DUT's beacons) | Severity |
|---|---|---|---|
| Country code | the DUT's regulatory **domain**, mapped to its allowed countries | Country element | fail if the code is not one of the domain's countries |
| Channel allowed | the DUT's own regulatory rules, if its console exposes them; otherwise **blocked on Q8**, because no regulatory table exists in this repository. Not `iw reg get`: on the bench AP6 it answers `country 00: DFS-UNSET` while the driver beacons `US`, so it does not reflect the SKU (Q7) | operating channel; Country element channel triplets | fail |
| Power within limit | regulatory max EIRP per range | Country element max power, Power Constraint, TPC | warn (the observed value is advertised, not measured) |
| Every VAP on air | configured VAPs (existing Source A) | BSSID present in the scan (existing join) | warn, unless the band is not scannable from the Host (existing caveat) |
| Security as configured | existing Source A security / PMF | RSN, WPA (v1), Privacy | existing diff, plus the §A8 legacy rules |

**An AP6 SKU is a regulatory domain, not a country** (found in #216,
corrected in #219). An AP6 unit carries one of six regulatory domains (0 US,
1 CA, 2 EU, 3 AU/NZ, 4 IN, 5 Rest of World), read on its console with
`board_config get domain`, and each domain allows a list of countries. So
"expected" is the domain, and the check asks whether the observed Country
element is one of that domain's countries. A US-advertising unit passes
domain 0 and fails domain 1 or 2. On the bench, an AP6_420 US SKU answers
`0` and advertises `US`, which passes.

- The domain → countries list is the SKU country list (Sophos
  KBA-000009623), transcribed into `dut_regdomain.py` in #216. It maps a
  *domain* to countries; it is not a per-model SKU table, so Q8 stays out of
  phase 1.
- The public KBA lists US and CA as separate SKUs but prints no domain
  numbers. US = 0 is confirmed on the bench. CA = 1 and the other numbers
  come from the QA team's copy of the list and have not been read from a
  unit of that SKU yet.
- `board_config get domain` is pinned to captured AP6 console output, and
  was read live on 2026-11-04 (Q7).
- AP7's domain command is not known yet. An AP7 or an unknown domain number
  is left unchecked, not guessed.

**Known limit, accepted by the decision.** An expectation read from the DUT
cannot catch a DUT whose own domain is set wrong: both sides follow the same
wrong setting. A model/SKU → domain table would catch that, but someone has to
own it. It is out of phase 1, and the report says which source "expected"
came from.

## A8. Findings on every scanned BSS

`services/packet_rules.py` (#214) already contains the rules the field check
asked for: `legacy_security`, `open_network`, the WPA3/PMF rules,
`six_ghz_security`, `hidden_ssid` and `beacon_timing`. They read plain dicts,
so an adapter maps each parsed BSS onto the row keys the rules read:

| Row key | From the scan |
|---|---|
| `akm` | AKM names → suite numbers (`PSK` 2, `SAE` 8, `FT/SAE` 9, `IEEE 802.1X` 1, `OWE` 18, raw `00-0f-ac:N` → N; unknown names → `None`, so rules skip) |
| `group_cipher`, `pairwise_cipher` | cipher names → suite numbers (`TKIP` 2, `CCMP` 4, `GCMP-256` 9, …) |
| `mfpr`, `mfpc` | `MFP-required` / `MFP-capable` |
| `privacy`, `wpa_version`, `wpa_group_cipher`, `wpa_pairwise_cipher` | §A6 |
| `freq`, `ssid`, `beacon_interval`, `dtim_period` | parsed fields |

The adapter always emits every key above. A BSS with no RSN block gets
`akm: None` (key present), and one with no WPA block gets
`wpa_version: None`. The rules read "key absent" as "this source cannot see
the field" and stay silent, so leaving keys out would make open and WEP
networks disappear from the findings.

Not applied to a scan: `bss_config_changed` (one scan is one sample per BSS)
and `rsn_not_evaluated` (the kernel already splits Multiple BSSID).

The rules move from "Packet Monitor's" to shared: the adapter imports them,
and they stay data-agnostic. Renaming `packet_rules` to something like
`wifi_rules` is cosmetic and is left until Part B is built.

## A9. Channel occupancy

`occupancy(neighbors, own_vaps)` goes into `services/site_survey.py`, next to
`channel_recommendation()`, so the DUT-side and Host-side views share one
implementation. Per band and channel it returns:

- the BSS count;
- the strongest neighbour (SSID, BSSID, signal);
- whether the channel is one of the DUT's own;
- for each DUT channel, every neighbour on it, sorted by signal.

2.4 GHz overlap uses the existing `_overlap_weight_24g()` rather than a second
weighting.

## A10. API and UI

- `GET /api/wifi/capability-report` gains these **additive** fields:
  `source_b.host` (collector id and label, or `"server"`),
  `source_b.scanned_at`, `findings` (as in Part B §B7), `sku_checks`,
  `occupancy` and `expected_source` (`"dut-console"`). Existing callers ignore
  them.
- **Gating (decided, Q6): only `engineer` and above trigger a capture.**
  - A capture is any new scan on the Host or any new serial capture on the
    DUT.
  - Today `GET /api/wifi/capability-report` does both on every request, and
    `main.py:352` mounts it with no role dependency, so a guest's page view
    runs DUT serial commands. With the Host scan added, it would also run a
    command on a shared box.
  - **`POST /api/wifi/capability-report/refresh`**, mounted with
    `_ENGINEER`, runs both captures and stores the result per DUT, with
    the Host and the capture time.
  - **`GET /api/wifi/capability-report`** stays open to `guest`, as the page
    is, but only **returns the last stored report**. When none is stored yet,
    it says so: "no report yet — an engineer can run one". It never captures.
  - This **changes existing guest behaviour**. A guest can no longer cause a
    DUT serial capture from this page. The implementation PR lists that
    separately, as a changelog item.
  - `tests/test_route_protection.py` asserts that guest gets 401/403 on
    `refresh` and 200 on the read.
  - The UI shows "Refresh" only to engineer and above, next to the age of
    the stored report.
- `SsidCapabilityCard`: add a findings list (fail/warn/info), an SKU-checks
  table, and an occupancy table that highlights the DUT's channels, labelled
  "Scanned from <Host> at <time>".
- Demo: `demo/ssid-capability.html` must match the observable contract
  (`demo/README_demo_kit.md`), and its `demo/verify` case gets updated.

## A11. PRs

1. **#214** — findings rules, including `legacy_security` / `open_network`.
   Done; reused as-is.
2. **#217** — `fix(backend): stop iw scan blocks from overwriting RSN, read
   Privacy` (§A12). Merged; the fixture is a real Pi 4 scan.
   - #217 also covers part of item 3: Privacy (WEP), WPA-only networks, OWE,
     and `\xNN` SSID escapes.
   - Country and power are still item 3's.
3. `feat(backend): parse privacy, WPA1, country and power from iw scan` — §A6.
   Mostly done in #216; country environment, channel triplets and power
   remain.
4. `feat(backend): run the capability survey on the DUT's Host` — §A5,
   including the collector `survey_iface` property and the probe. It also adds
   the engineer-only `refresh` route and the stored report (§A10, Q6). The
   change to guest behaviour is listed separately.
5. `feat(backend): findings and channel occupancy in the capability report`
   — §A8 and §A9.
6. `feat(backend): SKU checks from the DUT console` — §A7. #216 (merged)
   carries the domain table, the AP6 domain parser, `country_mismatch`,
   `channel_occupancy`, the `services/scan_rows.py` adapter and a host-side
   report CLI (`tools/host_scan_report.py`), all on the existing `iw` parser.
   The CLI is the vehicle for the Pi physical verification.
7. `feat(frontend): SSID Capability findings, SKU checks and occupancy`, and
   the demo page.

## A12. Found while reading: the `iw scan` parser loses RSN fields — fixed in #217

**Status.** Confirmed on a real Pi 4 scan (iw 6.9) and fixed in #217. On the
bench's AP6 420E serial logs, the fix moved PMF from `disabled` to the
correct value on hundreds of BSSes per log. Site Survey was hit harder than
SSID Capability. The original analysis follows.


`_parse_iw_scan` enters RSN mode at `RSN:` and leaves it only at an SSID line,
a new BSS, or an unindented line, but every element block in `iw` output is
indented. Any later block whose lines match the RSN patterns overwrites them:

- `HT capabilities:` contains `Capabilities: 0x…`, which matches the RSN
  Capabilities pattern, so **PMF becomes `disabled`**;
- a `WPA:` block repeats `Group cipher`, `Pairwise ciphers` and
  `Authentication suites`, so **WPA1 values replace the RSN ones**.

Reproduced with synthetic text in beacon element order (RSN, then HT, then
WPA). A WPA2/WPA3 mixed BSS with CCMP and MFP-capable parsed as
`WPA2-Personal`, TKIP only, PMF `disabled`. The committed fixtures place RSN
*after* the HT block, which is why the tests pass. If real `iw` output is in
element order — which is how `iw` prints IEs — SSID Capability and Site
Survey misreport PMF for every HT-capable BSS today. This needs confirming
against a real capture, and is PR 2 in §A11: a pre-existing user-visible bug,
listed on its own.

## A13. Questions

- **Q6 — decided (2026-11-03): `engineer` and above.** Only engineers trigger
  a capture, whether a Host scan or a DUT serial capture. Guests read the last
  stored report (§A10).
- **Q7 — answered for AP6.** AP6 reports its regulatory domain with
  `board_config get domain` (§A7, from #216). Read live on 2026-11-04 through
  the dashboard's serial console, on an AP6_420 US SKU:
  - `board_config get domain` → `0`;
  - `iw reg get` → `country 00: DFS-UNSET`. cfg80211's regdom is unset while
    the driver beacons `US`, so `iw reg get` cannot be the expected source
    for channel or power checks (§A7, "Channel allowed").

  Still open: the AP7 command.
- **Q8 — a model/SKU → domain table:** out of phase 1; who would own it if it
  is wanted later.
- **Q9 — `survey_iface`:** a collector property set by an admin (proposed),
  or detected automatically.

---

# Part B — Packet Monitor (phase 2, tshark)

Reviewed in this PR before the direction change, and kept so its decisions
are not lost. §B6–§B7 (Test Modes, decoders, findings) are implemented in
#214 and used by Part A. The rest becomes phase 2 work: per-beacon evidence
and changes over time, on Hosts that have a monitor-capable adapter.

## B1. Goal and scope

Help an engineer read a Wi-Fi capture without opening Wireshark: pick a capture,
pick a **Test Mode**, and get a table of exactly the fields that mode cares about,
a per-frame detail tree, and **findings** — rules that say what is wrong
(for example "WPA3-SAE advertised but MFPR = 0").

Reference for the feature set: ATLAS One "Smart Analysis" (concepts only; UI
follows this dashboard's own shell).

| In Part B | Out of Part B |
|---|---|
| Analyse an uploaded pcap / pcapng | Live capture from a Host interface (later, §B15) |
| `tshark` runs **on a Host** (an edge collector) | `tshark` on the dashboard server |
| Test Modes: Single Beacon Monitor, Wi-Fi Basic Settings, Wi-Fi Advanced Settings, Manual | User-defined modes saved from the UI |
| Display filter, column chooser, frame detail, CSV export | Multiple source panels side by side, drag reorder |
| Findings (rule engine) on extracted rows | Comparing a capture against the DUT's own config (SSID Capability tie-in) |

## B2. Flow

```
Files workspace (existing upload, 50 MB cap)
        │  file_id
        ▼
packets_api ── validate filter (§B9) ──► 400 on a bad filter
        │
        ▼  202 {id}
packet_jobs (background thread, one per analysis, per-Host queue §B3)
        │ 1. stage:  pcap on stdin → <sha>.<job>.tmp → checksum → mv <sha>.pcapng   (§B4)
        │ 2. run:    nice -n 19 tshark -n -r <sha>.pcapng -Y <filter> -T fields -e ...
        │            stdout = TSV rows, stderr = tshark diagnostics
        ▼
rows decoded (packet_modes) → data/packets/<analysis_id>.jsonl
        │
        ▼
findings (packet_rules) on all rows, then one-row-per-BSS display collapse
        │
frontend ◄── REST: status / rows (paged) / frame detail / export
```

Every remote step is a one-shot ssh whose command is built as in §B10.

## B3. Transport: a one-shot ssh per step, not the held-open session

`CollectorSession.run()` (`collector/ssh_session.py:86`) is the wrong tool:

- 15 s `COMMAND_TIMEOUT_SEC`; a 50 MB capture parse can take longer;
- it is a text channel with an end marker, shared by every caller of that
  collector — a multi-MB binary upload or a large reply would block the console
  probe and attach flows behind it.

Instead each step spawns its own `ssh <user>@<ip> <command>`, using the
**same** two authentication paths `open_session` already uses:

- key: `ssh -o BatchMode=yes -i <key>`;
- password: `pty_ssh.spawn_with_tty` + `pty_ssh.answer_password_prompt`, with
  the password from `CollectorRegistry.credentials_for()`.

`pty_ssh` keeps stdin/stdout as plain pipes (byte-exact, measured in that
module's docstring), so the pcap goes up on stdin and results come back on
stdout with no `scp`/`sftp` dependency.

**Shared pieces, not a shared runner.** The spawn-and-authenticate block is
currently written twice (`collector/ssh_session.py:206-240`,
`serial/serial_worker.py:270-310`). The two existing callers are long-lived
streams with their own stderr and tty handling, so a one-shot runner cannot
absorb them without changing the serial path — an asymmetric-failure
subsystem. B-PR 1 therefore extracts only what all three share,
`pty_ssh.build_ssh_argv(...)` and `pty_ssh.spawn_and_authenticate(...)`, and
uses them from its one new caller. Moving the two existing callers onto them is
a later, separate refactor PR (Q5, §B13 item 6). The one-shot exec loop itself
lives in `services/tshark_runner.py`.

**One-shot exec mechanics (password path).** The pty must keep being drained
(`pty_ssh.TtyDrain`) for the whole step or the child can block; stdin is written
only **after** the password prompt is answered; stdin write, stdout read and
stderr read each run on their own thread, because a 50 MB upload and a large
reply in flight at the same time deadlock on full pipe buffers otherwise.

**The remote shell is not ours.** ssh joins its arguments into one string that
the *login* shell parses first — which may be csh or fish, not sh. Wrapping in
`sh -c '…'` is only a second layer: csh rejects a newline inside single quotes
and fish interprets `\\` and `\'` inside them. A non-interactive ssh also does
not load the user's interactive `PATH` (e.g. `/opt/homebrew/bin` on macOS). §B10
says how the command is built so that none of this matters.

**Limits per job.** Wall-clock timeout (default 180 s); stdout cap (default
200 MB, then the job fails with "too many rows — narrow the filter"); at most
**one running analysis per Host** (queue the rest), plus at most one
short interactive tshark — a frame detail or a filter validation (§B9) — in a
separate lane, so those do not wait minutes behind an analysis; every tshark
runs under `nice -n 19`. A Host may also be serving DUT consoles through `socat`; a
CPU-bound tshark there can cost console bytes at baud rate, which the queue
alone does not prevent — hence `nice`, and hence the remote kill below.

`nice` lowers CPU priority only, and on a Pi the resource a Host shares with its
consoles is more often the SD card. So everything that reads or writes captures
— tshark, the stage step's upload (`cat`) and checksum, and the probe's
temp-file sweep — runs as `ionice -c3 nice -n 19 …` where `ionice` exists and
accepts the idle class (Linux; present on the bench Pi 4), and as
`nice -n 19 …` alone elsewhere, such as macOS. The step script tries
`ionice -c3 true` rather than looking `ionice` up, so one that refuses the idle
class never fails a step.

**A truncated capture is a warning, not a failure.** A capture copied while the
sniffer is still writing ends in a partial record. tshark prints every complete
frame, then exits 2 with "… appears to have been cut short in the middle of a
packet" (tshark 4.4.19, #228). Exactly that outcome — exit 2, that message, and
a step not stopped by timeout, cancel or the stdout cap — counts as success,
and the warning is stored in `packet_analyses.warning` so the UI can say how far
the analysis got. Any other non-zero exit is still a failure.

**A timeout must stop the remote tshark, not only the local ssh.** Killing the
local ssh leaves the remote command running: it has no pty, so tshark dies only
on SIGPIPE at its next stdout write, and a selective filter (few matching
frames) can read to the end of the file first. macOS has no `timeout(1)` by
default. So:

- the run script writes its own pid to `run/<job>.pid` and then `exec`s
  `[ionice -c3] nice -n 19 tshark …` — each of which execs the next — so the
  pid file holds tshark's pid;
- on timeout or user cancel the runner issues a second one-shot ssh with the
  fixed script "kill the pid in `run/<job>.pid` **if** the basename of
  `ps -p <pid> -o comm=` is `tshark`" — a guard against a recycled pid; the
  basename because macOS prints the path the process was exec'd with — then
  removes the pid file;
- a cancel can arrive before the pid file exists (still logging in, still
  uploading). The kill script waits for it up to 10 s; if the job is still
  staging, the runner also closes the upload ssh, which ends the stage step
  before any tshark starts;
- after a backend restart nobody owns the remote jobs any more, so the first
  probe of each Host (§B5) kills every `run/*.pid` it finds (same tshark guard)
  and deletes the files. Anything left in `run/` for over a day is swept too.

**Job lifecycle.**
- Passwords are memory-only, so `credentials_for()` raises after a backend
  restart or before a password is re-entered. The job fails fast with the
  registry's own message ("enter it again"), never retries silently.
- The queue is in memory. At startup, any `packet_analyses` row still in
  `queued` or `running` is marked `failed: interrupted by restart`.
- Deleting a Files-workspace row: see §B4 *Deletion* — one rule, stated once.
- A staged copy evicted from the Host (§B4) is re-staged on demand when frame
  detail is requested.

Host key policy is unchanged: the same `known_hosts` the collector already
trusts; a job never adds a key.

## B4. Remote workspace on the Host

Directory `~/.dut-browser/` with `captures/` (staged pcaps) and `run/`
(per-job scripts and pid files). Every remote step runs under **`umask 077`**,
so customer captures and scripts are readable by the login user only on a
shared box.

**Naming.** Captures are named by the **sha256** of their content, so one
capture is staged once and shared by every analysis of it. `files.sha256` is
nullable for legacy rows: compute and store it on first use, and require
`^[0-9a-f]{64}$` before it is ever used in a remote path.

**Staging, without races.**
- Each job uploads to its **own** temp name, `captures/<sha>.<job>.tmp`, so two
  jobs staging the same capture never write the same file.
- Checksum with whichever of `sha256sum` / `shasum -a 256` the probe found
  (macOS has only the latter), compare, then `mv` to `captures/<sha>.pcapng`.
  The `mv` is the only commit point: it is atomic within one directory, and two
  jobs that both reach it install identical content. A mismatch deletes the
  temp file and fails the job.
- A stage that times out or loses its connection cannot delete its own temp
  file, and the sha-based sweep below would never match it. The probe
  therefore also deletes `captures/*.tmp` older than one hour.
- In the backend, a per-(Host, sha) lock makes the "already staged?" check and
  the upload one step, so the same 50 MB is not sent twice concurrently. The
  `test -f` on the Host is an optimisation inside that lock, not the guard.

**Eviction.** The captures staged on one Host total at most **200 MB**
(`packet_hosts.STAGE_BUDGET_BYTES`), least recently used evicted first, after
each stage and after each probe — but a capture referenced by a `queued` or
`running` analysis on that Host is **pinned** and never evicted, and the
capture just staged is protected during its own stage. If pinned captures alone
exceed the budget, it is exceeded until they finish, and the probe reports it.
The budget is in bytes rather than a count because the Host is often a
Raspberry Pi serving DUT consoles from an SD card: twenty 50 MB captures would
be a gigabyte on a card that must not fill (Q11).

**Free space.** A stage also refuses, before writing a byte, when the Host
would be left with less than 200 MB free once the capture is on it — the same
floor the probe applies (§B5, Q10).

**Deletion** (the single rule; §B3 and §B14 Q4 refer here). Deleting a
Files-workspace row first cancels its queued and running analyses (running
ones through the §B3 remote kill), then deletes those analyses and their result
files under `data/packets/`. Its staged copies are removed only when **no remaining Files
row has the same sha256** — content shared by two rows stays until the last
one goes. Removal happens on every connected Host at once; a Host that is
disconnected at that moment is swept on its next probe (§B5), which deletes any
staged capture whose sha matches no Files row. Captures are customer data on a
shared box, so the sweep is part of retention, not housekeeping.

## B5. Host preflight (probe)

`POST /api/packets/hosts/{collector_id}/probe` runs on every connect and on
demand. Its commands are fixed strings (no user data), still built as in §B10 —
with one bootstrap problem: §B10's template needs to know whether the Host's
`base64` decodes with `-d` or only `-D`, and that is one of the things the probe
finds out. So the probe's own template tries both in one line,
`… | base64 -d > F || … | base64 -D > F`, which is still inside §B10's
character set (`||` works in sh, bash, zsh, csh and fish), and records which
one succeeded. It checks:

- tshark: absolute path (`command -v`, then the usual prefixes such as
  `/opt/homebrew/bin`, `/usr/local/bin`, `/Applications/Wireshark.app/…`),
  `tshark --version` → version stored per Host; a Host below 3.6 is refused
  (Q3);
- `tshark -G fields` → field name → FT_* type, cached per **tshark version**
  under `data/packets/fields-<version>.json`, used to resolve Test Mode columns
  (§B6) and to decode by type;
- tools: checksum (`sha256sum` or `shasum`), and whether `base64 -d` or only
  `base64 -D` (older macOS) decodes — §B10 depends on it;
- free space in `~`: a Host with less than **200 MB** free is not selectable,
  and the probe says how much it found (Q10);
- the §B4 sweeps (staged captures with no Files row, LRU beyond the 200 MB
  budget unless pinned, stale `*.tmp`) and the §B3 `run/` sweep (orphaned
  tshark killed). The Files digests to keep go to the probe script on stdin, so
  the §B10 16 KB script limit does not bound how many files the workspace holds.
  Jobs this backend is still running are passed in by id; their pid files and
  their processes survive the sweep, so an on-demand probe never kills a live
  analysis.

A Host without tshark is shown as "tshark not installed" with the install
command for its OS, and is not selectable.

Probe results are held **in memory**, per collector (Q12): the Host is probed
on every connect, so a stored result could only ever be a stale one. The field
table is the exception — it is a file per tshark *version*, not per Host.

## B6. Test Modes are data (implemented in #214)

`backend/app/services/packet_modes.py`: pydantic models validated at import
time. A mode has a display filter, an optional `group_by` column, columns, and
rule ids:

```python
TestMode(
    id="beacon_security",
    label="Single Beacon Monitor",
    display_filter="wlan.fc.type_subtype == 0x0008",
    group_by="bssid",                       # a column key
    columns=[
        Column(key="frame", label="No.", fields=["frame.number"], decoder="int"),
        Column(key="channel", label="Channel", decoder="int", fields=[
            "wlan.ext_tag.he_operation.6ghz.primary_channel",
            "wlan.ht.info.primarychannel",
            "wlan.ds.current_channel",
            "wlan_radio.channel",
        ]),
        Column(key="captured_on", label="Captured On (ch)", decoder="int",
               fields=["wlan_radio.channel"]),
        Column(key="akm", label="RSN AKM", fields=["wlan.rsn.akms.type"], decoder="akm_list"),
        ...
    ],
    rules=["wpa3_requires_mfpr", "sae_requires_mfpc", ...],
)
```

- **Candidates, first value wins per frame.** A column's `fields` is an ordered
  candidate list. Every candidate the Host's `tshark -G fields` knows is
  extracted, and for each frame the first one with a value is used. This one
  rule covers both renames between releases (`_ws.col.Protocol` in 3.x is
  `_ws.col.protocol` in 4.x, so only one resolves) and per-frame fallbacks.
- **Channel is what the AP advertises.** The HE Operation element carries the
  6 GHz primary channel; HT Operation and the DS Parameter Set cover 2.4 and
  5 GHz (neither is present in a 6 GHz beacon). The radio header's
  `wlan_radio.channel` comes last: it is where the *sniffer* was tuned, and on
  2.4 GHz a beacon is routinely received one or two channels off, which would
  otherwise read as the BSS changing channel. It is also shown on its own as
  "Captured On".
- **Band** comes from a frequency column (`wlan_radio.frequency`,
  `radiotap.channel.freq`) or, without a radio header, from the presence of
  the HE 6 GHz primary channel. Channel numbers overlap between bands and are
  not used for it.
- A column none of whose candidates resolve is **absent** from the decoded row;
  a resolved column with no value in this frame is `None`. Rules use the
  difference (§B7).
- **Decode by type, not by text.** tshark 4.x declares `wlan.ssid` `FT_BYTES`
  and prints hex; 3.6 declares it `FT_STRING` and prints text. Decoders get the
  field's FT_* type from the probe, so an SSID named `cafe` is not read as two
  bytes. A zero-length SSID prints as `<MISSING>` and decodes to `""`. Bytes
  that are not valid UTF-8 (Big5 SSIDs on legacy APs) keep their exact value as
  `0x…` hex rather than replacement characters, which would make many
  different SSIDs look alike. The hex form is not collision-free: a real SSID
  spelled `0xa4a4a4e5` reads the same. An exact form (`ssid_hex` beside the
  text) is deferred.
- **Multiple BSSID.** A beacon can carry its nontransmitted BSSes' RSN
  elements inside a Multiple BSSID element, and tshark reports them under the
  same field names as the beacon's own; `wlan.tag.number` lists nested elements
  flat. Merged, an SAE-only BSS reads as PSK + SAE and an open one as SAE. An
  element before the first Multiple BSSID element cannot be nested in one, and
  IEEE 802.11 orders RSN ahead of it, so the RSN columns keep only that first
  element: the AKM and pairwise lists are cut by `wlan.rsn.akms.count` /
  `wlan.rsn.pcs.count`, and scalars already take their first occurrence.
  - If no RSN element precedes the Multiple BSSID element, the Privacy bit
    decides. It is a fixed field, so it always belongs to the transmitting
    BSS. With Privacy = 0 the transmitting BSS is **open**: its RSN columns are
    `None` and the open-network rules judge it, which at 6 GHz is a fail.
  - With Privacy = 1 (a non-compliant element order, or one nobody can
    attribute), the row is ambiguous.
  - It is also ambiguous when the counts do not match the lists, or are
    missing on a tshark without the count fields while the frame has more
    than one RSN element.
  - An ambiguous row has its RSN columns dropped and is flagged
    `rsn_unresolved` (§B7).

  A proper structural split needs `-T json`; this is the safe subset without
  it.
- **Repeated fields** (several AKMs, several pairwise ciphers) are extracted
  with `-E occurrence=a -E aggregator=<0x1F>` — the ASCII unit separator, not a
  comma, so a comma inside an SSID or the Info column stays one value.
  Decoders return lists for these. Rates drop BSS membership selectors
  (0xFA–0xFF, e.g. SAE H2E).
- Values are shown decoded (`[2, 8]` → `PSK / SAE`, `4` → `CCMP-128`), as in the
  ATLAS screenshots; the raw value stays available on hover.
- **Manual** mode: user filter plus Wireshark's summary columns (No, Time,
  Source, Destination, Protocol, Info).
- Known limit: tshark exposes the suite type but not its OUI as a separate
  field, so a vendor suite (e.g. 00-40-96 type 8) reads as the IEEE suite with
  the same number.

## B7. Findings (implemented in #214)

`backend/app/services/packet_rules.py`. A **row rule** looks at one decoded
frame; a **group rule** looks at all frames of one `group_by` value (one BSS).
A rule whose inputs are absent from the row (the Host's tshark cannot resolve
them) stays silent. Identical findings from one group collapse into one, listing
every frame:

```python
{"rule": "wpa3_requires_mfpr", "severity": "fail",
 "group": "06:03:7f:ba:db:ad", "frames": [3, 41, 79],
 "message": "AKM is SAE only (WPA3) but MFPR = 0; WPA3-only modes require "
            "Management Frame Protection."}
```

| Rule | Severity |
|---|---|
| `wpa3_requires_mfpr`: AKMs only SAE-family (8, 9, 24, 25) and/or OWE (18), and MFPR = 0 | fail |
| `sae_requires_mfpc`: any SAE-family AKM and MFPC = 0 (transition mode included) | fail |
| `mfpr_without_mfpc`: MFPR = 1 and MFPC = 0, any AKM | fail |
| `no_legacy_cipher_with_wpa3`: SAE-family or OWE with TKIP/WEP group or pairwise | fail |
| `six_ghz_security`: 6 GHz BSS that is open, offers a PSK-family AKM or 802.1X SHA-1 (AKM 1, i.e. WPA3-Enterprise transition mode), uses TKIP/WEP, or has MFPR = 0. WPA3-Enterprise only mode, including FT-802.1X (AKM 3), is allowed | fail |
| `rsn_not_evaluated`: a Multiple BSSID beacon whose own RSN element could not be isolated (§B6); security rules were skipped for it | info |
| PSK + SAE transition mode (MFPC = 1, MFPR = 0) | **no finding** — valid by spec; tested |
| `bss_config_changed` (group): one BSS's SSID, channel, AKMs, ciphers, MFPR or MFPC differ between its beacons (list order ignored) | warn |
| `hidden_ssid`: empty or all-NUL SSID | info |
| `beacon_timing`: beacon interval ≠ 100 TU, or DTIM period > 3 | info |

**Ordering with display grouping.** `group_by` also means "one table row per
BSS". `bss_config_changed` needs every beacon, so findings are computed on the
full row set first, and collapsing to the latest beacon per BSS is a display
step afterwards. Rows with no group value (BSSID missing) are not pooled into a
group. Group rules are refused at import time in a mode without `group_by`.

Rules run in the backend, not in tshark filters, so they are unit-testable
against fixture rows without a Host. The test fixture is real tshark 4.4.1
output of synthetic beacons, regenerable from
`backend/tests/fixtures/packets/make_beacons.py`.

## B8. Storage

- The capture itself stays in the **Files workspace** (`data/uploads/`,
  existing 50 MB cap and extension whitelist, which already includes
  pcap/pcapng). No second upload store.
- New tables in `db/workspace.py` as `CREATE TABLE IF NOT EXISTS` in the
  schema script (`_ensure_column` is only for columns added to tables that
  already exist):
  - `packet_analyses(id, file_id, file_sha256, collector_id, mode_id,
    display_filter, status, error, warning, frames_total, frames_shown,
    rows_dropped, tshark_version, created_by_user_id, created_at,
    finished_at)`;
  - `packet_host_stage(collector_id, file_sha256, remote_path, size_bytes,
    staged_at, last_used_at)`. "Pinned" (§B4) is not a column: it is derived from
    `packet_analyses` rows in `queued`/`running` for that Host and sha, so it
    cannot drift. `size_bytes` is what the §B4 byte budget is counted in.
  - Both tables land with B-PR 1, because staging and eviction need them
    (Q13). B-PR 3 adds `packet_analyses.extra_fields` (a JSON list) with
    `_ensure_column`, so a database made with B-PR 1's table gains it too.
- Rows: `data/packets/<analysis_id>.jsonl` (gitignored by the existing `data/`
  rule), plus `<analysis_id>.idx` — the byte offset of every 500th row — so
  `GET rows?offset=` seeks instead of scanning a 200 MB file. Findings:
  `<analysis_id>.findings.json`. The resolved columns:
  `<analysis_id>.columns.json`. One row per `group_by` value (the latest frame
  of each BSS, with its frame count): `<analysis_id>.groups.json`. Frame
  details: `<analysis_id>/frames/<n>.json` once fetched (§B9).
- `warning`: set on a finished analysis the operator should still hear about —
  today only a truncated capture (§B3).
- `rows_dropped`: TSV lines with the wrong cell count (a tab inside a 3.6 text
  value) are dropped rather than misaligned, counted, and shown to the user.
- Authorship from the session (`created_by_user_id`), never from the client.

## B9. API (`backend/app/api/packets_api.py`)

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/packets/hosts` | connected collectors + tshark status/version |
| POST | `/api/packets/hosts/{id}/probe` | §B5 |
| GET  | `/api/packets/modes` | Test Modes (label, columns, rules) |
| POST | `/api/packets/filters/validate` | `{collector_id, display_filter}` → `200 {ok, error}` |
| POST | `/api/packets/analyses` | `{file_id, collector_id, mode_id, display_filter?, extra_fields?}` → `202 {id}`, or `400` with tshark's message for a bad filter |
| GET  | `/api/packets/analyses/{id}` | status, counts, `rows_dropped`, `warning`, tshark stderr on failure, findings summary |
| DELETE | `/api/packets/analyses/{id}` | cancel a queued/running job (remote kill, §B3) |
| GET  | `/api/packets/analyses/{id}/rows?offset=&limit=` | paged decoded rows, `limit ≤ 500` |
| GET  | `/api/packets/analyses/{id}/frames/{n}` | detail tree, see below |
| GET  | `/api/packets/analyses/{id}/export.csv` | visible columns, values formatted as shown |
| DELETE | `/api/packets/hosts/{id}/staged/{sha256}` | remove a staged copy (refused while pinned) |
| GET  | `/api/packets/analyses?limit=` | recent analyses, newest first (added in B-PR 3) |
| GET  | `/api/packets/analyses/{id}/groups` | one row per BSS with its frame count (added in B-PR 3) |
| GET  | `/api/packets/analyses/{id}/findings` | every finding (added in B-PR 3; the summary above only counts them) |

Error statuses: `400` for a request this side refuses (unknown mode, bad
filter, a field the Host's tshark does not know), `404` for an unknown
analysis, file or Host, `409` for a Host that is not connected or not ready,
or an analysis in the wrong state, and `502` when the Host could not be
logged into or tshark failed there.

**Filters combine.** A Test Mode's own display filter and the user's are
applied together, `(<mode filter>) && (<user filter>)`; Manual mode has no
filter of its own. `extra_fields` add text columns to any mode; every name must
`fullmatch` the field pattern and be in the Host's `tshark -G fields` list.

Every engineer sees and may cancel every analysis, as with the Files workspace
(Q16); `created_by_user_id` records who started it.

**Filter validation is up front and cheap.** tshark compiles the display filter
before it opens its input, and a valid filter over an empty capture exits 0. So
validation runs `tshark -n -r - -Y <filter>` on the Host with a 24-byte pcap
header on stdin — no staging, about the cost of a login. It runs in the
**interactive lane** (§B3) with a 10 s timeout, so it never queues behind an
analysis; a timeout answers `{ok: null, error: "Host did not answer"}`, which
the UI shows as "not checked" rather than as invalid. `POST /analyses` runs
the same check before returning `202`, so a typo is a `400` with tshark's own
message (e.g. `"wlan.nope" is not a valid protocol or protocol field`), not a
job that fails after a 50 MB upload. The UI calls the validate route as the
user types (debounced, 600 ms), and only for a filter that changed.

**Frame detail.** `tshark -n -r <sha>.pcapng -c <n> -Y "frame.number==<n>"
-T json`: `-c` counts frames *read*, so tshark stops at frame n instead of
scanning the whole file (measured with tshark 4.4.1). Details run in the
**interactive lane** (§B3): at most one per Host, alongside — not queued behind —
a running analysis, also under `nice -n 19` (and `ionice -c3`, §B3). Each result is cached under
`data/packets/<analysis_id>/frames/<n>.json`, so reopening a frame costs
nothing. Expected latency: one login plus reading the capture up to frame n.

The UI polls `GET /analyses/{id}` once a second **while a job is running** —
this is a backend REST poll, not a serial capture, so the serial RPC rule does
not apply. No new `/ws`.

Gating: `app.include_router(packets_router, dependencies=[_ENGINEER])` in
`main.py`, plus `tests/test_route_protection.py` cases. Role decided in Q1 (§B14).

## B10. Command construction (security)

Every remote command is built by **one** function in
`services/tshark_runner.py`, in two layers.

**Outer layer — what the login shell sees.** A fixed template whose only
variable parts are a job id (hex) and a base64 blob, written in pieces of at
most 1000 characters (one line in reality; wrapped here):

```
umask 077; mkdir -p ~/.dut-browser/run;
printf %s <B64 piece 1> > ~/.dut-browser/run/<job>.b64;
printf %s <B64 piece 2> >> ~/.dut-browser/run/<job>.b64; ...;
cat ~/.dut-browser/run/<job>.b64 | base64 -d > ~/.dut-browser/run/<job>.sh;
rm -f ~/.dut-browser/run/<job>.b64; sh ~/.dut-browser/run/<job>.sh
```

(`base64 -D` where the probe found that older macOS spelling.) The pieces exist
because of **BSD csh** — `csh` on Debian and Ubuntu, while macOS's `csh` is
tcsh — which refuses any word longer than its buffer with "Word too long". A
16 KB script is 22 KB of base64; as one word it failed the CI shell matrix on
Ubuntu. The blob is
produced by Python's `base64.b64encode`, which never wraps lines — GNU
`base64` wraps at 76 columns, and a newline in the outer string is exactly what
csh rejects. Every character in it is in `[A-Za-z0-9 ~/._;|>%=+-]`: no quote,
backslash, `!`, `$`, backtick or newline. `umask`, `mkdir`, `printf`, `cat`,
`rm`, the pipe, `>`, `>>`, `;` and `~` mean the same thing in sh, bash, zsh, csh and fish, so the
outer string survives whichever login shell the Host has. The script is read
from a file, not from stdin, so stdin stays free for the pcap upload.

Measured on macOS before writing this: the template run by `sh`, `bash`,
`zsh`, `csh` and `tcsh` delivered the filter
``wlan.ssid == "a'b" && !x $(id) `id` \ 中`` to the script byte for byte, the
script deleted itself, and `run/` was created `drwx------`. fish was not
installed and is untested.

The whole command travels as one ssh argument. tcsh accepted an 85 KB word in
the same test, but BSD csh does not (above), and older shells and `ARG_MAX` are
not ours to know, so the builder refuses a script over **16 KB** — far above a
1024-byte filter plus a field list — and a test runs a 16 KB script through
every installed shell. CI installs zsh and BSD csh, so that matrix is at least
sh, bash, zsh and BSD csh there, and tcsh on macOS.

**Inner layer — the script, always run by `sh`.** It starts with `rm -f "$0"`,
sets `PATH` to include the tshark directory from the probe, and then does the
step. All user-supplied data (display filter, field list) is inside this
script, quoted with `shlex.quote`, and never reaches the login shell, because
the whole script travels base64-encoded.

- field names: must `fullmatch` `[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*`
  (as `packet_modes.FIELD_RE` already does — `$` alone would accept a trailing
  newline) **and** be in that Host's `tshark -G fields` list;
- display filter: length ≤ 1024; validated as in §B9;
- paths: built only from the sha256 hex digest and the job id, never from a
  filename;
- always `-n` (no name resolution — the Host must not start DNS lookups for
  every MAC/IP in a customer capture).

**Tests.**
- The outer string matches the allowed character class for every input,
  including injection strings (`'; rm -rf ~ #`, `$(...)`, backticks, `\`, `!`,
  newlines, non-ASCII).
- The inner script, run by `sh` locally with a fake `tshark` that prints its
  argv, receives the filter as exactly one argument, byte for byte.
- The outer string is executed by every shell installed on the CI runner (at
  least `sh`, `bash`, `zsh`; `csh`/`fish` when present) and produces the same
  script file.

## B11. Frontend

Following the Wi-Fi Clients template:

- `api/rest.ts`: typed functions for §B9.
- `monitoring/PacketAnalysisContext.tsx`: caches analyses by id, scoped to the
  selected Host; one in-flight request per key.
- `components/PacketMonitorSection.tsx`:
  - source row: file picker (from Files workspace) + Host picker with tshark
    status dot;
  - Test Mode select + filter input (validated as you type, with recent-filter
    history in `localStorage`) + Run / Cancel;
  - findings bar (fail / warn / info counts). As built in B-PR 4, a count
    filters the **findings list**, and each finding's frame numbers open the
    detail panel; the table itself is not filtered, because rows are paged by
    offset and a frame filter would need a route of its own. The table colours
    every row that a finding names;
  - `PacketTable`: hand-rolled windowed rendering (only visible rows in the
    DOM), column chooser, horizontal scroll, row status colour from findings.
    Values are the backend's display text (`rows[].display`, §B9), with the raw
    value on hover; the browser holds no copy of the suite names;
  - "One row per BSS" switches the table to the groups route for modes with a
    `group_by`;
  - `PacketDetailPanel`: a side panel tree, not a popup window.
  - Extra fields: a free-text list beside the filter, sent as `extra_fields`.
- `navigation.ts`: new item `packets`, label "Packet Monitor", group
  **Monitoring**, `minRole: "engineer"` (Q1). Not Fleet: its Hosts/Profiles are
  admin-only, so an engineer would see a lone item there. The demo kit's
  navigation verifier reads this file and is updated in the same PR.
- Host picker lists only Hosts an admin has already connected; a disconnected
  Host shows "ask an admin to connect it" rather than a Connect button.
- Demo mode: `demoApi.ts` answers from an anonymised fixture built by
  `demo/build_demo_data.py`; a matching `demo/packet-monitor.html` page and its
  `demo/verify` case. (Can be its own PR.) Until that page exists, every demo
  sidebar draws Packet Monitor as a screen with no file in the kit, which is
  what the navigation verifier requires of an admin sidebar.
- `PacketAnalysisContext` sits in `AppShell` with the other providers, so a
  running analysis keeps its place while the user visits another section.

No chart or UI library; no CDN.

## B12. Testing

- Unit (done in #214): mode resolution and per-frame fallback, decoders by
  FT_* type, every rule with a failing and a passing case, the transition-mode
  row producing **no** finding, grouping edge cases; fixture from real tshark.
- Command builder: §B10 tests.
- Transport: a fake `ssh` script (the runner takes an `ssh_binary` parameter,
  as `open_session` already does) that runs the remote command locally, so
  staging, checksum, timeout, remote kill and stdout cap are tested without a
  Host. Two variants: the **key** path, and a **password** path whose fake
  prints a `password:` prompt on `/dev/tty` and refuses to read stdin until
  answered — the path with the pty, the drain and the threads is the one most
  likely to hang.
- Staging: two concurrent jobs on the same sha upload once and both succeed; an
  eviction never removes a pinned capture; deleting one of two Files rows with
  the same sha keeps the staged copy.
- Route protection: guest gets 401/403 on every new route.
- Revert check: break each guard on purpose once and watch its test go red.
- Bench: one real Host with tshark — **needs the bench owner's go-ahead**
  (shared hardware rule).

## B13. PR breakdown

1. `feat(backend): tshark runner, host probe and staging` — includes the new
   `pty_ssh.build_ssh_argv` / `spawn_and_authenticate` helpers with this one
   caller (Q5), the §B10 command builder, remote kill, the §B5 probe, the §B4
   staging rules and the §B8 tables (Q13). No routes: the probe route and the
   per-Host queue arrive with item 3.
2. `feat(backend): test modes, decoders and findings rules` — **#214**.
3. `feat(backend): packets API and analysis jobs` (+ route protection tests).
4. `feat(frontend): Packet Monitor section`.
5. `feat(demo): Packet Monitor demo page`.
6. Later, separately: `refactor(backend): move existing ssh callers onto the
   shared pty_ssh helpers` (Q5).

## B14. Decisions

- **Q1 — who may run tshark on a Host** (Part B only; Part A's scan is Q6): `engineer`, on Hosts an `admin` has
  already connected. Connecting, configuring and removing Hosts stay admin-only
  in `collectors_api`. `packets_router` is mounted with `_ENGINEER`; the
  route-protection tests assert guest is refused on every route.
- **Q2 — upload cap:** unchanged at 50 MB for Part B.
- **Q3 — tshark floor:** 3.6. The probe refuses older Hosts with the version it
  found; field candidates cover 3.6 and newer only.
- **Q4 — retention:** analysis results in `data/packets/` are deleted after
  30 days (swept at startup and daily); the captures staged on each Host total
  at most 200 MB (LRU, pinned ones excepted — Q11 replaced "at most 20
  captures"); deleting a Files row follows §B4 *Deletion*.
- **Q5 — existing ssh callers:** not moved in this work. The shared helpers
  land with their one new caller; moving `SerialWorker` and `open_session` onto
  them is a later, separate refactor PR.
- **Q10 — free-space floor (decided 2026-11-06):** 200 MB. Below it the probe
  marks the Host not selectable, and a stage refuses when the capture would
  leave less than that free.
- **Q11 — staged-capture budget (decided 2026-11-06):** 200 MB in total per
  Host, replacing Q4's "at most 20 captures". A Pi's SD card filling up stops
  the DUT consoles it serves; twenty 50 MB captures would have been 1 GB.
- **Q12 — where probe results live (decided 2026-11-06):** in memory, per
  collector; re-probed on every connect.
- **Q13 — when the §B8 tables land (decided 2026-11-06):** with B-PR 1, since
  staging, eviction and pinning are built and tested against them there.
- **Q14 — Files delete (decided 2026-11-10):** the §B4 *Deletion* rule lands
  in B-PR 3. `DELETE /api/files/{id}` keeps its response; after the row is gone
  it cancels and removes the file's analyses and results, and removes its
  staged copies from connected Hosts in a background thread, only when no other
  Files row has the same sha256.
- **Q15 — probe on connect (decided 2026-11-10):** `collectors_api`'s connect
  route starts the §B5 probe in a background thread after a successful login;
  a slow or failing probe never delays or fails the connect.
- **Q16 — who sees an analysis (decided 2026-11-10):** every engineer, and every
  engineer may cancel one.

## B15. Live capture (later)

Same transport and command builder, long-lived: `tshark -i <iface> -l -T fields
...` streaming lines on stdout, appended to the analysis's jsonl; the UI pages
through it with an `after=` cursor; stop is the §B3 remote kill. Interface names
come from `tshark -D` on the Host and are matched against that list before use.
Monitor-mode setup and capture privileges (`dumpcap` capabilities / sudo) are a
separate design question.

## B16. Found while reading, not part of this work

- `collector/ssh_session.py:129` reports a timeout as
  `int(deadline)` "seconds", but `deadline` is a `time.monotonic()` timestamp,
  so the message shows a meaningless large number. Separate `fix(backend)` PR.
