/**
 * One timestamp format for the whole dashboard.
 *
 * Everything the backend hands the UI is already a readable stamp — an ISO
 * string from a log listing, or SQLite's `YYYY-MM-DD HH:MM:SS` from the
 * workspace tables. The only thing worth doing to either is dropping the `T`
 * that makes an ISO one read like a machine field; a locale conversion would
 * make two colleagues looking at the same bench read different times for the
 * same row.
 *
 * Extracted from Downloads and Bulletin, which had grown one identical copy
 * each.
 */
export function formatTimestamp(value: string): string {
  return value.replace("T", " ");
}
