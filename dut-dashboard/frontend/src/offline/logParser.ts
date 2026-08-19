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

function readNumber(block: string, pattern: RegExp): number | null {
  const match = block.match(pattern);
  return match ? Number(match[1]) : null;
}

export function parseLog(text: string): { rows: LogRow[]; missing: number } {
  const marker = /^(?:\[([^\]]+)\]\s*)?=\s*Test Time:\s*(\d+)\s*,\s*([^=\r\n]+?)\s*=\s*$/gm;
  const starts: Array<{ index: number; consoleTimestamp: string | null; sourceNumber: number; testTimestamp: string }> = [];
  let match: RegExpExecArray | null;
  while ((match = marker.exec(text))) {
    starts.push({
      index: match.index,
      consoleTimestamp: match[1]?.trim() ?? null,
      sourceNumber: Number(match[2]),
      testTimestamp: match[3].trim(),
    });
  }

  let expected = starts.length ? Math.min(...starts.map((start) => start.sourceNumber)) : 1;
  let segmentStarted = false;
  const selected = starts.filter((start) => {
    if (segmentStarted && start.sourceNumber === 1 && expected > 2) {
      expected = 2;
      return true;
    }
    if (start.sourceNumber !== expected) return false;
    segmentStarted = true;
    expected += 1;
    return true;
  });

  const rows = selected.map((start): LogRow => {
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
      memFree: readNumber(block, /\]\s+MemFree:\s+(\d+)\s+kB/),
      memAvailable: readNumber(block, /\]\s+MemAvailable:\s+(\d+)\s+kB/),
      slab: readNumber(block, /\]\s+Slab:\s+(\d+)\s+kB/),
      sReclaimable: readNumber(block, /\]\s+SReclaimable:\s+(\d+)\s+kB/),
      sUnreclaim: readNumber(block, /\]\s+SUnreclaim:\s+(\d+)\s+kB/),
      conntrack: readNumber(block, /Total Conntrack Connections:\s*(\d+)/),
      tcp: readNumber(block, /Active TCP Sockets:\s*(\d+)/),
      udp: readNumber(block, /Active UDP Sockets:\s*(\d+)/),
      sta24: readNumber(block, /Connected STA Summary\s*->\s*2\.4GHz:\s*(\d+)/),
      sta5: readNumber(block, /Connected STA Summary[^\r\n]*?5GHz:\s*(\d+)/),
      sta6: readNumber(block, /Connected STA Summary[^\r\n]*?6GHz:\s*(\d+)/),
      staTotal: readNumber(block, /Connected STA Summary[^\r\n]*?Total:\s*(\d+)/),
    };
  });
  const missing = rows.reduce(
    (total, row) => total + LOG_FIELDS.filter((field) => row[field.key] === null).length,
    0,
  );
  return { rows, missing };
}
