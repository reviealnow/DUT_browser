# Compressing session logs

`tools/compress_session_logs.py` compresses finished `dut-session-*.log` files
in place. It is run by the operator; nothing in the dashboard calls it, and it
writes nothing without `--apply`.

## Why

sysMon output repeats itself heavily -- the same 45 interfaces and the same
process table, every cycle -- so it compresses far better than ordinary text.
Measured on a real 36-hour AP6 420E run (40,980,035 bytes, 1381 cycles):

| method | result | ratio |
|---|---|---|
| `gzip -6` (what this uses) | 1,723,696 B | **23.8x**, 0.15 s for the file |
| `gzip -9` | 1,555,349 B | 26.3x |
| `xz -6` | 1,003,372 B | 40.8x |
| `zstd -19` | 882,810 B | 46.4x |

One cycle is about **29.2 KB** (measured twice, independently: 29,225 B over
1381 cycles on the local serial path, 29,111 B over 24 cycles through a Pi).
Eight DUTs at a 30 s step come to roughly **0.4 GB a day**, which `gzip -6`
turns into about **17 MB a day**.

`gzip -6` rather than the denser options because it is stdlib, the ratio is
already free at that speed, and `.gz` opens with `zcat`/`zless` on every box on
the bench.

## What it costs: a compressed log leaves the UI

Nothing in the dashboard reads a `.gz`. Five places require the exact `.log`
suffix:

| place | effect |
|---|---|
| `main.py` `/api/logs` | globs `dut-session-*.log`; a `.gz` vanishes from Downloads |
| `main.py` `/api/logs/tail` | 400 "Not a session log" |
| `api/analyzer_api.py` | same validation |
| `api/serial_api.py` `download_log` | cannot find the source |
| `services/context_snapshot.py` `_SESSION_RE` | context stops matching the session |

So this is for logs that are **finished and being kept**, not for logs anybody
still wants to open in the app. `gunzip <file>.log.gz` brings one back, and the
app sees it again.

## Safety

The script never touches a log that is still being written. Three independent
guards, in order:

1. **`lsof`** -- any file a process currently holds open is skipped. Verified
   against the running backend: it correctly skipped the three logs the
   SerialWorkers had open.
2. **`--min-idle`** (default 15 min) -- a log written recently is skipped even
   if `lsof` is unavailable. This is why it is not optional.
3. **`--older-than`** (default 7 days) -- the age cut proper.

It also skips a log that already has a `.gz` of the same name, and empty files.
It only ever matches `dut-session-*.log`: `snapshots*.jsonl`, `duts.json` and
`collectors.json` are never candidates.

Before removing an original it **reads the archive back and compares SHA-256
against the source**. A gzip truncated by a full disk, or a file that changed
mid-read, fails that check and the original stays exactly where it was. The
archive is written to a `.partial` name and `os.replace`d into place, so an
interrupted run cannot leave a half-written `.gz` that looks complete.

`--keep-original` writes the `.gz` and removes nothing -- worth using for a
first run you want to check by hand.

## Usage

Run from `dut-dashboard/`, as with the other tools here. From anywhere else give
the script an absolute path -- a relative one resolves against your shell's
directory, not against the repository.

```bash
# look, change nothing (the default)
python3 tools/compress_session_logs.py --logs ~/Documents/DUT_browser-prod/dut-dashboard/logs

# act, on logs untouched for a week
python3 tools/compress_session_logs.py --logs ~/Documents/DUT_browser-prod/dut-dashboard/logs --older-than 7 --apply
```

`--logs` defaults to this checkout's `logs/`. **The running backend may serve
from a different tree** -- it currently serves from `~/Documents/DUT_browser-prod`
-- so point `--logs` at that one, not at wherever the script happens to live.

## Running it weekly

`launchd` rather than cron, because cron on macOS does not run when the machine
was asleep at the scheduled minute and `launchd` catches up. Save as
`~/Library/LaunchAgents/com.edimax.dut.compress-logs.plist` and load it with
`launchctl load ~/Library/LaunchAgents/com.edimax.dut.compress-logs.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.edimax.dut.compress-logs</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/iotedimax/Documents/DUT_browser/dut-dashboard/tools/compress_session_logs.py</string>
    <string>--logs</string>
    <string>/Users/iotedimax/Documents/DUT_browser-prod/dut-dashboard/logs</string>
    <string>--older-than</string>
    <string>7</string>
    <string>--apply</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>4</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/dut-compress-logs.out</string>
  <key>StandardErrorPath</key>
  <string>/tmp/dut-compress-logs.err</string>
</dict>
</plist>
```

Read the result with `tail /tmp/dut-compress-logs.out`. Start with a manual dry
run and one `--apply` by hand before scheduling anything.

## Retention

There is none, deliberately: this tool compresses and never prunes. Removing
old archives is the operator's action, as is everything else that deletes on
this bench.
