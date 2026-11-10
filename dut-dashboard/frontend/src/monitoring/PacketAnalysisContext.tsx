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
 * Row pages are cached per analysis and offset, with one request in flight per
 * page, so a table scrolling back and forth never asks twice.
 */
export const POLL_MS = 1000;

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

  const loadInflight = useRef<Promise<void> | null>(null);
  const pages = useRef<Map<string, Promise<PacketRowsPage>>>(new Map());

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
      setFindings(nextFindings);
      setGroups(nextGroups);
    } catch (e) {
      setAnalysisError(humanizeApiError(e));
    }
  }, []);

  const start = useCallback(
    async (request: PacketAnalysisRequest) => {
      setStarting(true);
      setAnalysisError("");
      try {
        const id = await createPacketAnalysis(request);
        pages.current.clear();
        setFindings([]);
        setGroups([]);
        const created = await getPacketAnalysis(id);
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
    try {
      await cancelPacketAnalysis(analysis.id);
      setAnalysis(await getPacketAnalysis(analysis.id));
    } catch (e) {
      setAnalysisError(humanizeApiError(e));
    }
  }, [analysis]);

  // The one poll: while the current analysis is queued or running.
  const activeId = isActive(analysis) ? analysis!.id : null;
  useEffect(() => {
    if (activeId === null) {
      return;
    }
    let stopped = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await getPacketAnalysis(activeId);
        if (stopped) {
          return;
        }
        setAnalysis(next);
        if (!isActive(next)) {
          void finished(next);
        }
      } catch (e) {
        if (!stopped) {
          setAnalysisError(humanizeApiError(e));
        }
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
      let pending = pages.current.get(key);
      if (!pending) {
        pending = getPacketRows(analysisId, offset, limit);
        pending.catch(() => pages.current.delete(key));
        pages.current.set(key, pending);
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
