/**
 * Reading a sysMon capture in the browser.
 *
 * **This is the third parser of this log format, and the other two are in
 * Python.** `= Test Time: N, <timestamp> =` is written by
 * `dut-dashboard/scripts/sysMon.sh`, and read by:
 *
 *   - `backend/app/parser/sysmon_parser.py` (`SNAPSHOT_RE`) — live telemetry
 *   - `tools/analyzer3.py` (`ts_pattern`) — offline analysis into PNGs
 *   - this file — the same log, with no backend and no DUT
 *
 * The duplication is deliberate: this feature exists precisely so a log can be
 * read with nothing running. Silent divergence between the three is not, and
 * has already cost one defect — see `snapshotMarker` below. **Change the format
 * and you are changing three parsers**; each of the three names the other two
 * so the next person finds them.
 */

export type FieldKey =
  | "testNumber"
  | "testTimestamp"
  | "consoleTimestamp"
  | "cpu0"
  | "cpu1"
  | "cpu2"
  | "cpu3"
  | "memFree"
  | "memAvailable"
  | "slab"
  | "sReclaimable"
  | "sUnreclaim"
  | "conntrack"
  | "tcp"
  | "udp"
  | "sta24"
  | "sta5"
  | "sta6"
  | "staTotal";

export type LogRow = Record<FieldKey, number | string | null>;

export type LogField = {
  key: FieldKey;
  label: string;
  unit: string;
};

export const LOG_FIELDS: LogField[] = [
  ["testNumber", "Test Times", ""],
  ["testTimestamp", "Test Timestamp", ""],
  ["consoleTimestamp", "Console Timestamp", ""],
  ["cpu0", "CPU0 (idle)", "%"],
  ["cpu1", "CPU1 (idle)", "%"],
  ["cpu2", "CPU2 (idle)", "%"],
  ["cpu3", "CPU3 (idle)", "%"],
  ["memFree", "MemFree", "kB"],
  ["memAvailable", "MemAvailable", "kB"],
  ["slab", "Slab", "kB"],
  ["sReclaimable", "SReclaimable", "kB"],
  ["sUnreclaim", "SUnreclaim", "kB"],
  ["conntrack", "Total Conntrack", "connections"],
  ["tcp", "Active TCP Sockets", "sockets"],
  ["udp", "Active UDP Sockets", "sockets"],
  ["sta24", "Connected 2.4GHz", "STA"],
  ["sta5", "Connected 5GHz", "STA"],
  ["sta6", "Connected 6GHz", "STA"],
  ["staTotal", "Connected STA", "STA"],
].map(([key, label, unit]) => ({ key: key as FieldKey, label, unit }));

export const NUMERIC_LOG_FIELDS = LOG_FIELDS.filter(
  (field) => !["testNumber", "testTimestamp", "consoleTimestamp"].includes(field.key),
);

/**
 * A console line may carry a capture tool's own `[…]` timestamp in front of it.
 *
 * The dashboard's own logger writes lines verbatim (`SerialWorker._write_log_line`)
 * so its captures have no prefix, and the excerpt shipped in the demo kit reads
 * `MemFree:          371328 kB`. Something else — `screen -L`, a minicom with
 * timestamping — does prefix, which is why the snapshot marker has always
 * tolerated one.
 *
 * **Optional, and the same rule for every field.** The five memory fields
 * required the prefix while the marker made it optional, so on a log this
 * repository actually produces the marker matched and every memory value came
 * back null: the chart and the table showed N/A for MemFree, MemAvailable,
 * Slab, SReclaimable and SUnreclaim, on every real capture.
 */
const LINE_START = String.raw`^(?:\[[^\]]*\]\s*)?`;

/** Anchored at a line start so a value cannot be read out of prose. */
function fieldPattern(name: string, value: string): RegExp {
  return new RegExp(`${LINE_START}${name}:\\s*${value}`, "m");
}

function readNumber(block: string, pattern: RegExp): number | null {
  const match = block.match(pattern);
  return match ? Number(match[1]) : null;
}

