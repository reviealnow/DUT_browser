import { createContext, ReactNode, useCallback, useContext, useState } from "react";

import { captureRemoteRssi, humanizeApiError, RemoteRssiResult } from "../api/rest";
import { FleetEntry } from "./useFleetMonitor";

/**
 * The backhaul capture, held once for every view that shows it.
 *
 * A provider rather than a hook each view calls: the Overview strip and the
 * Fleet section render the same cards, and a hook instance per view is a state
 * per view. They are never mounted together, so the visible cost is a race —
 * capture on the strip, switch to Fleet before it lands, and the result arrives
 * at a hook nobody is rendering while the section seeds itself from whatever
 * `/api/duts` answered a moment earlier. (The backend does persist a capture to
 * the registry, so the seed is usually current; "usually" is the bug.)
 *
 * Nothing here polls. A capture occupies that DUT's serial console — see the
 * RPC discipline in `dut-dashboard/CLAUDE.md` — so it runs only when asked.
 */
export type RemoteRssiState = {
  /** The newest capture for this DUT, or what the registry already knew. */
  get: (entry: FleetEntry) => RemoteRssiResult | null;
  capturing: (dutId: string) => boolean;
  /** Rejects on failure — the caller owns how that is shown. */
  refresh: (dutId: string) => Promise<RemoteRssiResult>;
  /**
   * Every mesh node with a console open, **children before roots**.
   *
   * Not a formality: a root cannot name its own backhaul VAP from its own
   * console, and is identified from the uplink a child reports. A root captured
   * first falls back to whatever interface was configured, which is a silent
   * empty child list when that guess is wrong.
   *
   * A DUT that has never been captured has `role: null`, which is not "not a
   * root" — it is "nobody knows yet". Ordering on the registry's role alone put
   * an unclassified root in the first pass, so on a cold fleet the order was
   * whatever order the DUTs happened to be registered in. Hence two passes: the
   * second re-captures anything now known to be a root, including one that only
   * turned out to be one during the first pass.
   *
   * One DUT failing does not abandon the rest: each is caught, and the failures
   * are reported together at the end.
   */
  refreshAll: (entries: FleetEntry[]) => Promise<void>;
};

/** What the registry persisted from the last capture, before this one. */
function seed(entry: FleetEntry): RemoteRssiResult | null {
  if (!entry.remote) {
    return null;
  }
  return {
    dut: entry.id,
    applicable: entry.remote.isMesh,
    role: entry.remote.role,
    uplink: entry.remote.uplink,
    downlink: entry.remote.downlink,
  };
}

const RemoteRssiContext = createContext<RemoteRssiState | null>(null);

export function RemoteRssiProvider({ children }: { children: ReactNode }) {
  const [results, setResults] = useState<Map<string, RemoteRssiResult>>(new Map());
  const [inflight, setInflight] = useState<Set<string>>(new Set());

  const mark = useCallback((dutId: string, busy: boolean) => {
    setInflight((current) => {
      const next = new Set(current);
      if (busy) {
        next.add(dutId);
      } else {
        next.delete(dutId);
      }
      return next;
    });
  }, []);

  const refresh = useCallback(
    async (dutId: string) => {
      mark(dutId, true);
      try {
        const result = await captureRemoteRssi(dutId);
        // `captureRemoteRssi` coalesces per DUT and answers with the id it
        // captured; keying on that rather than on the id we asked for is what
        // stops a result landing on the wrong card when the fleet changes
        // under an in-flight request.
        setResults((current) => new Map(current).set(result.dut, result));
        return result;
      } finally {
        mark(dutId, false);
      }
    },
    [mark],
  );

  const refreshAll = useCallback(
    async (entries: FleetEntry[]) => {
      const mesh = entries.filter((entry) => entry.remote?.isMesh && entry.serialOpen);
      const failures: string[] = [];
      const roles = new Map<string, "root" | "node" | null>(
        mesh.map((entry) => [entry.id, entry.remote!.role]),
      );

      const capture = async (dutId: string) => {
        try {
          const result = await refresh(dutId);
          roles.set(dutId, result.role);
        } catch (err) {
          // Sequential, but not fragile: a closed console or a DUT removed
          // mid-sweep costs its own reading and nothing else. Reporting them
          // together beats stopping at the first and leaving the rest stale
          // with no indication that they were never tried.
          // Humanised here, not by whoever displays the aggregate: once these
          // are joined into one message the result is no longer a JSON body,
          // so `humanizeApiError` at the call site cannot unwrap it and the raw
          // `{"detail": ...}` reaches the screen — the defect #129 fixed for a
          // single failure, back again in the sweep.
          failures.push(`${dutId}: ${humanizeApiError(err)}`);
        }
      };

      // Pass 1: everything not already known to be a root — the children, and
      // anything never captured. Sequential because each is one console's
      // synchronous RPC, and because pass 2 depends on what this pass learned.
      for (const entry of mesh.filter((e) => roles.get(e.id) !== "root")) {
        await capture(entry.id);
      }
      // Pass 2: everything known to be a root *now*, which includes a DUT that
      // pass 1 discovered was one. Its first reading was taken before any child
      // had reported an uplink, so it is exactly the reading that cannot be
      // trusted — capturing it again is the whole point of the second pass.
      for (const entry of mesh.filter((e) => roles.get(e.id) === "root")) {
        await capture(entry.id);
      }

      if (failures.length > 0) {
        throw new Error(failures.join("; "));
      }
    },
    [refresh],
  );

  const get = useCallback(
    (entry: FleetEntry) => results.get(entry.id) ?? seed(entry),
    [results],
  );

  const capturing = useCallback((dutId: string) => inflight.has(dutId), [inflight]);

  return (
    <RemoteRssiContext.Provider value={{ get, capturing, refresh, refreshAll }}>
      {children}
    </RemoteRssiContext.Provider>
  );
}

export function useRemoteRssi(): RemoteRssiState {
  const ctx = useContext(RemoteRssiContext);
  if (ctx === null) {
    throw new Error("useRemoteRssi must be used within a RemoteRssiProvider");
  }
  return ctx;
}
