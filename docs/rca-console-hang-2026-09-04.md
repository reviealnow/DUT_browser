# RCA — the Serial Console page hangs the browser

**2026-09-04** · `dut-dashboard` frontend · measured on `AP6420E-PB1005QPCFVFMA8`
(AP6_420E) · fixed by #164 and #165

## Symptom

Pressing **Download DUT Log** made the whole page stop responding: no rendering
updates, and the sidebar could not be clicked. It looked like the download
feature crashing the app.

## Root cause

The console rendered every line it held as **one text node**, and re-rendered on
**every batch** the backend sent.

- `ConsolePanel` rendered `{lines.join("\n")}` — the whole 1000-line window
  (~183 KB) as a single text node inside a `white-space: pre-wrap` box. Any
  change re-laid out all of it, changed or not.
- `useDutMonitor` called `setLines` once per incoming batch. The parser batches
  at 20 lines / 50 ms max latency, so a busy console produces up to 20 commits a
  second, each one re-rendering the monitor tree.

Measured in Chrome on 1000 lines of real sysMon output:

| | cost |
|---|---|
| one text node, per update | 6.46 ms |
| at 20 updates/s | **129 ms of every second** |

A **site survey** is what pushes it over: it leaves tens of thousands of
`iw scan` lines draining at 115200 baud. The session that hung had **39,052
lines** in its log. Sustained for minutes, the main thread has nothing left for
input, and the page stops answering the mouse.

**Pressing Download did not cause the hang.** It coincided with it — the console
was already saturated by the survey, and the button was simply the next thing
the operator touched.

## What it was not

Each of these was suspected, measured, and ruled out:

| Suspect | Measurement |
|---|---|
| The backend | Three `SIGUSR1` thread dumps during hangs showed it idle: `read_loop` parked in `serial.read()`, one AnyIO worker in `queue.get()`, the event loop in `select()`. Nothing blocked, nothing in flight. |
| The download endpoint | Called directly on the live 1.2 MB session log: 1.8 s, 0.7 MB zip, peak RSS 56 MB. |
| The fetch | The failing request was **422 in 19 ms**, 0.3 kB. The response arrived; the freeze came after. |
| The error banner | Suspected as the trigger, since an in-flow banner above the console forces a relayout while `position: fixed` success notices do not. **0.07 ms.** Free. |
| The crash-keyword scan | 1000 lines per batch: **0.06 ms** (1.2 ms/s). |
| The initial snapshot backfill | Bounded — 120 snapshots, 1000 lines, 200 crash lines. |

Two of these were asserted before being measured, and both were wrong. They are
listed because the numbers are the useful part.

## What changed

### #164 — one console commit per window, not one per batch

- **new** `frontend/src/monitoring/consoleLineBuffer.ts` — coalesces incoming
  lines into at most one commit per 100 ms. A window from the first pending
  line, deliberately **not** a debounce: a debounce restarts on every push, so a
  console that never goes quiet — the exact case this exists for — would never
  flush.
- **mod** `frontend/src/monitoring/useDutMonitor.ts` — pushes into the buffer
  instead of calling `setLines` per batch. Stream activity is still recorded on
  **arrival**, so "last event Ns ago" does not lag by a window. The buffer is
  cancelled, not flushed, on teardown, so a DUT switch cannot leak the previous
  DUT's last lines into the new one.

### #165 — the console renders a line at a time, not one text node

- **mod** `frontend/src/components/ConsolePanel.tsx` — one inline `<span>` per
  line, each carrying its own `"\n"`, keyed by position in the stream.
- **mod** `frontend/src/monitoring/useDutMonitor.ts` — adds `linesStartSeq`: how
  many lines have scrolled out of the window, so `linesStartSeq + index` is a
  line's position in the whole stream. `DutMonitorState` gains a field rather
  than changing the type of `lines` — the crash filter and the log export both
  consume `string[]`.
- **mod** `frontend/src/pages/Dashboard.tsx` — passes it through.

Two decisions inside #165 went against the obvious choice, both settled by
measurement rather than argument.

**Inline spans, not block elements.**

| | per update |
|---|---|
| per line, inline `<span>` | 2.25 ms |
| per line, block `<div>` | 0.50 ms — four times faster, **rejected** |

