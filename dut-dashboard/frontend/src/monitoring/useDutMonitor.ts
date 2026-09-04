import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DEFAULT_DUT_ID } from "../api/dut";
import { getConsoleTail, getSnapshots } from "../api/rest";
import { connectDashboardWebSocket, SnapshotPayload } from "../api/websocket";
import { createLineBuffer } from "./consoleLineBuffer";
import { setFirmwareProgress } from "./firmwareStore";
import { setSurveyProgress } from "./siteSurveyStore";
import { useCrashKeywords } from "./useCrashKeywords";

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

/**
 * Live memory at one snapshot, parsed from the streamed /proc/meminfo (kB).
 * `effectiveKb` = MemAvailable − SUnreclaim, matching the offline analyzer.
 * Fields are null when that key was absent from the stream.
 */
export type MemorySample = {
  ts: string;
  memTotalKb: number | null;
  memFreeKb: number | null;
  memAvailableKb: number | null;
  buffersKb: number | null;
  cachedKb: number | null;
  slabKb: number | null;
  sunreclaimKb: number | null;
  effectiveKb: number | null;
};

/** A serial drop the backend pushed, newest wins. */
export type SerialDisconnect = {
  /** The backend's raw reason (e.g. an errno string) — diagnostic, not UI copy. */
  detail: string;
  /** Receipt time; also makes a second drop a distinct object for effects. */
  at: number;
};

