import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";

import {
  cancelPacketAnalysis,
  createPacketAnalysis,
  getPacketAnalysis,
  getPacketFindings,
  getPacketGroups,
  getPacketHosts,
  getPacketModes,
  getPacketRows,
  humanizeApiError,
  PacketAnalysis,
  PacketAnalysisRequest,
  PacketFinding,
  PacketGroup,
  PacketHost,
  PacketMode,
  PacketRowsPage,
  probePacketHost,
} from "../api/rest";

/**
 * Packet Monitor's shared state (docs/design-packet-monitor.md §B11).
 *
 * Mounted above the sections so an analysis keeps its place while the user
 * looks elsewhere. Everything here is REST: the Hosts and Test Modes are
 * loaded on entry and on demand, never on a timer. The one poll is
 * `GET /analyses/{id}`, once a second and only while that analysis is queued
 * or running -- a backend poll, not a serial capture, so the serial RPC rule
 * does not apply (§B9).
 *
 * Row pages are requested once per analysis and offset while in flight, and
 * the last PAGE_CACHE_MAX answers are kept, so a table scrolling back and forth
 * over a few pages does not ask twice -- but an analysis of hundreds of
 * thousands of rows scrolled end to end does not stay in memory either.
 *
 * The form that produced the results lives here too, so leaving the section
 * and coming back shows the inputs behind the table, not a blank form.
 */
export const POLL_MS = 1000;
export const PAGE_CACHE_MAX = 16;

/** What the operator chose in the section; kept across navigation. */
export type PacketForm = {
  fileId: number | null;
  hostId: string;
  modeId: string;
  filter: string;
  extra: string;
  /** The frame whose detail panel is open, or null. */
  frame: number | null;
  /** "One row per BSS". */
  byBss: boolean;
};

export const EMPTY_FORM: PacketForm = {
  fileId: null, hostId: "", modeId: "", filter: "", extra: "", frame: null, byBss: false,
};

type PageCache = {
  inflight: Map<string, Promise<PacketRowsPage>>;
  /** Insertion order is recency: the first key is the least recently used. */
  done: Map<string, PacketRowsPage>;
};

const emptyCache = (): PageCache => ({ inflight: new Map(), done: new Map() });

export type PacketMonitorState = {
  hosts: PacketHost[];
  modes: PacketMode[];
  loading: boolean;
  error: string;
  /** Load the Hosts and Test Modes; coalesced, so mounting twice asks once. */
  load: () => Promise<void>;
  probe: (collectorId: string) => Promise<void>;
  probing: string | null;

  analysis: PacketAnalysis | null;
  analysisError: string;
  findings: PacketFinding[];
  groups: PacketGroup[];
  start: (request: PacketAnalysisRequest) => Promise<void>;
  cancel: () => Promise<void>;
  starting: boolean;
  /** One page of decoded rows of the current analysis. */
  page: (offset: number, limit: number) => Promise<PacketRowsPage>;

  form: PacketForm;
  /** Merge `patch` into the form. */
  setForm: (patch: Partial<PacketForm>) => void;
};

const PacketAnalysisContext = createContext<PacketMonitorState | null>(null);

const isActive = (analysis: PacketAnalysis | null) =>
  analysis !== null && (analysis.status === "queued" || analysis.status === "running");

