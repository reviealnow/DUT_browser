/**
 * Fetches crash keywords from the backend once per page load (module-level
 * singleton) so every consumer (useDutMonitor, useFleetMonitor, Dashboard,
 * SettingsSection) shares one network request and one RegExp.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getCrashKeywords, putCrashKeywords } from "../api/rest";
import { buildCrashPattern, DEFAULT_CRASH_KEYWORDS } from "./crash";

// Module-level singleton: resolved once, shared across all hook instances.
let _fetchPromise: Promise<string[]> | null = null;
let _cached: string[] | null = null;
const _subscribers = new Set<() => void>();

function getOrFetch(): Promise<string[]> {
  if (!_fetchPromise) {
    _fetchPromise = getCrashKeywords()
      .then((kws) => {
        _cached = kws;
        _subscribers.forEach((cb) => cb());
        return kws;
      })
      .catch(() => {
        _fetchPromise = null; // allow retry
        return DEFAULT_CRASH_KEYWORDS;
      });
  }
  return _fetchPromise;
}

export type UseCrashKeywordsResult = {
  keywords: string[];
  pattern: RegExp;
  saving: boolean;
  saveKeywords: (next: string[]) => Promise<void>;
};

export function useCrashKeywords(): UseCrashKeywordsResult {
  const [keywords, setKeywords] = useState<string[]>(_cached ?? DEFAULT_CRASH_KEYWORDS);
  const [saving, setSaving] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    const notify = () => {
      if (mountedRef.current && _cached) setKeywords([..._cached]);
    };
    _subscribers.add(notify);
    // If cache already populated (another instance fetched first), sync now.
    if (_cached) {
      setKeywords([..._cached]);
    } else {
      getOrFetch().then((kws) => {
        if (mountedRef.current) setKeywords([...kws]);
      });
    }
    return () => {
      mountedRef.current = false;
      _subscribers.delete(notify);
    };
  }, []);

  const saveKeywords = useCallback(async (next: string[]) => {
    setSaving(true);
    try {
      const saved = await putCrashKeywords(next);
      _cached = saved;
      _fetchPromise = Promise.resolve(saved);
      setKeywords([...saved]);
      _subscribers.forEach((cb) => cb());
    } finally {
      setSaving(false);
    }
  }, []);

  // Memoized so consumers whose effects depend on `pattern` (useFleetMonitor's
  // websocket) don't tear down and reconnect on every render.
  const pattern = useMemo(() => buildCrashPattern(keywords), [keywords]);
  return { keywords, pattern, saving, saveKeywords };
}
