import { useEffect, useRef, useState } from "react";

import { getDuts, getSnapshots } from "../api/rest";
import { applySnapshotDelta, connectFleetWebSocket, SnapshotPayload } from "../api/websocket";
import { useCrashKeywords } from "./useCrashKeywords";
import { cpuFromSnapshot, DutStatus } from "./useDutMonitor";

// Mirror the single-DUT monitor's activity windows so a DUT reads "streaming"
// for the same 10s after its last event, ticked at the same 2s cadence.
const STREAM_ACTIVE_WINDOW_MS = 10_000;
const STATUS_TICK_MS = 2_000;

/** One row of the fleet grid: a single DUT's at-a-glance state. */
export type FleetEntry = {
  id: string;
  label: string;
  status: DutStatus;
  /** 100 − mean idle across cores from the latest (reconstructed) snapshot. */
  cpuBusyPct: number | null;
  coreCount: number;
  /** Critical-crash matches seen on this DUT's stream since the Fleet opened. */
  crashCount: number;
  /** device_ts of the latest snapshot, or null before any. */
  lastSnapshotTs: string | null;
  /** Whole seconds since this DUT's last stream event; null before any event. */
  lastEventAgeSec: number | null;
};

/**
 * Phase 37: fleet aggregate. One un-filtered `/ws` connection (the backend
 * broadcasts every DUT's events tagged with `dut_id`) is demuxed per DUT so the
 * whole fleet updates from a single socket. Deliberately lightweight — it keeps
 * only the per-DUT delta base, an activity timestamp, and a crash count (no line
 * storage, no CPU/memory history). Drilling into a DUT's Overview still uses the
 * full single-DUT `useDutMonitor`.
 *
 * Mounted only while the Fleet section is visible, so the second socket exists
 * only when it is being looked at.
 */
export function useFleetMonitor(): FleetEntry[] {
  const { pattern: crashPattern } = useCrashKeywords();
  // Registry order + labels for every registered DUT (cards show even with no stream).
  const [duts, setDuts] = useState<{ id: string; label: string }[]>([]);
  const [connected, setConnected] = useState(false);
  const [nowTick, setNowTick] = useState(() => Date.now());

  // Per-DUT live state kept in refs (mutated by the socket, read on each tick) so
  // a burst of events doesn't trigger a render per event — the 2s tick re-renders.
  const baseRef = useRef<Map<string, SnapshotPayload>>(new Map());
  const lastActivityRef = useRef<Map<string, number>>(new Map());
  const crashRef = useRef<Map<string, number>>(new Map());

  // Load the registry once, then backfill each DUT's latest snapshot for an
  // instant first paint (CPU + last-ts). Backfill does NOT count as activity, so
  // a stale DUT stays "idle" until a live event — matching useDutMonitor.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      let list: { id: string; label: string }[] = [];
      try {
        list = (await getDuts()).map((d) => ({ id: d.id, label: d.label }));
      } catch {
        return; // backend unreachable: nothing to show
      }
      if (cancelled) {
        return;
      }
      setDuts(list);
      await Promise.all(
        list.map(async ({ id }) => {
          try {
            const snaps = await getSnapshots(1, id);
            if (!cancelled && snaps.length > 0) {
              baseRef.current.set(id, snaps[snaps.length - 1]);
            }
          } catch {
            // No history for this DUT — its card just shows "—" until live.
          }
        }),
      );
      if (!cancelled) {
        setNowTick(Date.now()); // reflect backfilled CPU immediately
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // One demuxed socket for the whole fleet.
  useEffect(() => {
    const recordActivity = (dutId: string) => {
      lastActivityRef.current.set(dutId, Date.now());
    };

    const socket = connectFleetWebSocket({
      onEvent: (event) => {
        const dutId = event.dut_id;
        if (!dutId) {
          return; // every real DUT event is tagged; ignore anything untagged
        }
        if (event.type === "snapshot_update") {
          baseRef.current.set(dutId, event.snapshot);
          recordActivity(dutId);
          return;
        }
        if (event.type === "snapshot_delta") {
          const base = baseRef.current.get(dutId);
          if (base) {
            baseRef.current.set(dutId, applySnapshotDelta(base, event.delta));
            recordActivity(dutId);
          }
          return;
        }
        if (event.type === "console_line" && typeof event.text === "string") {
          if (crashPattern.test(event.text)) {
            crashRef.current.set(dutId, (crashRef.current.get(dutId) ?? 0) + 1);
          }
          recordActivity(dutId);
          return;
        }
        if (event.type === "console_line_batch" && Array.isArray(event.lines)) {
          let matched = 0;
          for (const line of event.lines) {
            if (typeof line === "string" && crashPattern.test(line)) {
              matched += 1;
            }
          }
          if (matched > 0) {
            crashRef.current.set(dutId, (crashRef.current.get(dutId) ?? 0) + matched);
          }
          recordActivity(dutId);
        }
      },
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
    });

    const interval = window.setInterval(() => setNowTick(Date.now()), STATUS_TICK_MS);

    return () => {
      window.clearInterval(interval);
      socket.close();
    };
  }, [crashPattern]);

  // Derive the view rows on each tick from the registry order + live refs.
  return duts.map(({ id, label }) => {
    const base = baseRef.current.get(id) ?? null;
    const cpu = cpuFromSnapshot(base);
    const lastActivity = lastActivityRef.current.get(id) ?? 0;
    const status: DutStatus = !connected
      ? "offline"
      : nowTick - lastActivity < STREAM_ACTIVE_WINDOW_MS
      ? "streaming"
      : "idle";
    const lastEventAgeSec =
      lastActivity === 0 ? null : Math.max(0, Math.floor((nowTick - lastActivity) / 1000));
    return {
      id,
      label,
      status,
      cpuBusyPct: cpu.cpuBusyPct,
      coreCount: cpu.coreCount,
      crashCount: crashRef.current.get(id) ?? 0,
      lastSnapshotTs: base?.device_ts ?? null,
      lastEventAgeSec,
    };
  });
}
