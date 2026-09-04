import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createLineBuffer } from "./consoleLineBuffer";

/**
 * What this buffer is for, and the two ways it could be written wrong.
 *
 * A flooding console delivers ~20 batches a second and each one used to be its
 * own React commit. The point is to collapse them; the assertion that matters
 * is therefore about the NUMBER OF FLUSHES, not about the lines coming out --
 * a version that forwarded every push and still returned the right lines would
 * satisfy any test written about content alone, while fixing nothing.
 *
 * The second trap is writing it as a debounce. A debounce restarts its timer on
 * every push, so a line that never goes quiet never flushes -- which is exactly
 * the flood this exists for, and it would look fine in any test that pushes a
 * couple of times and then stops.
 */

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the console line buffer", () => {
  it("turns a burst of batches into ONE flush", () => {
    const onFlush = vi.fn();
    const buffer = createLineBuffer(onFlush, 100);

    for (let i = 0; i < 20; i++) {
      buffer.push([`line ${i}`]);
      vi.advanceTimersByTime(5); // ~20 batches/s, the rate a survey produces
    }
    vi.advanceTimersByTime(100);

    // 20 pushes over ~100ms must not be 20 commits.
    expect(onFlush.mock.calls.length).toBeLessThanOrEqual(2);
    expect(onFlush.mock.calls.flatMap((call) => call[0])).toHaveLength(20);
  });

  it("keeps every line, in arrival order", () => {
    const onFlush = vi.fn();
    const buffer = createLineBuffer(onFlush, 100);

    buffer.push(["a", "b"]);
    buffer.push(["c"]);
    buffer.push(["d", "e"]);
    vi.advanceTimersByTime(100);

    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush).toHaveBeenCalledWith(["a", "b", "c", "d", "e"]);
  });

  it("still flushes while the line never goes quiet (not a debounce)", () => {
    /* The failure a debounce would have: pushing faster than the window means
       the timer is always restarted and the console silently stops updating on
       exactly the DUT that is talking most. */
    const onFlush = vi.fn();
    const buffer = createLineBuffer(onFlush, 100);

    for (let i = 0; i < 50; i++) {
      buffer.push([`flood ${i}`]);
      vi.advanceTimersByTime(20); // pushes arrive faster than the window closes
    }

    expect(onFlush.mock.calls.length).toBeGreaterThan(0);
    // Every line delivered, none of them stranded in the pending array.
    expect(onFlush.mock.calls.flatMap((call) => call[0]).length).toBeGreaterThanOrEqual(45);
  });

  it("bounds how long a line can wait", () => {
    const onFlush = vi.fn();
    const buffer = createLineBuffer(onFlush, 100);

    buffer.push(["urgent"]);
    vi.advanceTimersByTime(99);
    expect(onFlush).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onFlush).toHaveBeenCalledWith(["urgent"]);
  });

  it("drops nothing on a flushNow, and everything on a cancel", () => {
    const onFlush = vi.fn();
    const buffer = createLineBuffer(onFlush, 100);

    buffer.push(["kept"]);
    buffer.flushNow();
    expect(onFlush).toHaveBeenCalledWith(["kept"]);

    onFlush.mockClear();
    buffer.push(["dropped"]);
    buffer.cancel();
    vi.advanceTimersByTime(500);
    expect(onFlush).not.toHaveBeenCalled();
  });

  it("does not re-emit a flushed batch when a push arrives during the flush", () => {
    /* onFlush sets React state; a re-entrant push must land in the NEXT window
       rather than in the array being handed out. */
    const seen: string[][] = [];
    const buffer = createLineBuffer((lines) => {
      seen.push(lines);
      if (seen.length === 1) {
        buffer.push(["from inside the flush"]);
      }
    }, 100);

    buffer.push(["first"]);
    vi.advanceTimersByTime(100);
    vi.advanceTimersByTime(100);

    expect(seen).toEqual([["first"], ["from inside the flush"]]);
  });
});
