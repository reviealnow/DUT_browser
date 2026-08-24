import type { LogRow } from "./logParser";
import type { OfflineDutRecord } from "./offlineDb";

/**
 * What an import of dropped files does, decided before anything is written.
 *
 * Lives outside `OfflineAnalyzerSection` because it is a decision, not a piece
 * of React: given what the parser found in each file and the DUTs already
 * saved, which files become records, which are refused, and what the reader is
 * told. The component keeps the effects — reading the files, minting an id,
 * writing to IndexedDB, moving the selection.
 *
 * The rule that earned the extraction: **a file with no snapshot in it is not a
 * sysMon log, whatever it is called.** Saved anyway, such a file became a
 * permanent DUT with an empty chart under a success message — the import
 * reported what it had done rather than what it had found. That is a decision
 * about text and counts, with no browser in it, and it was reviewed rather than
 * tested because it was written inside a component nothing could call.
 */

/** One accepted file, already read and parsed. */
export type ParsedLogFile = {
  /** The file's own name — what the notice names, and the DUT's default name. */
  name: string;
  result: { rows: LogRow[]; missing: number };
};

/** The two values a record needs that no decision can produce. */
export type ImportEffects = {
  /** A fresh record id — `crypto.randomUUID` in the browser. */
  id: () => string;
  /** Now, in epoch milliseconds; records are listed in this order. */
  now: () => number;
};

export type ImportPlan = {
  /** The records to save, in file order. Empty when nothing was a sysMon log. */
  records: OfflineDutRecord[];
  /** The files refused for carrying no snapshot, in file order. */
  refused: string[];
  /** What to put on screen, whatever the outcome. Never empty. */
  notice: string;
};

/**
 * A display name no saved DUT is using yet.
 *
 * Two logs called `capture.log` from two benches are the normal case, and a
 * second DUT sharing the first one's name is indistinguishable from it in the
 * tab strip, the compare list and the chart legend.
 */
export function uniqueName(filename: string, duts: OfflineDutRecord[]): string {
  const base = filename.replace(/\.(log|txt)$/i, "") || "DUT";
  const used = new Set(duts.map((dut) => dut.name));
  let name = base;
  let suffix = 2;
  while (used.has(name)) name = `${base} (${suffix++})`;
  return name;
}

export function planImport(
  files: ParsedLogFile[],
  existing: OfflineDutRecord[],
  { id, now }: ImportEffects,
): ImportPlan {
  const records: OfflineDutRecord[] = [];
  const refused: string[] = [];

  for (const file of files) {
    if (file.result.rows.length === 0) {
      refused.push(file.name);
      continue;
    }
    records.push({
      id: id(),
      // Against the records this import is about to add as well as the saved
      // ones: two files of the same name in one drop collide with each other.
      name: uniqueName(file.name, [...existing, ...records]),
      sourceFile: file.name,
      createdAt: now(),
      rows: file.result.rows,
      missing: file.result.missing,
    });
  }

  // Named, not counted. "1 file skipped" leaves the reader to work out which of
  // the four they dropped is not on screen.
  const refusedNotice = refused.length
    ? ` No sysMon snapshots in ${refused.join(", ")} — nothing saved for ${refused.length === 1 ? "it" : "those"}.`
    : "";

  if (records.length === 0) {
    return { records, refused, notice: refusedNotice.trim() || "No sysMon snapshots found in those files." };
  }
  return {
    records,
    refused,
    notice: `${records.length} log${records.length === 1 ? "" : "s"} imported and saved in this browser.${refusedNotice}`,
  };
}