Block elements break the console as something you can copy out of: an empty
`<div>` serialises to nothing, so every blank line the DUT printed disappears
from what an operator pastes into a bug report — and the DUT prints a lot of
them. Blocks also need `line-height` pinned to give blank lines any height,
which visibly tightens the console (18 px → 14.4 px). Spans holding their own
newline serialise exactly as the joined text node did, character for character,
bar one trailing newline.

**Keyed by stream position, never by array index.**

| | per update |
|---|---|
| per line, stable keys | 2.25 ms |
| per line, `key={index}` | 7.44 ms — **worse than the original** |

The window slides, so index N names a different line after every batch and React
rewrites all thousand nodes. This is why `linesStartSeq` has to come from the
hook: nothing inside the component can tell "twenty new lines" from "the same
lines, moved up twenty".

### Net effect

| | commits/s | per commit | main thread |
|---|---|---|---|
| before | 20 | 6.46 ms | ~129 ms of every second |
| after | 10 | 2.25 ms | **~22 ms of every second** |

## Tests

Five test files, every one reverted and re-run to confirm it goes red:

| Test | Guards |
|---|---|
| `consoleLineBuffer.test.ts` | The buffer's contract, including the debounce trap. |
| `useDutMonitor.coalescing.test.tsx` | Counts renders during a flood: **20 renders without the fix, <5 with it.** |
| `ConsolePanel.test.tsx` | Node **identity** across a window slide → red on `key={index}`. Serialisation round-trip → red on block `<div>`. Both → red on `lines.join("\n")`. |

Two verification mistakes are recorded in the commit messages because they
matter more than the results:

- The first coalescing test drove twenty batches inside a single `act()`, where
  React's own batching collapses them whether the hook coalesces or not. It
  **passed against the un-coalesced code**. Each batch now gets its own `act()`,
  which is what reproduces the WebSocket task boundaries.
- The first attempt at the `ConsolePanel` reversions used a `perl` substitution
  whose `\n` escaping matched nothing, so the "reverted" file was unchanged and
  the test "passed". Each reversion now asserts its anchor is present before
  applying it.

## Verification

Real `ConsolePanel` in Chrome, 1000-line window, 20 lines every 100 ms:

```
elements 1000 / lines 1000        textContent round-trips: true
blank lines: 4, all with height   indentation preserved: true
computed line-height: normal      auto-scroll to bottom: true
median frame 16.7 ms · p95 17.4 ms · worst 17.7 ms
frames over 50 ms: 0 · 60 fps
```

Then on the bench against the real AP6_420E with a site survey driving the
console: **no hang, console renders correctly.** That run is the one that counts
— a simulated flood does not reproduce three minutes and 39,052 lines.

Gates on each PR: `pytest`, `typecheck + unit tests`, `demo kit` — all green
before merge.

## Separate finding, same day

The investigation started from a different complaint — *"the serial console
connection is unstable since the service starts"* — which turned out to be
unrelated to the frontend and unrelated to any recent change.

A `minicom` left running since the previous afternoon held
`/dev/tty.PL2303G-*`, the **tty twin** of the same USB adapter, which blocks the
`cu` node. The check this repository prescribed, `lsof /dev/cu.PL2303G-*`,
cannot see it: it returned empty while every backend open failed with
`Resource busy`.

#163 corrects all three copies of that command to cover both node families, and
makes the app say "another process already has this cable, check both device
nodes" instead of relaying pyserial's errno.

The fleet PRs merged after #150 were cleared by measurement, not by reading:
#161's identify probe returned the right hostname **13/13** under confirmed
sysMon load (0.01–0.04 s each), the whole connect-time capture batch answered
200, and the console streamed for 20 minutes without a drop.

## Note on a contaminated window

Two of the intermediate "Download hangs" tests during this investigation were
**invalid, and the cause was the investigation itself.** Load-testing a wrong
hypothesis about a backend deadlock left ~6000 `TIME_WAIT` entries against port
8000; vite's proxy connections to the backend then stalled in `SYN_SENT` (node's
proxy has no connect timeout), so browser requests never reached the server at
all.

| Path | Result |
|---|---|
| `5173 → 8000` (poisoned) | 1 in 3 connections failed, 10 s timeout |
| `5174 → 8001` (fresh) | 0 of 12 failed |

Everything in *Root cause* through *Verification* was measured on the fresh port
pair. The lesson is worth keeping: **load-testing a shared bench pollutes the
measurement, and the pollution disguises itself as a defect in the system under
test.**
