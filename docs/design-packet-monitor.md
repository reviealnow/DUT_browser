# Packet Monitor — phase 1 design (file analysis on a Host)

Status: **agreed design** (decisions in §14). Nothing here is implemented yet.

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
packets_api ──► packet_jobs (background thread, one per analysis)
                    │
                    │ 1. stage: ssh <host> 'mkdir -p D && cat > D/<sha>.tmp && mv ...'   (pcap on stdin)
                    │ 2. run:   ssh <host> 'tshark -n -r D/<sha>.pcapng -Y <filter> -T fields -e ...'
                    │           stdout = TSV rows, stderr = tshark diagnostics
                    ▼
            rows cached in data/packets/<analysis_id>.jsonl
                    │
                    ▼
            rule engine (Python) → findings
                    │
frontend  ◄── REST: status / rows (paged) / frame detail / export
```

## 3. Transport: a one-shot ssh per job, not the held-open session

`CollectorSession.run()` (`collector/ssh_session.py:86`) is the wrong tool:

- 15 s `COMMAND_TIMEOUT_SEC`; a 50 MB capture parse can take longer;
- it is a text channel with an end marker, shared by every caller of that
  collector — a multi-MB binary upload or JSON reply would block the console
  probe and attach flows behind it.

Instead each step spawns its own `ssh <user>@<ip> '<command>'`, using the
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
streams with their own stderr and tty handling, so a one-shot `run_remote()`
cannot absorb them without changing the serial path — an asymmetric-failure
subsystem. Phase 1 therefore extracts only what all three share:
`pty_ssh.build_ssh_argv(...)` and `pty_ssh.spawn_and_authenticate(...)`.
Whether the two existing callers move onto them (in a **separate, earlier PR**,
no behaviour change) was decided in Q5 (§14): not in this work. The one-shot exec loop itself lives
in `services/tshark_runner.py`.

**One-shot exec mechanics (password path).** The pty must keep being drained
(`pty_ssh.TtyDrain`) for the whole job or the child can block; stdin is written
only **after** the password prompt is answered; stdin write, stdout read and
stderr read each run on their own thread, because a 50 MB upload and a large
reply in flight at the same time deadlock on full pipe buffers otherwise.

**Remote shell and PATH are not ours.** A non-interactive ssh does not load the
login user's interactive PATH (e.g. `/opt/homebrew/bin` on a macOS Host), and
the login shell may be csh or fish, where `shlex.quote` output is mis-parsed.
Every remote command is therefore sent as one argument to `sh -c`, with an
explicit `PATH=` prefix that includes the tshark directory found by the probe
(§5).

Limits per job: wall-clock timeout (default 180 s), stdout cap (default 200 MB,
then the job fails with "too many rows — narrow the filter"), at most **one
running tshark per Host** (queue the rest), and tshark always runs under
`nice -n 19`. A Host may also be serving DUT consoles through `socat`; a
CPU-bound tshark there can cost console bytes at baud rate, which the queue
alone does not prevent.

**Job lifecycle.**
- Passwords are memory-only, so `credentials_for()` raises after a backend
  restart or before a password is re-entered. The job fails fast with the
  registry's own message ("enter it again"), never retries silently.
- The queue is in memory. At startup, any `packet_analyses` row still in
  `queued` or `running` is marked `failed: interrupted by restart`.
- Deleting the Files-workspace row deletes its analyses and result files. A
  staged copy evicted from the Host (§4) is re-staged on demand when frame
  detail is requested.

Host key policy is unchanged: the same `known_hosts` the collector already
trusts; a job never adds a key.

## 4. Remote workspace on the Host

- Directory: `$HOME/.dut-browser/captures/`, files named by **sha256** of the
  content, so the same capture is staged once and shared by every analysis.
- Stage: `cat > <sha>.tmp`, checksum with whichever of `sha256sum` /
  `shasum -a 256` the probe found (macOS has only the latter) → compare → `mv`
  to final name. A mismatch deletes the temp file and fails the job.
- `files.sha256` is nullable for legacy rows: compute and store it on first use,
  and require `^[0-9a-f]{64}$` before it is ever used in a remote path.
- Customer captures sit in `$HOME` on a Host, so retention is a privacy
  setting, not only a disk one (Q4).
- Before staging: `test -f <sha>.pcapng` — skip the upload if already there.
- Cleanup: keep at most N captures (default 20) per Host, least-recently-used
  removed after each stage; plus an explicit "remove from Host" action.

## 5. Host preflight (probe)

`POST /api/packets/hosts/{collector_id}/probe` runs once per connect:

- `command -v tshark` and `tshark --version` → version string stored per Host;
- `tshark -G fields` → field list, cached per **tshark version** under
  `data/packets/fields-<version>.json`, used to validate columns and to resolve
  field aliases (§6);
- free space in `$HOME`;
- login shell (`echo $0` under `sh -c` is ours; `getent passwd`/`dscl` for
  the record), tshark's absolute path (searching the usual prefixes when
  `command -v` misses), and which checksum tool exists.

A Host without tshark is shown as "tshark not installed" with the install
command for its OS, and is not selectable.

## 6. Test Modes are data

`backend/app/services/packet_modes.py`, a list of plain dicts validated by a
pydantic model at import time:

```python
{
  "id": "beacon_security",
  "label": "Single Beacon Monitor",
  "display_filter": "wlan.fc.type_subtype == 0x08",
  "group_by": "wlan.bssid",          # one row per BSSID (latest beacon), optional
  "columns": [
    {"label": "SSID",            "fields": ["wlan.ssid"]},
    {"label": "Channel",         "fields": ["wlan_radio.channel", "wlan.ht.info.primarychannel",
                                            "wlan.ds.current_channel"]},
    {"label": "Group Cipher",    "fields": ["wlan.rsn.gcs.type"]},
    {"label": "Pairwise Cipher", "fields": ["wlan.rsn.pcs.type"]},
    {"label": "AKM",             "fields": ["wlan.rsn.akms.type"]},
    {"label": "MFPR",            "fields": ["wlan.rsn.capabilities.mfpr"]},
    {"label": "MFPC",            "fields": ["wlan.rsn.capabilities.mfpc"]},
  ],
  "rules": ["wpa3_requires_mfpr", "sae_requires_mfpc", "no_tkip_with_wpa3"],
}
```

- `fields` is an ordered **alias list**: the first one present in that Host's
  `tshark -G fields` is used. tshark renames fields between releases; this is
  where that is absorbed instead of in the UI.
- `wlan_radio.channel` exists only with a radiotap/PPI header;
  `wlan.ds.current_channel` is the DS Parameter Set, absent in 6 GHz beacons —
  hence the alias chain.
- RSN fields repeat (several AKMs, several pairwise ciphers), so extraction uses
  `-E occurrence=a -E aggregator=,` and decoders take **lists**, never a scalar.
- Raw values are decoded in Python (`akms.type 8` → `SAE`, `2` → `PSK`), so the
  table reads like the ATLAS screenshots, and the raw value stays available on
  hover.
- **Manual** mode = empty column list + user filter + the default columns
  (No, Time, Src, Dst, Protocol, Info).

## 7. Findings (the "help me read it" part)

`backend/app/services/packet_rules.py`. A rule is a Python function over one
decoded row (or one BSSID group) returning `None` or a finding:

```python
{"rule": "wpa3_requires_mfpr", "severity": "fail",
 "row": 3, "bssid": "06:03:7f:ba:db:ad",
 "message": "AKM is SAE only (WPA3-Personal) but MFPR = 0; WPA3 requires PMF required."}
