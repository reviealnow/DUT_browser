import { useSyncExternalStore } from "react";

import {
  captureDutContext,
  ChannelRecommendationResult,
  getChannelRecommendation,
  humanizeApiError,
  identifyDut,
  probeMesh,
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
 * Everything captured once, the moment a DUT is connected: the Wi-Fi clients
 * and SSID capability context snapshots, then the site survey prescan.
 *
 * The two share one serial capture gate, so they run **in sequence** — firing
 * them together would just make them queue behind each other anyway, and a
 * queued capture can time out. Connect is measurably slower for it; the trade
 * is deliberate, because this is the fixed "what the site looked like on
 * arrival" reference a log downloaded days later is bundled with.
 *
 * **The order matters and is not arbitrary.** capture_command returns when its
 * sentinel arrives *or its timeout expires*, but the DUT keeps transmitting
 * either way. A survey on an AP with ~29 VAPs leaves tens of thousands of
 * `iw scan` lines still draining at 115200 baud, so a capture started right
 * after it has its whole window filled by that backlog and reads nothing of its
 * own — measured on an AP6_840E, clients+capability returned 0 VAPs in 17.7s
 * straight after a survey, and 29 VAPs in 5.3s on an idle line. Connect is the
 * one moment the line is guaranteed quiet, so the short captures take it and
 * the survey, whose trailing output harms only whatever follows it, goes last.
 *
 * Fire-and-forget: every step swallows its own errors, so nothing here can
 * fail a connect. Wi-Fi Clients and SSID Capability keep their own
 * on-section-entry fetches — this is additional, not a replacement.
 */
export async function runConnectCaptures(dutId: string): Promise<void> {
  try {
    // First, and first for a reason: everything after it is a measurement, and
    // this is the step that says which device the measurements are of. A
    // capture filed before the identity is known is filed against whatever the
    // registry last believed — which, on a bench where one entry outlives the
    // hardware cabled to it, is how an 840E's readings came to be shown under a
    // 420E's name. It is also the shortest command here, so the quiet line
    // costs it least.
    await identifyDut(dutId);
  } catch {
    // A closed or busy console, or a `hostname` that lost the line to sysMon.
    // The stored identity is left exactly as it was: silence is not evidence
    // the hardware changed, and the next connect asks again.
  }
  try {
    await captureDutContext(dutId);
  } catch {
    // The endpoint already reports per-kind failures without raising; this only
    // catches transport-level errors, which must not surface on the connect.
  }
  try {
    // Before the survey, and that placement is the whole reason this is not
    // simply appended: the survey leaves tens of thousands of `iw scan` lines
    // draining at 115200 baud, and a capture started after it has its window
    // filled by that backlog and reads nothing of its own. This probe is one
    // short command, so it belongs in the quiet stretch with the other short
    // ones — a probe starved by the survey would come back "could not tell" on
    // a perfectly healthy mesh, which is worse than not asking.
    await probeMesh(dutId);
  } catch {
    // A closed or busy console. Nothing to show and nothing to fail: the next
    // connect asks again, and the card keeps saying "not probed".
  }
  await runSurvey(dutId);
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
