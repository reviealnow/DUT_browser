import { useEffect, useMemo, useRef, useState } from "react";

import { connectDashboardWebSocket, SnapshotPayload } from "../api/websocket";
import { CRITICAL_CRASH_PATTERN } from "./crash";

const MAX_LINES = 1000;
const MAX_CRASH_LINES = 200;
const MAX_HISTORY = 120;
const STREAM_ACTIVE_WINDOW_MS = 10_000;
const STATUS_TICK_MS = 2_000;

export type DutStatus = "offline" | "idle" | "streaming";

export type CpuHistoryPoint = {
  /** snapshot device_ts (one point per Test Time). */
  ts: string;
  /** 100 - mean idle across cores at this snapshot. */
  busyPct: number;
  /** per-core busy% (100 - idle) at this snapshot. */
  perCore: Record<string, number>;
};

export type DutMonitorState = {
  /** Backend WebSocket link + DUT stream activity. */
  status: DutStatus;
  /** Raw console stream (capped), shared with the Serial Console. */
  lines: string[];
  /** 100 - mean idle across cores, from the latest snapshot. null until first snapshot. */
  cpuBusyPct: number | null;
  cpuIdlePct: number | null;
  coreCount: number;
  /** Latest per-core busy% (100 - idle), for chart legends. */
  cpuPerCoreBusy: Record<string, number>;
  /** Recent CPU busy% history (one point per snapshot), for the trend chart. */
  cpuHistory: CpuHistoryPoint[];
  /** Associated clients summed across radios. null until first wifi update. */
  wifiClientTotal: number | null;
  wifiByRadio: Record<string, number>;
  /** Built-in critical-crash matches seen on the console stream. */
  crashCount: number;
  /** The matched crash lines themselves (capped), newest last. */
  crashLines: string[];
  /** device_ts of the latest snapshot. */
  lastSnapshotTs: string | null;
};

/**
 * Phase 2/3: derive real monitoring state from the existing /ws event stream.
 *
 * Opens a single WebSocket (same contract as the console) and reads the
 * snapshot / wifi / console events the backend already broadcasts. No backend
 * changes; no metrics invented — every value comes from a real event.
 *
 * Phase 3: this is the single source of WS truth. The Serial Console
 * (Dashboard) consumes `lines` from here via context instead of opening its
 * own connection, and the Overview charts read `cpuHistory` / `wifiByRadio` /
 * `crashLines`.
 */