export function parseLog(text: string): { rows: LogRow[]; missing: number } {
  const snapshotMarker = new RegExp(
    `${LINE_START}=\\s*Test Time:\\s*(\\d+)\\s*,\\s*([^=\\r\\n]+?)\\s*=\\s*$`,
    "gm",
  );
  const starts: Array<{ index: number; consoleTimestamp: string | null; sourceNumber: number; testTimestamp: string }> = [];
  let match: RegExpExecArray | null;
  while ((match = snapshotMarker.exec(text))) {
    const prefix = /^\[([^\]]*)\]/.exec(match[0]);
    starts.push({
      index: match.index,
      consoleTimestamp: prefix ? prefix[1].trim() : null,
      sourceNumber: Number(match[1]),
      testTimestamp: match[2].trim(),
    });
  }

  // Every marker is a snapshot the DUT wrote, so every marker is a row.
  //
  // This used to keep only a strictly consecutive run starting at the file's
  // lowest number, which silently dropped real data twice over: a log whose
  // numbers went 1, 3, 4 — one dropped serial line is enough — kept only the
  // first, and a capture that began mid-run at 50, 51 and then saw sysMon
  // restart at 1 lost the whole pre-restart segment. The user saw fewer points
  // than the file contained, with nothing saying so.
  //
  // A number going backwards means sysMon restarted; that is a new segment, not
  // a reason to discard either side of it.
  const rows = starts.map((start): LogRow => {
    const nextStart = starts.find((candidate) => candidate.index > start.index);
    const block = text.slice(start.index, nextStart?.index ?? text.length);
    return {
      testNumber: start.sourceNumber,
      testTimestamp: start.testTimestamp,
      consoleTimestamp: start.consoleTimestamp,
      cpu0: readNumber(block, /CPU0:.*?([\d.]+)%\s+idle/),
      cpu1: readNumber(block, /CPU1:.*?([\d.]+)%\s+idle/),
      cpu2: readNumber(block, /CPU2:.*?([\d.]+)%\s+idle/),
      cpu3: readNumber(block, /CPU3:.*?([\d.]+)%\s+idle/),
      memFree: readNumber(block, fieldPattern("MemFree", String.raw`(\d+)\s+kB`)),
      memAvailable: readNumber(block, fieldPattern("MemAvailable", String.raw`(\d+)\s+kB`)),
      slab: readNumber(block, fieldPattern("Slab", String.raw`(\d+)\s+kB`)),
      sReclaimable: readNumber(block, fieldPattern("SReclaimable", String.raw`(\d+)\s+kB`)),
      sUnreclaim: readNumber(block, fieldPattern("SUnreclaim", String.raw`(\d+)\s+kB`)),
      conntrack: readNumber(block, /Total Conntrack Connections:\s*(\d+)/),
      tcp: readNumber(block, /Active TCP Sockets:\s*(\d+)/),
      udp: readNumber(block, /Active UDP Sockets:\s*(\d+)/),
      sta24: readNumber(block, /Connected STA Summary\s*->\s*2\.4GHz:\s*(\d+)/),
      sta5: readNumber(block, /Connected STA Summary[^\r\n]*?5GHz:\s*(\d+)/),
      sta6: readNumber(block, /Connected STA Summary[^\r\n]*?6GHz:\s*(\d+)/),
      staTotal: readNumber(block, /Connected STA Summary[^\r\n]*?Total:\s*(\d+)/),
    };
  });
  // Gaps in what this log *does* report, not the distance between it and the
  // widest schema this parser knows.
  //
  // Counting every null said "39 missing values" about three complete captures.
  // Two reasons, neither of them damage: `consoleTimestamp` is absent from every
  // log the dashboard writes itself, because its logger does not prefix lines;
  // and four fields — conntrack, the two socket counts and the STA summary —
  // have no producer anywhere in this repository. `scripts/sysMon.sh` does not
  // emit them and neither Python parser knows them, so a DUT running a different
  // vintage of the script may report them and the one here never will.
  //
  // A field no snapshot in this file carries is this log's shape. A field some
  // snapshots carry and others do not is a gap, and that is what a reader is
  // being told about.
  const reported = LOG_FIELDS.filter((field) =>
    rows.some((row) => row[field.key] !== null),
  );
  const missing = rows.reduce(
    (total, row) => total + reported.filter((field) => row[field.key] === null).length,
    0,
  );
  return { rows, missing };
}
