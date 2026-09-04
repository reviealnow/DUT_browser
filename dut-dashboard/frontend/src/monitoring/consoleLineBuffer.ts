/**
 * Coalesce incoming console lines into at most one React commit per window.
 *
 * The backend already batches (`SysMonParser.CONSOLE_BATCH_SIZE = 20`, flushed
 * at latest every 50 ms), so a flooding console delivers up to ~20 batches a
 * second. Each one used to be its own `setLines`, and every one of those
 * re-renders the whole monitor tree and re-lays out the console.
 *
 * Measured in Chrome on the bench machine, 1000 lines of real sysMon output in
 * the console's own `pre-wrap` box: **6.25 ms per update**. At 20 updates a
 * second that is 125 ms of every second spent laying out text nobody asked to
 * see twice -- and that is the console alone, before the rest of the tree
 * re-renders with it. A site survey holds the line at that rate for minutes
 * (39,052 lines in one session on 2026-09-04), which is when the page stops
 * answering the mouse.
 *
 * Halving the commits halves all of it, and costs the reader nothing: the
 * window is shorter than a frame boundary is wide, and the lines still arrive
 * in order, complete, and within `flushMs` of the device saying them.
 *
 * Deliberately NOT a debounce. A debounce restarts its timer on every push, so
 * a console that never goes quiet -- exactly the flood this exists for -- would
 * never flush at all. This one flushes on a fixed window from the first pending
 * line, so latency has a ceiling no matter how hard the line is driven.
 */

/** One commit's worth of coalesced lines, in arrival order. */
export type LineFlush = (lines: string[]) => void;

export type LineBuffer = {
  /** Queue lines for the next flush. Starts the window if none is open. */
  push: (lines: string[]) => void;
  /** Emit anything pending now and close the window. */
  flushNow: () => void;
  /** Drop anything pending and close the window (teardown, DUT switch). */
  cancel: () => void;
};

export const CONSOLE_FLUSH_MS = 100;

export function createLineBuffer(onFlush: LineFlush, flushMs = CONSOLE_FLUSH_MS): LineBuffer {
  let pending: string[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;

  const clearTimer = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  };

  const flushNow = () => {
    clearTimer();
    if (pending.length === 0) {
      return;
    }
    // Handed over before the callback runs: onFlush sets React state, and a
    // re-entrant push from that render must queue for the NEXT window rather
    // than land in the array being emitted.
    const emitting = pending;
    pending = [];
    onFlush(emitting);
  };

  return {
    push(lines: string[]) {
      if (lines.length === 0) {
        return;
      }
      pending = pending.length === 0 ? lines.slice() : pending.concat(lines);
      if (timer === null) {
        timer = setTimeout(flushNow, flushMs);
      }
    },
    flushNow,
    cancel() {
      clearTimer();
      pending = [];
    },
  };
}
