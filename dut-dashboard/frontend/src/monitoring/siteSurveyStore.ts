import { useSyncExternalStore } from "react";

import {
  ChannelRecommendationResult,
  getChannelRecommendation,
  humanizeApiError,
} from "../api/rest";

/**
 * Shared, app-wide cache of the per-DUT site survey.
 *
 * The off-channel survey (`iw scan` on every active VAP) takes ~40s and holds
 * the serial capture gate, so it must run rarely and its result must outlive the
 * SiteSurveyCard — which only mounts while its section is open. Keeping the
 * cache at module scope (not in the card) lets two things share one scan:
 *
 *   1. A background prescan fired the moment serial connects (see runSurvey in
 *      the Dashboard open flow), so the survey is ready no matter which page the
 *      user is on when they eventually open Site Survey.
 *   2. The card itself, which subscribes via useSurvey() and shows the cached
 *      result instantly instead of re-running the long scan on every visit.
 *
 * A failed scan keeps any previous result (only the error is updated) so a
 * re-scan never blanks the card.
 */
export type SurveyProgress = {
  stage: "capabilities" | "scanning" | "done";
  iface: string | null;
  index: number;
  total: number;
};

export type SurveyEntry = {
  result: ChannelRecommendationResult | null;
  loading: boolean;
  error: string;
  /** Live scan progress from survey_progress /ws events; null when idle. */
  progress: SurveyProgress | null;
};

const EMPTY: SurveyEntry = { result: null, loading: false, error: "", progress: null };

// One entry per DUT id. Entries are replaced (never mutated in place) so a
// stable reference between changes keeps useSyncExternalStore from looping.
const store = new Map<string, SurveyEntry>();
// Coalesce concurrent scans for the same DUT (e.g. the connect-time prescan and
// the card's auto-on-open) into a single in-flight request.
const inflight = new Map<string, Promise<void>>();
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function setEntry(dutId: string, patch: Partial<SurveyEntry>) {
  const prev = store.get(dutId) ?? EMPTY;
  store.set(dutId, { ...prev, ...patch });
  emit();
}

export function getSurveyEntry(dutId: string): SurveyEntry {
  return store.get(dutId) ?? EMPTY;
}

/**
 * Run the survey for a DUT now (a manual Re-scan or the connect-time prescan).
 * Concurrent calls for the same DUT share one request. On failure the previous
 * result is preserved and only the error is set.
 */
export function runSurvey(dutId: string): Promise<void> {
  const existing = inflight.get(dutId);
  if (existing) {
    return existing;
  }
  const run = (async () => {
    setEntry(dutId, { loading: true, error: "", progress: null });
    try {
      const result = await getChannelRecommendation(dutId);
      setEntry(dutId, { result, loading: false, error: "", progress: null });
    } catch (e) {
      setEntry(dutId, { loading: false, error: humanizeApiError(e), progress: null });
    } finally {
      inflight.delete(dutId);
    }
  })();
  inflight.set(dutId, run);
  return run;
}

/**
 * Ingest a survey_progress /ws event (forwarded by useDutMonitor — the shared
 * dashboard socket, NOT a new connection). Progress is only rendered while an
 * entry is loading, and runSurvey resets it on start and settle, so a stray
 * late event can't leave a stale bar behind.
 */
export function setSurveyProgress(dutId: string, progress: SurveyProgress): void {
  setEntry(dutId, { progress });
}

/**
 * Scan a DUT only if it has never produced a result and isn't already scanning
 * — used by the card's auto-on-open so opening the section fills the cache once
 * but never re-runs a scan that already succeeded (a fresh connect uses
 * runSurvey directly to force a refresh).
 */
export function ensureSurvey(dutId: string): void {
  const entry = store.get(dutId);
  if ((entry && (entry.result || entry.loading)) || inflight.has(dutId)) {
    return;
  }
  void runSurvey(dutId);
}

/** Subscribe to the per-DUT survey entry; re-renders when its scan state moves. */
export function useSurvey(dutId: string): SurveyEntry {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => getSurveyEntry(dutId),
  );
}