export function useDutMonitor(): DutMonitorState {
  const [lines, setLines] = useState<string[]>([]);
  const [snapshot, setSnapshot] = useState<SnapshotPayload | null>(null);
  const [cpuHistory, setCpuHistory] = useState<CpuHistoryPoint[]>([]);
  const [wifiByRadio, setWifiByRadio] = useState<Record<string, number>>({});
  const [wifiSeen, setWifiSeen] = useState(false);
  const [connected, setConnected] = useState(false);
  const [nowTick, setNowTick] = useState(() => Date.now());

  const lastActivityRef = useRef(0);

  useEffect(() => {
    const recordActivity = () => {
      lastActivityRef.current = Date.now();
    };

    const ingestLines = (incoming: string[]) => {
      if (incoming.length === 0) {
        return;
      }
      setLines((prev) => [...prev, ...incoming].slice(-MAX_LINES));
      recordActivity();
    };

    const ws = connectDashboardWebSocket((event) => {
      const maybeText = (event as { text?: unknown }).text;
      if (event.type === "console_line" && typeof maybeText === "string") {
        ingestLines([maybeText]);
        return;
      }
      if (event.type === "console_line_batch" && Array.isArray(event.lines)) {
        ingestLines(event.lines as string[]);
        return;
      }
      if (event.type === "snapshot_update" && "snapshot" in event) {
        const snap = (event as { snapshot: SnapshotPayload }).snapshot;
        setSnapshot(snap);
        setCpuHistory((prev) => upsertCpuPoint(prev, snap));
        recordActivity();
        return;
      }
      if (event.type === "wifi_clients_update") {
        const radio = (event as { radio?: string }).radio;
        const totalSize = (event as { total_size?: number }).total_size;
        if (typeof radio === "string" && typeof totalSize === "number") {
          setWifiByRadio((prev) => ({ ...prev, [radio]: totalSize }));
          setWifiSeen(true);
          recordActivity();
        }
      }
    });

    ws.addEventListener("open", () => setConnected(true));
    ws.addEventListener("close", () => setConnected(false));
    if (ws.readyState === WebSocket.OPEN) {
      setConnected(true);
    }

    const interval = window.setInterval(() => setNowTick(Date.now()), STATUS_TICK_MS);

    return () => {
      window.clearInterval(interval);
      ws.close();
    };
  }, []);

  // Keep wifi totals in sync with the latest snapshot's client list when present.
  useEffect(() => {
    if (!snapshot?.wifi_clients) {
      return;
    }
    const fromSnapshot: Record<string, number> = {};
    for (const [radio, payload] of Object.entries(snapshot.wifi_clients)) {
      fromSnapshot[radio] = payload.total_size;
    }
    if (Object.keys(fromSnapshot).length > 0) {
      setWifiByRadio((prev) => ({ ...prev, ...fromSnapshot }));
      setWifiSeen(true);
    }
  }, [snapshot]);

  const cpu = useMemo(() => cpuFromSnapshot(snapshot), [snapshot]);

  const { crashLines, crashCount } = useMemo(() => {
    const matched = lines.filter((line) => CRITICAL_CRASH_PATTERN.test(line)).slice(-MAX_CRASH_LINES);
    return { crashLines: matched, crashCount: matched.length };
  }, [lines]);

  const wifiClientTotal = useMemo(() => {
    if (!wifiSeen) {
      return null;
    }
    return Object.values(wifiByRadio).reduce((sum, value) => sum + value, 0);
  }, [wifiByRadio, wifiSeen]);

  const status: DutStatus = useMemo(() => {
    if (!connected) {
      return "offline";
    }
    return nowTick - lastActivityRef.current < STREAM_ACTIVE_WINDOW_MS ? "streaming" : "idle";
  }, [connected, nowTick]);

  return {
    status,
    lines,
    cpuBusyPct: cpu.cpuBusyPct,
    cpuIdlePct: cpu.cpuIdlePct,
    coreCount: cpu.coreCount,
    cpuPerCoreBusy: cpu.perCore,
    cpuHistory,
    wifiClientTotal,
    wifiByRadio,
    crashCount,
    crashLines,
    lastSnapshotTs: snapshot?.device_ts ?? null,
  };
}

function cpuFromSnapshot(snapshot: SnapshotPayload | null): {
  cpuBusyPct: number | null;
  cpuIdlePct: number | null;
  coreCount: number;
  perCore: Record<string, number>;
} {
  if (!snapshot) {
    return { cpuBusyPct: null, cpuIdlePct: null, coreCount: 0, perCore: {} };
  }
  const entries = Object.entries(snapshot.cpu ?? {});
  if (entries.length === 0) {
    return { cpuBusyPct: null, cpuIdlePct: null, coreCount: 0, perCore: {} };
  }
  const perCore: Record<string, number> = {};
  let idleSum = 0;
  for (const [coreId, core] of entries) {
    const idle = core.idle ?? 0;
    idleSum += idle;
    perCore[coreId] = round1(Math.max(0, 100 - idle));
  }
  const meanIdle = idleSum / entries.length;
  return {
    cpuBusyPct: round1(Math.max(0, 100 - meanIdle)),
    cpuIdlePct: round1(meanIdle),
    coreCount: entries.length,
    perCore,
  };
}

// One point per Test Time: replace the last point while cores stream in for the
// same device_ts, otherwise append a new point.
function upsertCpuPoint(history: CpuHistoryPoint[], snapshot: SnapshotPayload): CpuHistoryPoint[] {
  const cpu = cpuFromSnapshot(snapshot);
  if (cpu.cpuBusyPct === null) {
    return history;
  }
  const point: CpuHistoryPoint = {
    ts: snapshot.device_ts,
    busyPct: cpu.cpuBusyPct,
    perCore: cpu.perCore,
  };
  const last = history[history.length - 1];
  if (last && last.ts === point.ts) {
    return [...history.slice(0, -1), point];
  }
  return [...history, point].slice(-MAX_HISTORY);
}

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}
