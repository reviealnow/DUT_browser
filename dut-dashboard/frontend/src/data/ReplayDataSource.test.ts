import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DashboardEvent, SnapshotPayload } from "../api/websocket";
import { consoleBlock, DEMO_DUT_ID, ReplayDataSource, ReplayDataset } from "./ReplayDataSource";

const core = (idle: number) => ({ usr: 100 - idle, sys: 0, nic: 0, idle, io: 0, irq: 0, sirq: 0 });

const DATASET: ReplayDataset = {
  interval_ms: 1000,
  records: [
    { cpu: { "0": core(90) }, memory: { MemAvailable: 500 }, wifi_clients: {} },
    { cpu: { "0": core(50) }, memory: { MemAvailable: 400 }, wifi_clients: {} },
    { cpu: { "0": core(10) }, memory: { MemAvailable: 300 }, wifi_clients: {} },
  ],
  events: [{ at: 1, line: "watchdog event" }],
};

function makeSource() {
  return new ReplayDataSource({ loader: async () => DATASET, now: () => Date.now() });
}

function snapshotsOf(events: DashboardEvent[]): SnapshotPayload[] {
  return events.flatMap((e) => (e.type === "snapshot_update" ? [e.snapshot] : []));
}

describe("ReplayDataSource", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 9, 5, 14, 30, 0));
  });
  afterEach(() => vi.useRealTimers());

  it("opens, then emits one record per interval and loops back to the first", async () => {
    const source = makeSource();
    const events: DashboardEvent[] = [];
    const onOpen = vi.fn();
    source.subscribe({ onEvent: (e) => events.push(e), onOpen }, DEMO_DUT_ID);
    await vi.advanceTimersByTimeAsync(0);
    expect(onOpen).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(4000);
    const snaps = snapshotsOf(events);
    expect(snaps.map((s) => s.cpu["0"].idle)).toEqual([90, 50, 10, 90]);
    // A looped record must carry a new clock, or the charts' per-Test-Time
    // upsert would overwrite the previous pass instead of extending it.
    expect(new Set(snaps.map((s) => s.device_ts)).size).toBe(4);
    expect(snaps.map((s) => s.test_count)).toEqual([1000, 1001, 1002, 1003]);
  });

  it("speaks the console format the backend parser reads, events included", async () => {
    const source = makeSource();
    const lines: string[] = [];
    source.subscribe(
      { onEvent: (e) => e.type === "console_line_batch" && lines.push(...e.lines) },
      DEMO_DUT_ID,
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(lines[0]).toMatch(/^= Test Time:\s*1000,\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*=*\s*$/);
    expect(lines).toContain("watchdog event");
    const cpuLine = lines.find((l) => l.startsWith("CPU0:"))!;
    // The same expression as SysMonParser.CPU_RE.
    expect(cpuLine).toMatch(
      /^CPU(\d+):\s*([\d.]+)% usr\s+([\d.]+)% sys\s+([\d.]+)% nic\s+([\d.]+)% idle\s+([\d.]+)% io\s+([\d.]+)% irq\s+([\d.]+)%%?\s+sirq\s*$/,
    );
  });

  it("backfills the records before the cursor, oldest first, ending before now", async () => {
    const source = makeSource();
    const history = await source.loadSnapshots(2, DEMO_DUT_ID);
    // Cursor 0: the two records before it wrap to the end of the dataset.
    expect(history.map((s) => s.cpu["0"].idle)).toEqual([50, 10]);
    expect(history[0].device_ts < history[1].device_ts).toBe(true);
  });

  it("feeds the fleet tagged with the demo DUT, and other DUT ids nothing", async () => {
    const source = makeSource();
    const fleet: Array<DashboardEvent & { dut_id?: string }> = [];
    const other: DashboardEvent[] = [];
    source.subscribeFleet({ onEvent: (e) => fleet.push(e) });
    source.subscribe({ onEvent: (e) => other.push(e) }, "some-other-dut");
    await vi.advanceTimersByTimeAsync(1000);
    expect(fleet.length).toBeGreaterThan(0);
    expect(fleet.every((e) => e.dut_id === DEMO_DUT_ID)).toBe(true);
    expect(other).toEqual([]);
    expect(await source.loadSnapshots(5, "some-other-dut")).toEqual([]);
  });

  it("stops its clock when the last subscriber leaves", async () => {
    const source = makeSource();
    const onEvent = vi.fn();
    const handle = source.subscribe({ onEvent }, DEMO_DUT_ID);
    await vi.advanceTimersByTimeAsync(1000);
    handle.close();
    onEvent.mockClear();
    await vi.advanceTimersByTimeAsync(5000);
    expect(onEvent).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("renders memory lines the parser's MEMINFO_RE accepts", () => {
    const lines = consoleBlock({
      test_count: 1,
      device_ts: "2026-10-05 14:30:00",
      cpu: {},
      memory: { MemAvailable: 475472 },
    });
    expect(lines).toContain("MemAvailable:     475472 kB");
    expect(lines[1]).toMatch(/^(\w+):\s+(\d+)\s*kB\s*$/);
  });
});
