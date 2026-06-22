import { useCallback, useEffect, useRef, useState } from "react";

import { getVersion } from "../api/rest";

/**
 * Detects a backend redeploy from an already-open SPA.
 *
 * Records the version this tab booted with, then re-checks /api/version every
 * `pollMs` and whenever the tab regains focus. When the live version differs
 * from the booted one, `updateAvailable` flips true so the shell can prompt a
 * reload. The user can `dismiss()` the banner, but a *newer* version detected
 * afterwards re-raises it (we only suppress the exact version dismissed).
 *
 * Transient fetch errors during a deploy restart are swallowed (keep last
 * known), and an in-flight guard coalesces overlapping checks (StrictMode
 * double-fire + focus/poll races).
 */
export function useAppVersion(pollMs = 60000) {
  const loadedRef = useRef<string | null>(null);
  const inFlightRef = useRef(false);
  const [latest, setLatest] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);

  useEffect(() => {
    // The booted version is captured by whichever check resolves first; the
    // in-flight guard coalesces overlapping checks (StrictMode double-mount +
    // focus/poll races) without dropping the first result, so `loadedRef`
    // reliably records the version this tab actually started on.
    const check = async () => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        const info = await getVersion();
        if (loadedRef.current === null) {
          loadedRef.current = info.version;
        }
        setLatest(info.version);
      } catch {
        // Backend briefly unreachable (e.g. deploy restart) — keep last known.
      } finally {
        inFlightRef.current = false;
      }
    };

    void check();
    const interval = window.setInterval(() => void check(), pollMs);
    const onFocus = () => void check();
    const onVisible = () => {
      if (document.visibilityState === "visible") void check();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [pollMs]);

  const dismiss = useCallback(() => setDismissed(latest), [latest]);

  const updateAvailable =
    latest !== null &&
    loadedRef.current !== null &&
    latest !== loadedRef.current &&
    latest !== dismissed;

  return { updateAvailable, dismiss };
}
