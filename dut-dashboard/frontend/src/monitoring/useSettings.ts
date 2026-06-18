import { useCallback, useState } from "react";

export type AccentPreset = { name: string; accent: string; weak: string };

export const ACCENT_PRESETS: AccentPreset[] = [
  { name: "Blue", accent: "#1565c0", weak: "#e8f0fb" },
  { name: "Teal", accent: "#0e7c86", weak: "#e2f3f4" },
  { name: "Violet", accent: "#6d28d9", weak: "#efe9fb" },
  { name: "Green", accent: "#0c7a43", weak: "#e4f4ea" },
  { name: "Slate", accent: "#475569", weak: "#eef1f5" },
];

export type Settings = { accent: string; defaultBaud: number; displayName: string };

const DEFAULTS: Settings = { accent: "#1565c0", defaultBaud: 115200, displayName: "" };
const KEY = "dut.settings.v1";
const CRASH_KEY = "dut.crashKeywords.v1";

export function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw);
    return {
      accent: typeof parsed.accent === "string" ? parsed.accent : DEFAULTS.accent,
      defaultBaud: Number.isFinite(parsed.defaultBaud) ? parsed.defaultBaud : DEFAULTS.defaultBaud,
      displayName: typeof parsed.displayName === "string" ? parsed.displayName : DEFAULTS.displayName,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

/** The free-text display name used as uploader/author in the Workspace. */
export function loadDisplayName(): string {
  return loadSettings().displayName.trim();
}

/** Apply the accent to the whole dashboard via the design-system CSS vars. */
export function applyAccent(accent: string): void {
  const preset = ACCENT_PRESETS.find((p) => p.accent === accent);
  document.documentElement.style.setProperty("--accent", accent);
  document.documentElement.style.setProperty("--accent-weak", preset?.weak ?? DEFAULTS.accent);
}

export function loadCrashKeywords(): string[] {
  try {
    const raw = localStorage.getItem(CRASH_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function saveCrashKeywords(keywords: string[]): void {
  try {
    localStorage.setItem(CRASH_KEY, JSON.stringify(keywords));
  } catch {
    // ignore (private mode / quota)
  }
}

export function useSettings() {
  const [settings, setSettings] = useState<Settings>(loadSettings);

  const persist = useCallback((patch: Partial<Settings>) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      try {
        localStorage.setItem(KEY, JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  const setAccent = useCallback(
    (accent: string) => {
      applyAccent(accent);
      persist({ accent });
    },
    [persist],
  );

  const setDefaultBaud = useCallback((defaultBaud: number) => persist({ defaultBaud }), [persist]);

  const setDisplayName = useCallback((displayName: string) => persist({ displayName }), [persist]);

  return { settings, setAccent, setDefaultBaud, setDisplayName };
}
