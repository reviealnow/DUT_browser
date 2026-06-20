import { createContext, ReactNode, useCallback, useContext, useRef, useState } from "react";

import { getWifiClients, humanizeApiError, WifiClientsResult } from "../api/rest";

/**
 * Shared cache of the last on-demand Wi-Fi scan (`wlanconfig` per VAP).
 *
 * `getWifiClients()` is a SYNCHRONOUS serial RPC that occupies the read loop and
 * briefly pauses sysmon — so it must never be background-polled. This context
 * lets the Wi-Fi section and the Overview reuse a single most-recent scan
 * instead of each firing their own RPC: whoever scans first populates the
 * cache, and the other view reflects it. `scan(dutId)` is the only trigger
 * (auto-on-entry once, or a manual button); the result is tagged with the DUT
 * it was captured for so a stale scan never leaks across the DUT switcher.
 */
export type WifiScanState = {
  /** Which DUT the current result/error pertains to (null = never scanned). */
  dutId: string | null;
  result: WifiClientsResult | null;
  loading: boolean;
  error: string;
  scan: (dutId: string) => Promise<void>;
};

const WifiScanContext = createContext<WifiScanState | null>(null);

export function WifiScanProvider({ children }: { children: ReactNode }) {
  const [dutId, setDutId] = useState<string | null>(null);
  const [result, setResult] = useState<WifiClientsResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  // Coalesce concurrent scans for the same DUT into one RPC: an auto-on-entry
  // effect (double-invoked under StrictMode) plus a manual click would otherwise
  // fire overlapping serial captures. The single in-flight promise is shared.
  const inflight = useRef<Map<string, Promise<void>>>(new Map());

  const scan = useCallback((target: string) => {
    const existing = inflight.current.get(target);
    if (existing) {
      return existing;
    }
    const run = (async () => {
      setLoading(true);
      setError("");
      // Drop a result captured for a different DUT so the UI never shows a stale
      // count against the wrong DUT while the new scan is in flight.
      setDutId((prev) => {
        if (prev !== target) {
          setResult(null);
        }
        return target;
      });
      try {
        const data = await getWifiClients(target);
        setResult(data);
      } catch (e) {
        setResult(null);
        setError(humanizeApiError(e));
      } finally {
        setLoading(false);
        inflight.current.delete(target);
      }
    })();
    inflight.current.set(target, run);
    return run;
  }, []);

  return (
    <WifiScanContext.Provider value={{ dutId, result, loading, error, scan }}>
      {children}
    </WifiScanContext.Provider>
  );
}

export function useWifiScan(): WifiScanState {
  const ctx = useContext(WifiScanContext);
  if (ctx === null) {
    throw new Error("useWifiScan must be used within a WifiScanProvider");
  }
  return ctx;
}

/**
 * View of the cache scoped to one DUT: returns the result/error/loading only
 * when the cached scan belongs to `dutId`, so a scan for another DUT reads as
 * "no scan yet" rather than leaking the wrong DUT's clients.
 */
export function wifiScanForDut(state: WifiScanState, dutId: string) {
  const isForDut = state.dutId === dutId;
  return {
    result: isForDut ? state.result : null,
    error: isForDut ? state.error : "",
    loading: state.loading && (state.dutId === dutId || state.dutId === null),
  };
}