```

Initial set (each one cites the clause it checks in its docstring):

| Rule | Severity |
|---|---|
| AKM set contains only SAE-family suites (8, 9, 24, 25) and/or OWE (18), and MFPR = 0 | fail |
| Any SAE-family AKM present and MFPC = 0 | fail |
| WPA3 AKM together with TKIP group/pairwise cipher | fail |
| 6 GHz BSS without SAE/OWE, or with MFPR = 0 | fail |
| PSK + SAE transition mode (MFPC = 1, MFPR = 0) | **no finding** — valid by spec; a test asserts this |
| Same BSSID changes RSN IE / channel between beacons in one capture | warn |
| Beacon interval ≠ 100 TU, DTIM period > 3 | info |
| Hidden SSID (empty / null SSID) | info |

Rules run in the backend, not in tshark filters, so they are unit-testable
against fixture rows without a Host.

## 8. Storage

- The capture itself stays in the **Files workspace** (`data/uploads/`,
  existing 50 MB cap and extension whitelist, which already includes
  pcap/pcapng). No second upload store.
- New tables in `db/workspace.py` as `CREATE TABLE IF NOT EXISTS` in the
  schema script (`_ensure_column` is only for columns added to tables that
  already exist):
  - `packet_analyses(id, file_id, file_sha256, collector_id, mode_id,
    display_filter, status, error, frames_total, frames_shown,
    tshark_version, created_by_user_id, created_at, finished_at)`;
  - `packet_host_stage(collector_id, file_sha256, remote_path, staged_at,
    last_used_at)`.
- Rows: `data/packets/<analysis_id>.jsonl` (gitignored by the existing
  `data/` rule), plus `<analysis_id>.idx` — the byte offset of every 500th row —
  so `GET rows?offset=` seeks instead of scanning a 200 MB file. Findings: `data/packets/<analysis_id>.findings.json`.
- Authorship from the session (`created_by_user_id`), never from the client.

## 9. API (`backend/app/api/packets_api.py`)

| Method | Path | Notes |
|---|---|---|
| GET  | `/api/packets/hosts` | connected collectors + tshark status/version |
| POST | `/api/packets/hosts/{id}/probe` | §5 |
| GET  | `/api/packets/modes` | Test Modes (label, columns, rules) |
| POST | `/api/packets/analyses` | `{file_id, collector_id, mode_id, display_filter?, extra_fields?}` → `202 {id}` |
| GET  | `/api/packets/analyses/{id}` | status, counts, tshark stderr on failure, findings summary |
| GET  | `/api/packets/analyses/{id}/rows?offset=&limit=` | paged decoded rows, `limit ≤ 500` |
| GET  | `/api/packets/analyses/{id}/frames/{n}` | detail tree: `tshark -T json -Y "frame.number==n"` on the Host |
| GET  | `/api/packets/analyses/{id}/export.csv` | |
| DELETE | `/api/packets/hosts/{id}/staged/{sha256}` | remove a staged copy |

The UI polls `GET /analyses/{id}` once a second **while a job is running** —
this is a backend REST poll, not a serial capture, so the serial RPC rule does
not apply. No new `/ws`.

Gating: `app.include_router(packets_router, dependencies=[_ENGINEER])` in
`main.py`, plus `tests/test_route_protection.py` cases. Role decided in Q1 (§14).

## 10. Command construction (security)

The remote end is a shell, so every remote command is built by **one** function
in `services/tshark_runner.py` from a fixed template:

- field names: must match `^[a-z0-9_]+(\.[a-z0-9_]+)*$` **and** be in that
  Host's `tshark -G fields` list;
- display filter: length ≤ 1024, passed through `shlex.quote`; tshark itself
  rejects an invalid filter and its stderr line is returned as a 400;
- paths: built only from the sha256 hex digest, never from a filename;
- always `-n` (no name resolution — the Host must not start DNS lookups for
  every MAC/IP in a customer capture);
- tests include injection strings (`'; rm -rf ~ #`, `$(...)`, backticks,
  newlines) and assert the produced command keeps them inside one quoted
  argument.