export type DutMonitorState = {
  /** Backend WebSocket link + DUT stream activity. */
  status: DutStatus;
  /** Raw console stream (capped), shared with the Serial Console. */
  lines: string[];
  /**
   * How many lines have already scrolled out of `lines` this session, so that
   * `linesStartSeq + index` gives a line its position in the whole stream.
   *
   * Here so a renderer can key each line by something that does not change when
   * the window slides. Keying by array index instead shifts every element's
   * content by one on every batch and React rewrites all thousand of them:
   * measured in Chrome at 7.44 ms an update against 0.52 ms with stable keys --
   * worse than the single text node it would be replacing.
   */
  linesStartSeq: number;
  /** 100 - mean idle across cores, from the latest snapshot. null until first snapshot. */
  cpuBusyPct: number | null;
  cpuIdlePct: number | null;
  coreCount: number;
  /** Latest per-core busy% (100 - idle), for chart legends. */
  cpuPerCoreBusy: Record<string, number>;
  /** Recent CPU busy% history (one point per snapshot), for the trend chart. */
  cpuHistory: CpuHistoryPoint[];
  /** Live memory from the latest snapshot's /proc/meminfo. null until one streams. */
  memoryLive: MemorySample | null;
  /** Recent live-memory history (one point per snapshot), for the trend chart. */
  memoryHistory: MemorySample[];
  /** Associated clients summed across radios. null until first wifi update. */
  wifiClientTotal: number | null;
  wifiByRadio: Record<string, number>;
  /** Built-in critical-crash matches seen on the console stream. */
  crashCount: number;
  /** The matched crash lines themselves (capped), newest last. */
  crashLines: string[];
  /** device_ts of the latest snapshot. */
  lastSnapshotTs: string | null;
  /** Whole seconds since the last stream event; null before any event. */
  lastEventAgeSec: number | null;
  /** Set when this DUT's serial device vanished mid-session; null otherwise. */
  serialDisconnect: SerialDisconnect | null;
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
export function useDutMonitor(dutId: string = DEFAULT_DUT_ID): DutMonitorState {
  const { pattern: crashPattern } = useCrashKeywords();
  // The window and how far it has slid, in ONE state. They have to move
  // together: a commit that dropped lines without advancing the count would
  // hand every surviving line a key belonging to a different line, which is
  // worse than no key at all.
  const [consoleWindow, setConsoleWindow] = useState<{ lines: string[]; startSeq: number }>({
    lines: [],
    startSeq: 0,
  });
  const lines = consoleWindow.lines;
  const [snapshot, setSnapshot] = useState<SnapshotPayload | null>(null);
  const [cpuHistory, setCpuHistory] = useState<CpuHistoryPoint[]>([]);
  const [memoryHistory, setMemoryHistory] = useState<MemorySample[]>([]);
  const [wifiByRadio, setWifiByRadio] = useState<Record<string, number>>({});
  const [wifiSeen, setWifiSeen] = useState(false);
  const [connected, setConnected] = useState(false);
  const [serialDisconnect, setSerialDisconnect] = useState<SerialDisconnect | null>(null);
  const [nowTick, setNowTick] = useState(() => Date.now());

  const lastActivityRef = useRef(0);

  // Seed charts/KPIs/console from server-side history so they populate instantly
  // on (re)connect instead of waiting for the next event. Idempotent (upsert by
  // device_ts / seed-if-empty) so it is safe to call on every reconnect.
  const runBackfill = useCallback(async () => {
    try {
      const snaps = await getSnapshots(MAX_HISTORY, dutId);
      if (snaps.length > 0) {
        const backfillHistory = snaps.reduce<CpuHistoryPoint[]>((acc, snap) => upsertCpuPoint(acc, snap), []);
        setCpuHistory((prev) => {
          const seen = new Set(prev.map((point) => point.ts));
          const additions = backfillHistory.filter((point) => !seen.has(point.ts));
          return [...additions, ...prev].slice(-MAX_HISTORY);
        });

        const backfillMemory = snaps.reduce<MemorySample[]>((acc, snap) => upsertMemoryPoint(acc, snap), []);
        setMemoryHistory((prev) => {
          const seen = new Set(prev.map((point) => point.ts));
          const additions = backfillMemory.filter((point) => !seen.has(point.ts));
          return [...additions, ...prev].slice(-MAX_HISTORY);
        });

        const backfillWifi: Record<string, number> = {};
        let sawWifi = false;
        for (const snap of snaps) {
          for (const [radio, payload] of Object.entries(snap.wifi_clients ?? {})) {
            backfillWifi[radio] = payload.total_size;
            sawWifi = true;
          }
        }
        if (sawWifi) {
          setWifiByRadio((prev) => ({ ...backfillWifi, ...prev }));
          setWifiSeen(true);
        }
        setSnapshot((prev) => prev ?? snaps[snaps.length - 1]);
      }
    } catch {
      // Offline or endpoint unavailable: charts stay empty until live.
    }

    try {
      const tail = await getConsoleTail(MAX_LINES, dutId);
      // Console is an unkeyed append-only stream → seed only if empty (live wins).
      if (tail.length > 0) {
        setConsoleWindow((prev) =>
          prev.lines.length > 0 ? prev : { lines: tail.slice(-MAX_LINES), startSeq: 0 },
        );
      }
    } catch {
      // Offline or endpoint unavailable: console stays empty until live.
    }
  }, [dutId]);

  // Switching DUT: drop the previous DUT's data so nothing lingers before the
  // new DUT's backfill/live events arrive.
  useEffect(() => {
    // startSeq restarts too: it numbers one DUT's stream, and the lines it
    // numbered are gone.
    setConsoleWindow({ lines: [], startSeq: 0 });
    setSnapshot(null);
    setCpuHistory([]);
    setMemoryHistory([]);
    setWifiByRadio({});
    setWifiSeen(false);
    setSerialDisconnect(null);
    lastActivityRef.current = 0;
  }, [dutId]);

  useEffect(() => {
    const recordActivity = () => {
      lastActivityRef.current = Date.now();
    };

    // Coalesced rather than committed per batch. The backend already batches,
    // so a flooding console arrives as ~20 setLines a second, and each one
    // re-renders this tree and re-lays out the console box -- 6.25 ms a time,
    // measured in Chrome on 1000 lines of real sysMon output. A site survey
    // holds that rate for minutes, which is when the page stops answering the
    // mouse. See consoleLineBuffer.ts for why it is a window and not a debounce.
    //
    // `recordActivity` stays on the ARRIVAL, not on the flush: "last event Ns
    // ago" answers whether the DUT is talking, and delaying that by up to a
    // flush window would make a healthy console look stale.
    const buffer = createLineBuffer((flushed) => {
      setConsoleWindow((prev) => {
        const merged = prev.lines.concat(flushed);
        const overflow = Math.max(0, merged.length - MAX_LINES);
        return {
          lines: overflow > 0 ? merged.slice(overflow) : merged,
          startSeq: prev.startSeq + overflow,
        };
      });
    });

    const ingestLines = (incoming: string[]) => {
      if (incoming.length === 0) {
        return;
      }
      buffer.push(incoming);
      recordActivity();
    };

    const socket = connectDashboardWebSocket({
      onEvent: (event) => {
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
          setMemoryHistory((prev) => upsertMemoryPoint(prev, snap));
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
          return;
        }
        if (event.type === "survey_progress") {
          // Forwarded into the module-level survey store (not React state here):
          // the scan may have been started by the connect-time prescan or the
          // SiteSurveyCard, both of which subscribe to that store.
          setSurveyProgress(dutId, {
            stage: event.stage,
            iface: event.iface,
            index: event.index,
            total: event.total,
          });
          return;
        }
        if (event.type === "serial_disconnected") {
          // /ws is one broadcast stream for every DUT, so check whose device
          // dropped — another DUT's adapter must not close this session's UI.
          if (event.dut_id === dutId) {
            setSerialDisconnect({ detail: event.detail, at: Date.now() });
          }
          return;
        }
        if (event.type === "firmware_progress") {
          // Same module-store pattern as the survey: the Firmware panel may not
          // be mounted when the flash starts, and must not miss stages.
          setFirmwareProgress(dutId, {
            stage: event.stage,
            detail: event.detail,
            dryRun: event.dry_run,
          });
        }
      },
      // Runs on first connect and on every reconnect → recover after a drop.
      onOpen: () => {
        setConnected(true);
        void runBackfill();
      },
      onClose: () => setConnected(false),
    }, dutId);

    const interval = window.setInterval(() => setNowTick(Date.now()), STATUS_TICK_MS);

    return () => {
      window.clearInterval(interval);
      socket.close();
      // Dropped, not flushed: this tears down on a DUT switch, and the effect
      // below clears `lines` for the new DUT. Flushing here would push the old
      // DUT's last few lines into the new one's console.
      buffer.cancel();
    };
  }, [runBackfill]);

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
  const memoryLive = useMemo(() => memoryFromSnapshot(snapshot), [snapshot]);

  const { crashLines, crashCount } = useMemo(() => {
    const matched = lines.filter((line) => crashPattern.test(line)).slice(-MAX_CRASH_LINES);
    return { crashLines: matched, crashCount: matched.length };
  }, [lines, crashPattern]);

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

  const lastEventAgeSec = useMemo(() => {
    if (lastActivityRef.current === 0) {
      return null;
    }
    return Math.max(0, Math.floor((nowTick - lastActivityRef.current) / 1000));
  }, [nowTick]);

  return {
    status,
    lines,
    linesStartSeq: consoleWindow.startSeq,
    cpuBusyPct: cpu.cpuBusyPct,
    cpuIdlePct: cpu.cpuIdlePct,
    coreCount: cpu.coreCount,
    cpuPerCoreBusy: cpu.perCore,
    cpuHistory,
    memoryLive,
    memoryHistory,
    wifiClientTotal,
    wifiByRadio,
    crashCount,
    crashLines,
    lastSnapshotTs: snapshot?.device_ts ?? null,
    lastEventAgeSec,
    serialDisconnect,
  };
}