export function PacketAnalysisProvider({ children }: { children: ReactNode }) {
  const [hosts, setHosts] = useState<PacketHost[]>([]);
  const [modes, setModes] = useState<PacketMode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [probing, setProbing] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<PacketAnalysis | null>(null);
  const [analysisError, setAnalysisError] = useState("");
  const [findings, setFindings] = useState<PacketFinding[]>([]);
  const [groups, setGroups] = useState<PacketGroup[]>([]);
  const [starting, setStarting] = useState(false);
  const [form, setFormState] = useState<PacketForm>(EMPTY_FORM);
  const setForm = useCallback((patch: Partial<PacketForm>) => setFormState((prev) => ({ ...prev, ...patch })), []);

  const loadInflight = useRef<Promise<void> | null>(null);
  const pages = useRef<PageCache>(emptyCache());
  // The analysis on screen, and the one whose findings were already fetched:
  // a slow answer for an earlier analysis must not land on a later one, and
  // one analysis's results are fetched once however many paths see it end.
  const currentId = useRef<number | null>(null);
  const finishedId = useRef<number | null>(null);

  const load = useCallback(() => {
    if (loadInflight.current) {
      return loadInflight.current;
    }
    const run = (async () => {
      setLoading(true);
      setError("");
      try {
        const [nextHosts, nextModes] = await Promise.all([getPacketHosts(), getPacketModes()]);
        setHosts(nextHosts);
        setModes(nextModes);
      } catch (e) {
        setError(humanizeApiError(e));
      } finally {
        setLoading(false);
        loadInflight.current = null;
      }
    })();
    loadInflight.current = run;
    return run;
  }, []);

  const probe = useCallback(async (collectorId: string) => {
    setProbing(collectorId);
    setError("");
    try {
      await probePacketHost(collectorId);
      setHosts(await getPacketHosts());
    } catch (e) {
      setError(humanizeApiError(e));
    } finally {
      setProbing(null);
    }
  }, []);

  const finished = useCallback(async (done: PacketAnalysis) => {
    if (done.id !== currentId.current || finishedId.current === done.id) {
      return;
    }
    finishedId.current = done.id;
    if (done.status !== "done") {
      setFindings([]);
      setGroups([]);
      return;
    }
    try {
      const [nextFindings, nextGroups] = await Promise.all([
        getPacketFindings(done.id),
        getPacketGroups(done.id),
      ]);
      if (done.id !== currentId.current) {
        return; // the user started another analysis meanwhile
      }
      setFindings(nextFindings);
      setGroups(nextGroups);
    } catch (e) {
      if (done.id !== currentId.current) {
        return;
      }
      finishedId.current = null; // let a later poll or Cancel try again
      setAnalysisError(humanizeApiError(e));
    }
  }, []);

  const start = useCallback(
    async (request: PacketAnalysisRequest) => {
      setStarting(true);
      setAnalysisError("");
      try {
        const id = await createPacketAnalysis(request);
        currentId.current = id;
        // A fresh object, not a clear: a page still in flight for the previous
        // analysis then lands in a cache nobody reads.
        pages.current = emptyCache();
        setFindings([]);
        setGroups([]);
        const created = await getPacketAnalysis(id);
        if (currentId.current !== id) {
          return;
        }
        setAnalysis(created);
        if (!isActive(created)) {
          // Finished (or refused) before the first poll could see it run.
          await finished(created);
        }
      } catch (e) {
        setAnalysisError(humanizeApiError(e));
      } finally {
        setStarting(false);
      }
    },
    [finished],
  );

  const cancel = useCallback(async () => {
    if (!analysis) {
      return;
    }
    const id = analysis.id;
    let cancelError = "";
    try {
      await cancelPacketAnalysis(id);
    } catch (e) {
      // Often a 409 because the analysis ended a moment ago; the state below
      // says so, and the error is only worth showing if it is still running.
      cancelError = humanizeApiError(e);
    }
    try {
      const next = await getPacketAnalysis(id);
      if (currentId.current !== id) {
        return;
      }
      setAnalysis(next);
      // Ending here stops the poll, so this path must fetch the results too.
      if (!isActive(next)) {
        setAnalysisError("");
        void finished(next);
      } else if (cancelError) {
        setAnalysisError(cancelError);
      }
    } catch (e) {
      if (currentId.current === id) {
        setAnalysisError(cancelError || humanizeApiError(e));
      }
    }
  }, [analysis, finished]);

  // The one poll: while the current analysis is queued or running. One request
  // at a time, so a slow backend does not stack them up.
  const activeId = isActive(analysis) ? analysis!.id : null;
  useEffect(() => {
    if (activeId === null) {
      return;
    }
    let stopped = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (inFlight || stopped) {
        return;
      }
      inFlight = true;
      try {
        const next = await getPacketAnalysis(activeId);
        if (stopped || currentId.current !== activeId) {
          return;
        }
        setAnalysis(next);
        setAnalysisError(""); // a poll that worked supersedes one that did not
        if (!isActive(next)) {
          stopped = true;
          window.clearInterval(timer);
          void finished(next);
        }
      } catch (e) {
        if (!stopped) {
          setAnalysisError(humanizeApiError(e));
        }
      } finally {
        inFlight = false;
      }
    }, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [activeId, finished]);

  const analysisId = analysis?.id ?? null;
  const page = useCallback(
    (offset: number, limit: number) => {
      if (analysisId === null) {
        return Promise.reject(new Error("No analysis"));
      }
      const key = `${analysisId}:${offset}:${limit}`;
      const cache = pages.current;
      const hit = cache.done.get(key);
      if (hit) {
        cache.done.delete(key); // re-insert: now the most recently used
        cache.done.set(key, hit);
        return Promise.resolve(hit);
      }
      let pending = cache.inflight.get(key);
      if (!pending) {
        pending = getPacketRows(analysisId, offset, limit);
        cache.inflight.set(key, pending);
        pending.then(
          (result) => {
            cache.inflight.delete(key);
            cache.done.set(key, result);
            while (cache.done.size > PAGE_CACHE_MAX) {
              cache.done.delete(cache.done.keys().next().value as string);
            }
          },
          () => cache.inflight.delete(key),
        );
      }
      return pending;
    },
    [analysisId],
  );

  return (
    <PacketAnalysisContext.Provider
      value={{
        hosts, modes, loading, error, load, probe, probing,
        analysis, analysisError, findings, groups, start, cancel, starting, page,
        form, setForm,
      }}
    >
      {children}
    </PacketAnalysisContext.Provider>
  );
}

export function usePacketAnalysis(): PacketMonitorState {
  const ctx = useContext(PacketAnalysisContext);
  if (ctx === null) {
    throw new Error("usePacketAnalysis must be used within a PacketAnalysisProvider");
  }
  return ctx;
}