## 11. Frontend

Following the Wi-Fi Clients template:

- `api/rest.ts`: typed functions for §9.
- `monitoring/PacketAnalysisContext.tsx`: caches analyses by id, scoped to the
  selected Host; one in-flight request per key.
- `components/PacketMonitorSection.tsx`:
  - source row: file picker (from Files workspace) + Host picker with tshark
    status dot;
  - Test Mode select + filter input (with recent-filter history in
    `localStorage`) + Run;
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

- Unit: mode/alias resolution, value decoders, every rule (one failing and one
  passing row each), command builder incl. injection cases, row paging.
- Transport: a fake `ssh` script (the runner takes an `ssh_binary` parameter,
  as `open_session` already does) that runs the remote command locally, so
  staging, checksum, timeout and stdout cap are tested without a Host. Two
  variants: the **key** path, and a **password** path whose fake prints a
  `password:` prompt on `/dev/tty` and refuses to read stdin until answered —
  the path with the pty, the drain and the threads is the one most likely to
  hang.
- Rules: the PSK+SAE transition-mode row must produce **no** finding.
- Route protection: guest gets 401/403 on every new route.
- Revert check: break one rule on purpose, watch its test go red.
- Bench: one real Host with tshark — **needs the bench owner's go-ahead**
  (shared hardware rule).

## 13. PR breakdown

1. `feat(backend): tshark runner, host probe and staging` — includes the new
   `pty_ssh.build_ssh_argv` / `spawn_and_authenticate` helpers with this one
   caller (Q5).
2. `feat(backend): test modes, decoders and findings rules` — no ssh, no Host;
   can be developed in parallel with PR 1.
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
  found; field aliases cover 3.6 and newer only.
- **Q4 — retention:** analysis results in `data/packets/` are deleted after
  30 days (swept at startup and daily); each Host keeps at most 20 staged
  captures (LRU); deleting the Files-workspace row deletes its analyses and its
  staged copies on every connected Host.
- **Q5 — existing ssh callers:** not moved in this work. The shared helpers
  land with their one new caller; moving `SerialWorker` and `open_session` onto
  them is a later, separate refactor PR.

## 15. Phase 2 sketch (live capture)

Same transport, long-lived: `tshark -i <iface> -l -T fields ...` streaming lines
on stdout, appended to the analysis's jsonl; the UI pages through it with an
`after=` cursor. Interface names come from `tshark -D` on the Host and are
matched against that list before use. Monitor-mode setup and capture
privileges (`dumpcap` capabilities / sudo) are a separate design question.

## 16. Found while reading, not part of this work

- `collector/ssh_session.py:129` reports a timeout as
  `int(deadline)` "seconds", but `deadline` is a `time.monotonic()` timestamp,
  so the message shows a meaningless large number. Separate `fix(backend)` PR.
