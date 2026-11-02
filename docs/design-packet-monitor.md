# Packet Monitor — phase 1 design (file analysis on a Host)

Status: **agreed design** (decisions in §14). Test Modes, decoders and findings
rules are implemented in #214; the rest is not implemented yet.

## 1. Goal and scope

Help an engineer read a Wi-Fi capture without opening Wireshark: pick a capture,
pick a **Test Mode**, and get a table of exactly the fields that mode cares about,
a per-frame detail tree, and **findings** — rules that say what is wrong
(for example "WPA3-SAE advertised but MFPR = 0").

Reference for the feature set: ATLAS One "Smart Analysis" (concepts only; UI
follows this dashboard's own shell).

| In phase 1 | Out of phase 1 |
|---|---|
| Analyse an uploaded pcap / pcapng | Live capture from a Host interface (phase 2) |
| `tshark` runs **on a Host** (an edge collector) | `tshark` on the dashboard server |
| Test Modes: Single Beacon Monitor, Wi-Fi Basic Settings, Wi-Fi Advanced Settings, Manual | User-defined modes saved from the UI |
| Display filter, column chooser, frame detail, CSV export | Multiple source panels side by side, drag reorder |
| Findings (rule engine) on extracted rows | Comparing a capture against the DUT's own config (SSID Capability tie-in) |

## 2. Flow

```
Files workspace (existing upload, 50 MB cap)
        │  file_id
        ▼
packets_api ── validate filter (§9) ──► 400 on a bad filter
        │
        ▼  202 {id}
packet_jobs (background thread, one per analysis, per-Host queue §3)
        │ 1. stage:  pcap on stdin → <sha>.<job>.tmp → checksum → mv <sha>.pcapng   (§4)
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

Every remote step is a one-shot ssh whose command is built as in §10.

## 3. Transport: a one-shot ssh per step, not the held-open session

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
subsystem. PR 1 therefore extracts only what all three share,
`pty_ssh.build_ssh_argv(...)` and `pty_ssh.spawn_and_authenticate(...)`, and
uses them from its one new caller. Moving the two existing callers onto them is
a later, separate refactor PR (Q5, §13 item 6). The one-shot exec loop itself
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
not load the user's interactive `PATH` (e.g. `/opt/homebrew/bin` on macOS). §10
says how the command is built so that none of this matters.

**Limits per job.** Wall-clock timeout (default 180 s); stdout cap (default
200 MB, then the job fails with "too many rows — narrow the filter"); at most
**one running analysis per Host** (queue the rest), plus at most one
short interactive tshark — a frame detail or a filter validation (§9) — in a
separate lane, so those do not wait minutes behind an analysis; every tshark
runs under `nice -n 19`. A Host may also be serving DUT consoles through `socat`; a
CPU-bound tshark there can cost console bytes at baud rate, which the queue
alone does not prevent — hence `nice`, and hence the remote kill below.

**A timeout must stop the remote tshark, not only the local ssh.** Killing the
local ssh leaves the remote command running: it has no pty, so tshark dies only
on SIGPIPE at its next stdout write, and a selective filter (few matching
frames) can read to the end of the file first. macOS has no `timeout(1)` by
default. So:

- the run script writes its own pid to `run/<job>.pid` and then `exec`s
  `nice -n 19 tshark …`, so the pid file holds tshark's pid;
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
  probe of each Host (§5) kills every `run/*.pid` it finds (same tshark guard)
  and deletes the files. Anything left in `run/` for over a day is swept too.

**Job lifecycle.**
- Passwords are memory-only, so `credentials_for()` raises after a backend
  restart or before a password is re-entered. The job fails fast with the
  registry's own message ("enter it again"), never retries silently.
- The queue is in memory. At startup, any `packet_analyses` row still in
  `queued` or `running` is marked `failed: interrupted by restart`.
- Deleting a Files-workspace row: see §4 *Deletion* — one rule, stated once.
- A staged copy evicted from the Host (§4) is re-staged on demand when frame
  detail is requested.

Host key policy is unchanged: the same `known_hosts` the collector already
trusts; a job never adds a key.

## 4. Remote workspace on the Host

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

**Eviction.** At most 20 captures per Host, least recently used first, after
each stage — but a capture referenced by a `queued` or `running` analysis on
that Host is **pinned** and never evicted. If pinned captures alone exceed the
limit, the limit is exceeded until they finish, and the probe reports it.

**Deletion** (the single rule; §3 and §14 Q4 refer here). Deleting a
Files-workspace row first cancels its queued and running analyses (running
ones through the §3 remote kill), then deletes those analyses and their result
files under `data/packets/`. Its staged copies are removed only when **no remaining Files
row has the same sha256** — content shared by two rows stays until the last
one goes. Removal happens on every connected Host at once; a Host that is
disconnected at that moment is swept on its next probe (§5), which deletes any
staged capture whose sha matches no Files row. Captures are customer data on a
shared box, so the sweep is part of retention, not housekeeping.

## 5. Host preflight (probe)

`POST /api/packets/hosts/{collector_id}/probe` runs on every connect and on
demand. Its commands are fixed strings (no user data), still built as in §10 —
with one bootstrap problem: §10's template needs to know whether the Host's
`base64` decodes with `-d` or only `-D`, and that is one of the things the probe
finds out. So the probe's own template tries both in one line,
`… | base64 -d > F || … | base64 -D > F`, which is still inside §10's
character set (`||` works in sh, bash, zsh, csh and fish), and records which
one succeeded. It checks:

- tshark: absolute path (`command -v`, then the usual prefixes such as
  `/opt/homebrew/bin`, `/usr/local/bin`, `/Applications/Wireshark.app/…`),
  `tshark --version` → version stored per Host; a Host below 3.6 is refused
  (Q3);
- `tshark -G fields` → field name → FT_* type, cached per **tshark version**
  under `data/packets/fields-<version>.json`, used to resolve Test Mode columns
  (§6) and to decode by type;
- tools: checksum (`sha256sum` or `shasum`), and whether `base64 -d` or only
  `base64 -D` (older macOS) decodes — §10 depends on it;
- free space in `~`;
- the §4 sweeps (staged captures with no Files row, LRU beyond 20 unless
  pinned, stale `*.tmp`) and the §3 `run/` sweep (orphaned tshark killed).

A Host without tshark is shown as "tshark not installed" with the install
command for its OS, and is not selectable.

## 6. Test Modes are data (implemented in #214)

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
            "wlan_radio.channel",
            "wlan.ext_tag.he_operation.6ghz.primary_channel",
            "wlan.ht.info.primarychannel",
            "wlan.ds.current_channel",
        ]),
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
  `_ws.col.protocol` in 4.x, so only one resolves) and per-frame fallbacks:
  `wlan_radio.channel` needs a radiotap/PPI header, the HE Operation element
  carries the 6 GHz primary channel, and HT Operation / DS Parameter Set cover
  2.4 and 5 GHz (neither is present in a 6 GHz beacon).
- **Band** comes from a frequency column (`wlan_radio.frequency`,
  `radiotap.channel.freq`) or, without a radio header, from the presence of
  the HE 6 GHz primary channel. Channel numbers overlap between bands and are
  not used for it.
- A column none of whose candidates resolve is **absent** from the decoded row;
  a resolved column with no value in this frame is `None`. Rules use the
  difference (§7).
- **Decode by type, not by text.** tshark 4.x declares `wlan.ssid` `FT_BYTES`
  and prints hex; 3.6 declares it `FT_STRING` and prints text. Decoders get the
  field's FT_* type from the probe, so an SSID named `cafe` is not read as two
  bytes. A zero-length SSID prints as `<MISSING>` and decodes to `""`.
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

## 7. Findings (implemented in #214)

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
| `six_ghz_security`: 6 GHz BSS that is open, offers a PSK-family AKM, uses TKIP/WEP, or has MFPR = 0 (WPA3-Enterprise is allowed) | fail |
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

## 8. Storage

- The capture itself stays in the **Files workspace** (`data/uploads/`,
  existing 50 MB cap and extension whitelist, which already includes
  pcap/pcapng). No second upload store.
- New tables in `db/workspace.py` as `CREATE TABLE IF NOT EXISTS` in the
  schema script (`_ensure_column` is only for columns added to tables that
  already exist):
  - `packet_analyses(id, file_id, file_sha256, collector_id, mode_id,
    display_filter, status, error, frames_total, frames_shown,
    rows_dropped, tshark_version, created_by_user_id, created_at,
    finished_at)`;
  - `packet_host_stage(collector_id, file_sha256, remote_path, staged_at,
    last_used_at)`. "Pinned" (§4) is not a column: it is derived from
    `packet_analyses` rows in `queued`/`running` for that Host and sha, so it
    cannot drift.
- Rows: `data/packets/<analysis_id>.jsonl` (gitignored by the existing `data/`
  rule), plus `<analysis_id>.idx` — the byte offset of every 500th row — so
  `GET rows?offset=` seeks instead of scanning a 200 MB file. Findings:
  `<analysis_id>.findings.json`. Frame details: `<analysis_id>/frames/<n>.json`
  once fetched (§9).
- `rows_dropped`: TSV lines with the wrong cell count (a tab inside a 3.6 text
  value) are dropped rather than misaligned, counted, and shown to the user.
- Authorship from the session (`created_by_user_id`), never from the client.

## 9. API (`backend/app/api/packets_api.py`)

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/packets/hosts` | connected collectors + tshark status/version |
| POST | `/api/packets/hosts/{id}/probe` | §5 |
| GET  | `/api/packets/modes` | Test Modes (label, columns, rules) |
| POST | `/api/packets/filters/validate` | `{collector_id, display_filter}` → `200 {ok, error}` |
| POST | `/api/packets/analyses` | `{file_id, collector_id, mode_id, display_filter?, extra_fields?}` → `202 {id}`, or `400` with tshark's message for a bad filter |
| GET  | `/api/packets/analyses/{id}` | status, counts, `rows_dropped`, tshark stderr on failure, findings summary |
| DELETE | `/api/packets/analyses/{id}` | cancel a queued/running job (remote kill, §3) |
| GET  | `/api/packets/analyses/{id}/rows?offset=&limit=` | paged decoded rows, `limit ≤ 500` |
| GET  | `/api/packets/analyses/{id}/frames/{n}` | detail tree, see below |
| GET  | `/api/packets/analyses/{id}/export.csv` | |
| DELETE | `/api/packets/hosts/{id}/staged/{sha256}` | remove a staged copy (refused while pinned) |

**Filter validation is up front and cheap.** tshark compiles the display filter
before it opens its input, and a valid filter over an empty capture exits 0. So
validation runs `tshark -n -r - -Y <filter>` on the Host with a 24-byte pcap
header on stdin — no staging, about the cost of a login. It runs in the
**interactive lane** (§3) with a 10 s timeout, so it never queues behind an
analysis; a timeout answers `{ok: null, error: "Host did not answer"}`, which
the UI shows as "not checked" rather than as invalid. `POST /analyses` runs
the same check before returning `202`, so a typo is a `400` with tshark's own
message (e.g. `"wlan.nope" is not a valid protocol or protocol field`), not a
job that fails after a 50 MB upload. The UI calls the validate route as the
user types (debounced, 600 ms), and only for a filter that changed.

**Frame detail.** `tshark -n -r <sha>.pcapng -c <n> -Y "frame.number==<n>"
-T json`: `-c` counts frames *read*, so tshark stops at frame n instead of
scanning the whole file (measured with tshark 4.4.1). Details run in the
**interactive lane** (§3): at most one per Host, alongside — not queued behind —
a running analysis, also under `nice -n 19`. Each result is cached under
`data/packets/<analysis_id>/frames/<n>.json`, so reopening a frame costs
nothing. Expected latency: one login plus reading the capture up to frame n.

The UI polls `GET /analyses/{id}` once a second **while a job is running** —
this is a backend REST poll, not a serial capture, so the serial RPC rule does
not apply. No new `/ws`.

Gating: `app.include_router(packets_router, dependencies=[_ENGINEER])` in
`main.py`, plus `tests/test_route_protection.py` cases. Role decided in Q1 (§14).

## 10. Command construction (security)

Every remote command is built by **one** function in
`services/tshark_runner.py`, in two layers.

**Outer layer — what the login shell sees.** A fixed template whose only
variable parts are a job id (hex) and a base64 blob:

```
umask 077; mkdir -p ~/.dut-browser/run; printf %s <B64> | base64 -d > ~/.dut-browser/run/<job>.sh; sh ~/.dut-browser/run/<job>.sh
```

(`base64 -D` where the probe found that older macOS spelling.) The blob is
produced by Python's `base64.b64encode`, which never wraps lines — GNU
`base64` wraps at 76 columns, and a newline in the outer string is exactly what
csh rejects. Every character in it is in `[A-Za-z0-9 ~/._;|>%=+-]`: no quote,
backslash, `!`, `$`, backtick or newline. `umask`, `mkdir`, `printf`, the pipe,
`>`, `;` and `~` mean the same thing in sh, bash, zsh, csh and fish, so the
outer string survives whichever login shell the Host has. The script is read
from a file, not from stdin, so stdin stays free for the pcap upload.

Measured on macOS before writing this: the template run by `sh`, `bash`,
`zsh`, `csh` and `tcsh` delivered the filter
``wlan.ssid == "a'b" && !x $(id) `id` \ 中`` to the script byte for byte, the
script deleted itself, and `run/` was created `drwx------`. fish was not
installed and is untested.

The whole script travels as one argument word. csh and tcsh accepted an 85 KB
word in the same test, but older shells and `ARG_MAX` are not ours to know, so
the builder refuses a script over **16 KB** — far above a 1024-byte filter plus
a field list — and a test runs a 16 KB script through every installed shell.

**Inner layer — the script, always run by `sh`.** It starts with `rm -f "$0"`,
sets `PATH` to include the tshark directory from the probe, and then does the
step. All user-supplied data (display filter, field list) is inside this
script, quoted with `shlex.quote`, and never reaches the login shell, because
the whole script travels base64-encoded.

- field names: must `fullmatch` `[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*`
  (as `packet_modes.FIELD_RE` already does — `$` alone would accept a trailing
  newline) **and** be in that Host's `tshark -G fields` list;
- display filter: length ≤ 1024; validated as in §9;
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

## 11. Frontend

Following the Wi-Fi Clients template:

- `api/rest.ts`: typed functions for §9.
- `monitoring/PacketAnalysisContext.tsx`: caches analyses by id, scoped to the
  selected Host; one in-flight request per key.
- `components/PacketMonitorSection.tsx`:
  - source row: file picker (from Files workspace) + Host picker with tshark
    status dot;
  - Test Mode select + filter input (validated as you type, with recent-filter
    history in `localStorage`) + Run / Cancel;
  - findings bar (fail / warn / info counts, click to filter the table);
  - `PacketTable`: hand-rolled windowed rendering (only visible rows in the
    DOM), column chooser, horizontal scroll, row status colour from findings;
  - `PacketDetailPanel`: a side panel tree, not a popup window.
- `navigation.ts`: new item `packets`, label "Packet Monitor", group
  **Monitoring**, `minRole: "engineer"` (Q1). Not Fleet: its Hosts/Profiles are
  admin-only, so an engineer would see a lone item there. The demo kit's
  navigation verifier reads this file and is updated in the same PR.
- Host picker lists only Hosts an admin has already connected; a disconnected
  Host shows "ask an admin to connect it" rather than a Connect button.
- Demo mode: `demoApi.ts` answers from an anonymised fixture built by
  `demo/build_demo_data.py`; a matching `demo/packet-monitor.html` page and its
  `demo/verify` case. (Can be its own PR.)

No chart or UI library; no CDN.

## 12. Testing

- Unit (done in #214): mode resolution and per-frame fallback, decoders by
  FT_* type, every rule with a failing and a passing case, the transition-mode
  row producing **no** finding, grouping edge cases; fixture from real tshark.
- Command builder: §10 tests.
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

## 13. PR breakdown

1. `feat(backend): tshark runner, host probe and staging` — includes the new
   `pty_ssh.build_ssh_argv` / `spawn_and_authenticate` helpers with this one
   caller (Q5), the §10 command builder, remote kill and the §4 staging rules.
2. `feat(backend): test modes, decoders and findings rules` — **#214**.
3. `feat(backend): packets API and analysis jobs` (+ route protection tests).
4. `feat(frontend): Packet Monitor section`.
5. `feat(demo): Packet Monitor demo page`.
6. Later, separately: `refactor(backend): move existing ssh callers onto the
   shared pty_ssh helpers` (Q5).

## 14. Decisions

- **Q1 — who may run tshark on a Host:** `engineer`, on Hosts an `admin` has
  already connected. Connecting, configuring and removing Hosts stay admin-only
  in `collectors_api`. `packets_router` is mounted with `_ENGINEER`; the
  route-protection tests assert guest is refused on every route.
- **Q2 — upload cap:** unchanged at 50 MB for phase 1.
- **Q3 — tshark floor:** 3.6. The probe refuses older Hosts with the version it
  found; field candidates cover 3.6 and newer only.
- **Q4 — retention:** analysis results in `data/packets/` are deleted after
  30 days (swept at startup and daily); each Host keeps at most 20 staged
  captures (LRU, pinned ones excepted); deleting a Files row follows §4
  *Deletion*.
- **Q5 — existing ssh callers:** not moved in this work. The shared helpers
  land with their one new caller; moving `SerialWorker` and `open_session` onto
  them is a later, separate refactor PR.

## 15. Phase 2 sketch (live capture)

Same transport and command builder, long-lived: `tshark -i <iface> -l -T fields
...` streaming lines on stdout, appended to the analysis's jsonl; the UI pages
through it with an `after=` cursor; stop is the §3 remote kill. Interface names
come from `tshark -D` on the Host and are matched against that list before use.
Monitor-mode setup and capture privileges (`dumpcap` capabilities / sudo) are a
separate design question.

## 16. Found while reading, not part of this work

- `collector/ssh_session.py:129` reports a timeout as
  `int(deadline)` "seconds", but `deadline` is a `time.monotonic()` timestamp,
  so the message shows a meaningless large number. Separate `fix(backend)` PR.
