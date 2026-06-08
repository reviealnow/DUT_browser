// Built-in critical-crash keyword pattern.
// Mirrors CRITICAL_CRASH_PATTERN in pages/Dashboard.tsx (kept in sync by hand
// for now; Dashboard keeps its own copy plus user-locked keywords). The KPI
// counts only these built-in critical matches.
export const CRITICAL_CRASH_PATTERN =
  /\b(kernel panic|q6 crash|watchdog(?:\s+reset|\s+bite|\s+timeout)?)\b/i;

export function countCrashLines(lines: string[]): number {
  let count = 0;
  for (const line of lines) {
    if (CRITICAL_CRASH_PATTERN.test(line)) {
      count += 1;
    }
  }
  return count;
}
