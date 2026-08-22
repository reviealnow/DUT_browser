import { describe, expect, it } from "vitest";

import { LOG_FIELDS, parseLog } from "./logParser";

/**
 * What this parser gets out of a real sysMon capture.
 *
 * The fixture below is the shape `scripts/sysMon.sh` actually writes — `run cat
 * /proc/meminfo` emits `/proc/meminfo` verbatim, and `SerialWorker` writes
 * console lines with no prefix of its own. That matters more than usual here:
 * the five memory fields used to require a `]` in front of the field name,
 * which no log from this bench has, so every real capture charted MemFree,
 * MemAvailable, Slab, SReclaimable and SUnreclaim as N/A. Nothing caught it
 * because nothing tested this against a log.
 */
const SNAPSHOT = (n: number, ts: string, memFree: number) => `
= Test Time: ${n}, ${ts}                                              =
=== CPU Utilization =================================================================
CPU0:   12.5% user   4.0% sys   82.5% idle
CPU1:    8.0% user   2.0% sys   90.0% idle
=== Memory Utilization (/proc/meminfo) ==============================================
----- cat /proc/meminfo -----
MemTotal:         968232 kB
MemFree:          ${memFree} kB
MemAvailable:     454580 kB
Slab:              47116 kB
SReclaimable:      12044 kB
SUnreclaim:        35072 kB
`;

const TWO_SNAPSHOTS = SNAPSHOT(1, "2026-08-04 12:44:01", 371328)
  + SNAPSHOT(2, "2026-08-04 12:45:01", 338352);

describe("reading a capture this repository actually produces", () => {
  it("reads the memory fields out of an unprefixed /proc/meminfo", () => {
    const { rows } = parseLog(TWO_SNAPSHOTS);

    expect(rows).toHaveLength(2);
    expect(rows[0].memFree).toBe(371328);
    expect(rows[0].memAvailable).toBe(454580);
    expect(rows[0].slab).toBe(47116);
    expect(rows[0].sReclaimable).toBe(12044);
    expect(rows[0].sUnreclaim).toBe(35072);
    expect(rows[1].memFree).toBe(338352);
  });

  it("reads them just as well when a capture tool prefixes every line", () => {
    // `screen -L` and a timestamping minicom both do this; the snapshot marker
    // has always tolerated it, and the field patterns now agree with it.
    const prefixed = TWO_SNAPSHOTS.split("\n")
      .map((line) => (line ? `[2026-08-04 12:44:01] ${line}` : line))
      .join("\n");

    const { rows } = parseLog(prefixed);

    expect(rows).toHaveLength(2);
    expect(rows[0].memFree).toBe(371328);
    expect(rows[0].consoleTimestamp).toBe("2026-08-04 12:44:01");
  });

  it("reads the CPU idle percentages", () => {
    const { rows } = parseLog(TWO_SNAPSHOTS);
    expect(rows[0].cpu0).toBe(82.5);
    expect(rows[0].cpu1).toBe(90);
  });

  it("survives CRLF and a truncated final snapshot", () => {
    const truncated = (TWO_SNAPSHOTS + "= Test Time: 3, 2026-08-04 12:46:01     =\nCPU0:")
      .replace(/\n/g, "\r\n");

    const { rows } = parseLog(truncated);

    expect(rows).toHaveLength(3);
    expect(rows[2].memFree).toBeNull();
    expect(rows[0].memFree).toBe(371328);
  });

  it("finds nothing in a file that is not a sysMon log", () => {
    expect(parseLog("a shopping list\nmilk\n").rows).toHaveLength(0);
    expect(parseLog("").rows).toHaveLength(0);
  });
});

describe("every snapshot in the file is a row", () => {
  it("keeps a snapshot whose number was skipped", () => {
    // One dropped serial line is enough to make the numbering jump, and the
    // rows either side of it are still measurements the DUT wrote.
    const gapped = SNAPSHOT(1, "t1", 100) + SNAPSHOT(3, "t3", 300) + SNAPSHOT(4, "t4", 400);

    const { rows } = parseLog(gapped);

    expect(rows.map((row) => row.testNumber)).toEqual([1, 3, 4]);
  });

  it("keeps both sides of a sysMon restart", () => {
    // A capture started mid-run, then sysMon restarted and numbering went back
    // to 1. The pre-restart segment used to vanish entirely.
    const restarted = SNAPSHOT(50, "t50", 500) + SNAPSHOT(51, "t51", 510)
      + SNAPSHOT(1, "t1", 100) + SNAPSHOT(2, "t2", 200);

    const { rows } = parseLog(restarted);

    expect(rows.map((row) => row.testNumber)).toEqual([50, 51, 1, 2]);
  });
});

describe("what 'missing values' counts", () => {
  it("does not count fields this log never carries", () => {
    // `scripts/sysMon.sh` emits no conntrack, socket or STA-summary lines, and
    // the dashboard's own logger writes no console timestamp. Counting those as
    // missing reported the distance between the log and the widest schema this
    // parser knows, and read on screen as damage to the capture.
    const { rows, missing } = parseLog(TWO_SNAPSHOTS);

    expect(rows).toHaveLength(2);
    expect(missing).toBe(0);
  });

  it("counts a field that some snapshots carry and others do not", () => {
    const partial = TWO_SNAPSHOTS + `
= Test Time: 3, 2026-08-04 12:46:01                                   =
CPU0:   10.0% user   4.0% sys   86.0% idle
`;

    const { missing } = parseLog(partial);

    // Six: the third snapshot carries neither the memory block — five fields
    // the other two report — nor a CPU1 line, which they also report. Every
    // field some snapshot in this log carries counts against the ones that
    // do not, which is what a reader means by a gap.
    expect(missing).toBe(6);
  });

  it("never counts more than the fields it knows", () => {
    const { rows, missing } = parseLog(TWO_SNAPSHOTS);
    expect(missing).toBeLessThanOrEqual(rows.length * LOG_FIELDS.length);
  });
});
