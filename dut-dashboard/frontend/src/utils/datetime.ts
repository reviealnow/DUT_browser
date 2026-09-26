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

/**
 * "just now" / "3m ago" / "2h ago" from an ISO timestamp; "" if unparseable.
 *
 * Moved here from the Overview's channel recommendation when the Wi-Fi cards
 * needed the same scan-age wording.
 */
export function formatAge(iso: string | null): string {
  if (!iso) {
    return "";
  }
  const then = Date.parse(iso);
  if (Number.isNaN(then)) {
    return "";
  }
  const sec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (sec < 60) {
    return "just now";
  }
  if (sec < 3600) {
    return `${Math.floor(sec / 60)}m ago`;
  }
  return `${Math.floor(sec / 3600)}h ago`;
}
