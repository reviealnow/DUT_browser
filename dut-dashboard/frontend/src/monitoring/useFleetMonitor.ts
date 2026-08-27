import { useCallback, useEffect, useRef, useState } from "react";

import { DutInfo, getDuts, getSnapshots } from "../api/rest";
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
  /** Registry truth: whether a serial/replay session is open on this DUT.
   * Distinct from `status` — a quiet DUT can be open yet read "idle". */
  serialOpen: boolean;
  /** Last successful serial-open params, or null. Enables one-click Connect. */
  lastSerial: { port: string; baudrate: number } | null;
  /** Null for a DUT cabled to this machine. Where its console lives, not
   *  whether it is in a mesh: those are independent, and only the second is
   *  measured (`backhaul`). `is_mesh` is deliberately absent — whether a
   *  capture applies is `backhaul.applicable`, computed once by the registry,
   *  and a second copy of that question here is a second answer to it. */
  remote: {
    host: string;
    port: number;
    device: string;
  } | null;
  /** This DUT's own management address, or `""` when none is set. Distinct from
   *  `remote.host` in the row above — that is the Pi holding a console, this is
   *  the device — and the only thing that says whether it can be asked for the
   *  mesh table at all. */
  mgmtUrl: string;
  /** Which AP6 this is, read from its console prompt — null for a DUT nobody
   *  has ever opened a console on. Named on the card because the interface
   *  numbering, and so which band an athN belongs to, depends on it. */
  model: string | null;
  /** What this DUT's model says its core count is, or null when the model is
   *  unknown. Compared against `coreCount`, which comes from the last snapshot
   *  whatever hardware that was recorded on. */
  modelCores: number | null;
  /** What the DUT itself last said about its mesh, or null before any probe.
   *  A sibling of `backhaul`, not part of it: that is what a console measured
   *  off this DUT's radios, this is what the device reported when asked. */
  meshProbe: DutInfo["mesh_probe"];
  /** The last backhaul capture — for every DUT, cabled ones included. Up to the
   *  parent and down to the children are separate measurements from separate
   *  commands; the card must not blur them into one number. */
  backhaul: {
    applicable: boolean;
    captured: boolean;
    /** The registry's own name for the console behind this DUT — see rest.ts. */
    consoleId: string;
    role: "root" | "node" | null;
    uplink: DutInfo["backhaul"]["uplink"];
    downlink: DutInfo["backhaul"]["downlink"];
  };
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
 * Phase 69: mounted by the Fleet strip at the top of Overview (its own nav
 * section was removed). Its subscription shares the app-wide `/ws` transport.
 */
/** The registry's own words for one DUT, in this hook's shape.
 *
 *  Written once: the initial load and every refresh must agree about what a
 *  DUT is, and as two copies of the same object literal they had already
 *  started to. */
type RegistryEntry = Pick<
  FleetEntry,
  | "id"
  | "label"
  | "serialOpen"
  | "lastSerial"
  | "remote"
  | "mgmtUrl"
  | "model"
  | "modelCores"
  | "meshProbe"
  | "backhaul"
>;

function fromRegistry(d: DutInfo): RegistryEntry {
  return {
    id: d.id,
    label: d.label,
    serialOpen: d.serial_open,
    lastSerial: d.last_serial,
    remote: d.remote
      ? { host: d.remote.host, port: d.remote.port, device: d.remote.device }
      : null,
    mgmtUrl: d.mgmt_url ?? "",
    model: d.model ?? null,
    modelCores: d.model_cores ?? null,
    meshProbe: d.mesh_probe ?? null,
    backhaul: {
      applicable: d.backhaul.applicable,
      captured: d.backhaul.captured,
      consoleId: d.backhaul.console_id,
      role: d.backhaul.role,
      uplink: d.backhaul.uplink,
      downlink: d.backhaul.downlink,
    },
  };
}

export function useFleetMonitor(): { fleet: FleetEntry[]; refreshRegistry: () => Promise<void> } {
  const { pattern: crashPattern } = useCrashKeywords();
  // Read through a ref inside the socket callbacks so the websocket effect can
  // run once per mount: a pattern change must not tear down the fleet socket
  // (doing so re-rendered via onClose → new pattern → reconnect, ad infinitum).
  const crashPatternRef = useRef(crashPattern);
  crashPatternRef.current = crashPattern;
  // Registry order + labels + open-state for every registered DUT (cards show
  // even with no stream).
  const [duts, setDuts] = useState<RegistryEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const [nowTick, setNowTick] = useState(() => Date.now());

  // Per-DUT live state kept in refs (mutated by the socket, read on each tick) so
  // a burst of events doesn't trigger a render per event — the 2s tick re-renders.
  const baseRef = useRef<Map<string, SnapshotPayload>>(new Map());
  const lastActivityRef = useRef<Map<string, number>>(new Map());
  const crashRef = useRef<Map<string, number>>(new Map());

  // Re-read the registry (labels + serial_open) without re-running the snapshot
  // backfill. Used by quick actions after they change a DUT's open state.
  const refreshRegistry = useCallback(async () => {
    try {
      setDuts((await getDuts()).map(fromRegistry));
    } catch {
      // Keep the current registry view; the action's own error surfaces to the user.
    }
  }, []);

  // Load the registry once, then backfill each DUT's latest snapshot for an
  // instant first paint (CPU + last-ts). Backfill does NOT count as activity, so
  // a stale DUT stays "idle" until a live event — matching useDutMonitor.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      let list: RegistryEntry[] = [];
      try {
        list = (await getDuts()).map(fromRegistry);
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
        if (event.type === "serial_disconnected") {
          setDuts((current) => current.map((dut) =>
            dut.id === dutId ? { ...dut, serialOpen: false } : dut
          ));
          return;
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
          if (crashPatternRef.current.test(event.text)) {
            crashRef.current.set(dutId, (crashRef.current.get(dutId) ?? 0) + 1);
          }
          recordActivity(dutId);
          return;
        }
        if (event.type === "console_line_batch" && Array.isArray(event.lines)) {
          let matched = 0;
          for (const line of event.lines) {
            if (typeof line === "string" && crashPatternRef.current.test(line)) {
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
  }, []);

  // Derive the view rows on each tick from the registry order + live refs.
  const fleet = duts.map(({ id, label, serialOpen, lastSerial, remote, mgmtUrl, model, modelCores, meshProbe, backhaul }) => {
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
      serialOpen,
      lastSerial,
      remote,
      mgmtUrl,
      model,
      modelCores,
      meshProbe,
      backhaul,
      cpuBusyPct: cpu.cpuBusyPct,
      coreCount: cpu.coreCount,
      crashCount: crashRef.current.get(id) ?? 0,
      lastSnapshotTs: base?.device_ts ?? null,
      lastEventAgeSec,
    };
  });

  return { fleet, refreshRegistry };
}