export function cpuFromSnapshot(snapshot: SnapshotPayload | null): {
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

// Build a MemorySample from a snapshot's streamed /proc/meminfo. Returns null
// when no memory keys are present (e.g. snapshots from before live memory, or a
// DUT that does not dump meminfo) so the card can fall back to post-analysis.
function memoryFromSnapshot(snapshot: SnapshotPayload | null): MemorySample | null {
  const mem = snapshot?.memory;
  if (!mem || Object.keys(mem).length === 0) {
    return null;
  }
  const get = (key: string): number | null => (typeof mem[key] === "number" ? mem[key] : null);
  const memAvailableKb = get("MemAvailable");
  const sunreclaimKb = get("SUnreclaim");
  const effectiveKb =
    memAvailableKb !== null && sunreclaimKb !== null ? memAvailableKb - sunreclaimKb : memAvailableKb;
  return {
    ts: snapshot!.device_ts,
    memTotalKb: get("MemTotal"),
    memFreeKb: get("MemFree"),
    memAvailableKb,
    buffersKb: get("Buffers"),
    cachedKb: get("Cached"),
    slabKb: get("Slab"),
    sunreclaimKb,
    effectiveKb,
  };
}

// One point per Test Time, mirroring upsertCpuPoint: replace the last point
// while memory keys stream in for the same device_ts, otherwise append.
function upsertMemoryPoint(history: MemorySample[], snapshot: SnapshotPayload): MemorySample[] {
  const sample = memoryFromSnapshot(snapshot);
  if (sample === null || sample.effectiveKb === null) {
    return history;
  }
  const last = history[history.length - 1];
  if (last && last.ts === sample.ts) {
    return [...history.slice(0, -1), sample];
  }
  return [...history, sample].slice(-MAX_HISTORY);
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
