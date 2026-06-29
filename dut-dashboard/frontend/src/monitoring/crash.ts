export const DEFAULT_CRASH_KEYWORDS = ["kernel panic", "q6 crash", "watchdog"];

// Built-in fallback pattern used before the backend responds.
export const CRITICAL_CRASH_PATTERN = buildCrashPattern(DEFAULT_CRASH_KEYWORDS);

export function buildCrashPattern(keywords: string[]): RegExp {
  if (keywords.length === 0) return /(?!)/; // never matches
  const alts = keywords.map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  return new RegExp(`(?:${alts})`, "i");
}

export function countCrashLines(lines: string[], pattern?: RegExp): number {
  const re = pattern ?? CRITICAL_CRASH_PATTERN;
  let count = 0;
  for (const line of lines) {
    if (re.test(line)) count += 1;
  }
  return count;
}
