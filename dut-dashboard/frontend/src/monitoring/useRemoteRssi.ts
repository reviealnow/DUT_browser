import { useCallback, useState } from "react";

import { captureRemoteRssi, RemoteRssiResult } from "../api/rest";
import { FleetEntry } from "./useFleetMonitor";

/**
 * The backhaul capture, held once for every card that shows it.
 *
 * The strip and the Fleet section render the same cards, so the last capture
 * has to live above both of them — kept inside a card it would be re-fetched,
 * or silently lost, every time the other view mounted. Nothing here polls: a
 * capture occupies that DUT's serial console (see the RPC discipline in
 * `dut-dashboard/CLAUDE.md`), so it runs only when something asks.
 */
export type RemoteRssiState = {
  /** The newest capture for this DUT, or what the registry already knew. */
  get: (entry: FleetEntry) => RemoteRssiResult | null;
  capturing: (dutId: string) => boolean;
  /** Rejects on failure — the caller owns how that is shown. */
  refresh: (dutId: string) => Promise<void>;
  /**
   * Every mesh node in turn, **nodes before roots**. Not a formality: a root
   * cannot name its own backhaul VAP from its own console, and is identified
   * from the uplink a child reports. Capturing a root first falls back to
   * whatever interface was configured, which is a silent empty list when that
   * guess is wrong.
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

export function useRemoteRssi(): RemoteRssiState {
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
      } finally {
        mark(dutId, false);
      }
    },
    [mark],
  );

  const refreshAll = useCallback(
    async (entries: FleetEntry[]) => {
      const mesh = entries.filter((entry) => entry.remote?.isMesh && entry.serialOpen);
      const ordered = [
        ...mesh.filter((entry) => entry.remote!.role !== "root"),
        ...mesh.filter((entry) => entry.remote!.role === "root"),
      ];
      for (const entry of ordered) {
        // Sequential on purpose. Each capture is one console's synchronous RPC,
        // and the ordering above only means anything if the node's result has
        // landed before the root is asked.
        await refresh(entry.id);
      }
    },
    [refresh],
  );

  const get = useCallback(
    (entry: FleetEntry) => results.get(entry.id) ?? seed(entry),
    [results],
  );

  const capturing = useCallback((dutId: string) => inflight.has(dutId), [inflight]);

  return { get, capturing, refresh, refreshAll };
}
