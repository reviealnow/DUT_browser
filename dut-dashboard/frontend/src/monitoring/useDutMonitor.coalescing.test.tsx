// @vitest-environment jsdom
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * That the hook actually coalesces -- not merely that a buffer module exists.
 *
 * `consoleLineBuffer.test.ts` holds the buffer to its contract, and would go on
 * passing if somebody dropped it from `useDutMonitor` and went back to a
 * `setLines` per batch. That is the regression this file exists to catch: the
 * defect was never in the buffer, it was in how often the tree re-rendered.
 *
 * So this counts RENDERS while a flood arrives. On the bench, a site survey
 * delivered 39,052 lines through ~20 batches a second for minutes, and the page
 * stopped answering the mouse.
 */

const listeners: { onEvent?: (event: unknown) => void; onOpen?: () => void } = {};

vi.mock("../api/websocket", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/websocket")>()),
  connectDashboardWebSocket: (handlers: { onEvent?: (e: unknown) => void; onOpen?: () => void }) => {
    listeners.onEvent = handlers.onEvent;
    listeners.onOpen = handlers.onOpen;
    return { close: () => {} };
  },
}));

// Backfill would fetch; jsdom has no server and the batching is what is under
// test, so both endpoints answer empty.
vi.mock("../api/rest", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rest")>()),
  getSnapshots: async () => [],
  getConsoleTail: async () => [],
}));

vi.mock("./useCrashKeywords", () => ({
  useCrashKeywords: () => ({ keywords: [], pattern: /kernel panic/i, saving: false, saveKeywords: async () => {} }),
}));

const { useDutMonitor } = await import("./useDutMonitor");

let renderCount = 0;
let lastLines: string[] = [];

function Probe() {
  const { lines } = useDutMonitor("default");
  renderCount += 1;
  lastLines = lines;
  return null;
}

beforeEach(() => {
  renderCount = 0;
  lastLines = [];
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useDutMonitor under a console flood", () => {
  it("does not re-render once per batch", () => {
    render(<Probe />);
    const before = renderCount;

    // Each batch in its OWN act(), which is the part that has to be right:
    // React 18 batches every update made inside one task, so a loop wrapped in a
    // single act() collapses to a couple of renders whether this hook coalesces
    // or not -- the first version of this test passed against the un-coalesced
    // code for exactly that reason. A WebSocket message arrives in a task of its
    // own, and separate act() calls are what reproduces that.
    for (let batch = 0; batch < 20; batch++) {
      act(() => {
        listeners.onEvent?.({
          type: "console_line_batch",
          lines: Array.from({ length: 20 }, (_, i) => `batch ${batch} line ${i}`),
        });
      });
      act(() => {
        vi.advanceTimersByTime(5); // ~20 batches/s, the rate a survey produces
      });
    }
    act(() => {
      vi.advanceTimersByTime(200);
    });

    const rendersCausedByTheFlood = renderCount - before;
    // Un-coalesced this is 20 -- one commit per batch. Coalesced, the whole
    // burst spans ~100ms, so it is a couple of commits.
    expect(rendersCausedByTheFlood).toBeLessThan(5);
    // ... and not by losing lines: all 400 arrived, capped only by MAX_LINES.
    expect(lastLines).toHaveLength(400);
    expect(lastLines[0]).toBe("batch 0 line 0");
    expect(lastLines[lastLines.length - 1]).toBe("batch 19 line 19");
  });
});
