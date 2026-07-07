/**
 * Deterministic per-author colour for Bulletin name tags.
 *
 * The same name always maps to the same colour with no stored state and no cap
 * on the number of authors: the name is hashed to an HSL hue, then a fixed
 * saturation/lightness gives every tag a soft pastel background with dark,
 * readable text (lightness is held constant so contrast is consistent across the
 * hue wheel). Anonymous names ("—" / empty) get a neutral grey.
 */

export type AuthorTagColor = { bg: string; fg: string };

const NEUTRAL: AuthorTagColor = { bg: "var(--bg)", fg: "var(--muted)" };

/** Stable 0–359 hue from a string (djb2-ish; unsigned). Also reused for the
 * file-type chip fallback colour in FilesSection. */
export function hashHue(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  }
  return hash % 360;
}

export function authorColor(name: string | null | undefined): AuthorTagColor {
  const trimmed = (name ?? "").trim();
  if (!trimmed || trimmed === "—") {
    return NEUTRAL;
  }
  const hue = hashHue(trimmed);
  return { bg: `hsl(${hue} 65% 86%)`, fg: `hsl(${hue} 72% 28%)` };
}
