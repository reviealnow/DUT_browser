import { describe, expect, it } from "vitest";

import { ParsedLogFile, planImport, uniqueName } from "./importPlan";
import type { LogRow } from "./logParser";
import type { OfflineDutRecord } from "./offlineDb";

/**
 * What an import saves, what it refuses, and what it says it did.
 *
 * The defect this is built around: a file with no snapshot in it was saved
 * anyway, becoming a permanent DUT with an empty chart under a message that
 * said the import had succeeded. So every case here asks two questions of one
 * outcome — what reached storage, and what the reader was told — because the
 * bug was that those two disagreed.
 */

const ROW = { testNumber: 1, memFree: 371328 } as unknown as LogRow;

/** A file the parser found snapshots in. */
function log(name: string, rows = 1, missing = 0): ParsedLogFile {
  return { name, result: { rows: Array.from({ length: rows }, () => ROW), missing } };
}

/** A file the parser found nothing in — a text file, a boot log, a PDF renamed. */
function notALog(name: string): ParsedLogFile {
  return { name, result: { rows: [], missing: 0 } };
}

/** Ids and timestamps that count up, so a test can name what it expects. */
function effects() {
  let n = 0;
  return { id: () => `id-${++n}`, now: () => 1_000 + n };
}

function saved(name: string): OfflineDutRecord {
  return { id: `saved-${name}`, name, sourceFile: `${name}.log`, createdAt: 1, rows: [], missing: 0 };
}

describe("a file with no sysMon snapshots", () => {
  it("does not become a saved DUT", () => {
    const plan = planImport([notALog("shopping-list.txt")], [], effects());

    expect(plan.records).toEqual([]);
    expect(plan.refused).toEqual(["shopping-list.txt"]);
  });

  it("is named in the notice, and nothing claims it was imported", () => {
    const plan = planImport([notALog("shopping-list.txt")], [], effects());

    expect(plan.notice).toContain("shopping-list.txt");
    expect(plan.notice).not.toMatch(/imported/);
  });

  it("does not stop the files either side of it from being saved", () => {
    const plan = planImport(
      [log("dut-a.log"), notALog("readme.txt"), log("dut-b.log")],
      [],
      effects(),
    );

    expect(plan.records.map((dut) => dut.sourceFile)).toEqual(["dut-a.log", "dut-b.log"]);
    expect(plan.refused).toEqual(["readme.txt"]);
    // Both halves in one line: what was saved, and what was not.
    expect(plan.notice).toBe(
      "2 logs imported and saved in this browser. No sysMon snapshots in readme.txt — nothing saved for it.",
    );
  });

  it("names every refused file when several are refused together", () => {
    const plan = planImport([notALog("a.txt"), notALog("b.txt")], [], effects());

    expect(plan.records).toEqual([]);
    expect(plan.notice).toBe("No sysMon snapshots in a.txt, b.txt — nothing saved for those.");
  });

  it("still says something when there is nothing at all to report", () => {
    // Not reachable from the drop zone today — it refuses an empty selection
    // before it gets here — but a plan with no notice would leave the screen
    // silent after a drop, which is the one outcome this must never produce.
    const plan = planImport([], [], effects());

    expect(plan.notice).toBe("No sysMon snapshots found in those files.");
  });
});

describe("the records an import produces", () => {
  it("carries the parser's rows and gap count, and the file it came from", () => {
    const plan = planImport([log("capture.log", 3, 6)], [], effects());

    expect(plan.records).toEqual([
      {
        id: "id-1",
        name: "capture",
        sourceFile: "capture.log",
        createdAt: 1_001,
        rows: [ROW, ROW, ROW],
        missing: 6,
      },
    ]);
  });

  it("counts one log in the singular", () => {
    expect(planImport([log("capture.log")], [], effects()).notice).toBe(
      "1 log imported and saved in this browser.",
    );
  });
});

describe("the display name a file gets", () => {
  it("drops a .log or .txt extension, and nothing else", () => {
    expect(uniqueName("capture.log", [])).toBe("capture");
    expect(uniqueName("capture.TXT", [])).toBe("capture");
    expect(uniqueName("2026-08-04.capture", [])).toBe("2026-08-04.capture");
  });

  it("falls back to DUT when the name was only an extension", () => {
    expect(uniqueName(".log", [])).toBe("DUT");
  });

  it("does not reuse the name of a DUT already saved", () => {
    expect(uniqueName("capture.log", [saved("capture")])).toBe("capture (2)");
    expect(uniqueName("capture.log", [saved("capture"), saved("capture (2)")])).toBe("capture (3)");
  });

  it("does not let two files in one drop take the same name", () => {
    // Two benches both call their capture `capture.log`. Sharing a name makes
    // them indistinguishable in the tab strip, the compare list and the legend.
    const plan = planImport([log("capture.log"), log("capture.log")], [], effects());

    expect(plan.records.map((dut) => dut.name)).toEqual(["capture", "capture (2)"]);
  });

  it("counts the saved DUTs and this drop together", () => {
    const plan = planImport([log("capture.log"), log("capture.log")], [saved("capture")], effects());

    expect(plan.records.map((dut) => dut.name)).toEqual(["capture (2)", "capture (3)"]);
  });
});
