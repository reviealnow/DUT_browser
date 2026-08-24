// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FieldKey, LogRow } from "../offline/logParser";
import type { OfflineDutRecord } from "../offline/offlineDb";

/**
 * The offline analyzer's chart must publish its own source data.
 *
 * `dut-dashboard/CLAUDE.md`'s offline-first constraint: charts are hand-rendered
 * inline SVG, and a new chart must also emit that SVG's data as
 * `<script type="application/json">` so a later move to a charting library needs
 * no change to how the data is produced. This chart does it through the shared
 * `charts/ChartData` component — but nothing checked that the tag reaches the
 * DOM, which is the only form the constraint is actually stated in.
 *
 * So the subject here is the section that ships, not `ChartData` on its own. A
 * test that rendered the helper directly would stay green through the one edit
 * that breaks the constraint: dropping the `<ChartData />` from the chart. What
 * is asserted is the shape a migration would rely on — a parseable JSON script,
 * carrying the series that is plotted, inside the same wrapper as the `<svg>`.
 *
 * Storage is the only thing stubbed. `loadOfflineDuts` reaches for IndexedDB,
 * which is not what this is about; the series, the metric selector and the SVG
 * are all real.
 */

const RECORDS: OfflineDutRecord[] = [];

vi.mock("../offline/offlineDb", () => ({
  loadOfflineDuts: () => Promise.resolve(RECORDS),
  saveOfflineDut: () => Promise.resolve(),
  removeOfflineDut: () => Promise.resolve(),
}));

const { default: OfflineAnalyzerSection } = await import("./OfflineAnalyzerSection");

/** A parsed sample: every field present, as the parser produces it. */
function row(values: Partial<Record<FieldKey, number | string | null>>): LogRow {
  const keys: FieldKey[] = [
    "testNumber", "testTimestamp", "consoleTimestamp",
    "cpu0", "cpu1", "cpu2", "cpu3",
    "memFree", "memAvailable", "slab", "sReclaimable", "sUnreclaim",
    "conntrack", "tcp", "udp",
    "sta24", "sta5", "sta6", "staTotal",
  ];
  return Object.fromEntries(keys.map((key) => [key, values[key] ?? null])) as LogRow;
}

function record(overrides: Partial<OfflineDutRecord> = {}): OfflineDutRecord {
  return {
    id: "dut-a",
    name: "Bench AP",
    sourceFile: "bench.log",
    createdAt: 1,
    missing: 0,
    rows: [
      row({ testNumber: 1, cpu0: 91, memFree: 371328 }),
      row({ testNumber: 2, cpu0: 88, memFree: 366144 }),
    ],
    ...overrides,
  };
}

/** The chart's published data, read back out of the DOM the section rendered. */
function publishedChartData(container: HTMLElement) {
  const wrap = container.querySelector(".offline-chart-wrap");
  if (!wrap) throw new Error("the chart did not render");
  return {
    svg: wrap.querySelector("svg"),
    script: wrap.querySelector<HTMLScriptElement>('script[type="application/json"]'),
  };
}

afterEach(() => {
  RECORDS.length = 0;
  cleanup();
});

describe("the offline analyzer chart's source data", () => {
  it("emits a JSON script beside the SVG, carrying the series it plotted", async () => {
    RECORDS.push(record());
    const { container } = render(<OfflineAnalyzerSection />);
    await screen.findByText("Offline log workspace");

    const { svg, script } = publishedChartData(container);
    // "Beside its SVG" is part of the constraint: both are looked up inside the
    // one wrapper, so a script left elsewhere on the page does not satisfy it.
    expect(svg).not.toBeNull();
    expect(script).not.toBeNull();
    expect(script!.id).toBe("offline-analyzer-chart-data");

    const data = JSON.parse(script!.textContent ?? "");
    expect(data.unit).toBe("%");
    expect(data.series).toEqual([
      {
        id: "dut-a",
        name: "Bench AP",
        values: [{ x: 1, y: 91 }, { x: 2, y: 88 }],
      },
    ]);
  });

  it("republishes when the plotted metric changes", async () => {
    RECORDS.push(record());
    const { container } = render(<OfflineAnalyzerSection />);
    await screen.findByText("Offline log workspace");

    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "memFree" } });

    const { script } = publishedChartData(container);
    const data = JSON.parse(script!.textContent ?? "");
    // A tag emitted once with the first metric's numbers would be worse than no
    // tag: a migration reading it would draw a chart nobody is looking at.
    expect(data.unit).toBe("kB");
    expect(data.series[0].values).toEqual([{ x: 1, y: 371328 }, { x: 2, y: 366144 }]);
  });

  it("covers every DUT the chart draws a line for", async () => {
    RECORDS.push(record(), record({ id: "dut-b", name: "Spare AP", createdAt: 2, rows: [row({ testNumber: 1, cpu0: 74 })] }));
    const { container } = render(<OfflineAnalyzerSection />);
    await screen.findByText("Offline log workspace");

    const { svg, script } = publishedChartData(container);
    const data = JSON.parse(script!.textContent ?? "");
    expect(data.series.map((item: { name: string }) => item.name)).toEqual(["Bench AP", "Spare AP"]);
    // One published series per drawn line — the tag stands in for the picture.
    expect(svg!.querySelectorAll("polyline")).toHaveLength(data.series.length);
  });
});
