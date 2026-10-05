import { DEFAULT_DUT_ID } from "../api/dut";
import type {
  DashboardEvent,
  DashboardSocket,
  DashboardSocketHandlers,
  FleetSocketHandlers,
  SnapshotPayload,
} from "../api/websocket";
import type { IDataSource } from "./IDataSource";

/** The demo has exactly one DUT, under the id the app already defaults to. */
export const DEMO_DUT_ID = DEFAULT_DUT_ID;
export const DEMO_DEVICE_ID = "demo-ap-01";

/** One tick of `public/demo/system-monitor.json`: a snapshot minus its clock. */
export type ReplayRecord = Pick<SnapshotPayload, "cpu" | "memory" | "wifi_clients">;

export type ReplayDataset = {
  interval_ms: number;
  records: ReplayRecord[];
  /** Extra console lines, each emitted right after record `at`. */
  events?: { at: number; line: string }[];
};

/** Where a file under `public/` lives once Vite has applied `base`. */
export function demoAssetUrl(name: string): string {
  return `${import.meta.env.BASE_URL}demo/${name}`;
}

/** Test count the replay starts from, so the demo reads as a session in progress. */
const FIRST_TEST_COUNT = 1000;

type Subscriber =
  | { kind: "dut"; dutId: string; handlers: DashboardSocketHandlers }
  | { kind: "fleet"; handlers: FleetSocketHandlers };

/**
 * Demo mode: replays bundled synthetic telemetry on a clock, in the exact
 * event shapes the backend broadcasts, so every chart, KPI and console view
 * runs unmodified on it.
 *
 * - One clock for every subscriber (as live mode has one socket), started by
 *   the first subscriber and stopped by the last.
 * - Each tick emits the console block sysMon would have printed, then the
 *   `snapshot_update` parsed from it, stamped with the wall clock. Stamping
 *   "now" is what lets the dataset loop: every pass yields fresh `device_ts`
 *   values, so the monitors' one-point-per-Test-Time upsert keeps appending
 *   rather than overwriting the points it drew on the previous pass.
 * - History (`loadSnapshots` / `loadConsoleTail`) is the records *before* the
 *   cursor, back-dated one interval apiece, so the charts are full on first
 *   paint the way a live backfill makes them.
 *
 * It never opens a socket or calls the backend; the only requests are for its
 * own files under `<base>/demo/`.
 */
export class ReplayDataSource implements IDataSource {
  private readonly loader: () => Promise<ReplayDataset>;
  private dataset: ReplayDataset | null = null;
  private loading: Promise<ReplayDataset> | null = null;
  private readonly subscribers = new Set<Subscriber>();
  private timer: ReturnType<typeof setInterval> | null = null;
  /** Index of the next record to emit; grows without bound, read modulo length. */
  private cursor = 0;
  private readonly now: () => number;

  constructor(options: { loader?: () => Promise<ReplayDataset>; now?: () => number } = {}) {
    this.loader = options.loader ?? fetchDataset;
    this.now = options.now ?? Date.now;
  }

  subscribe(handlers: DashboardSocketHandlers, dutId: string): DashboardSocket {
    return this.add({ kind: "dut", dutId, handlers });
  }

  subscribeFleet(handlers: FleetSocketHandlers): DashboardSocket {
    return this.add({ kind: "fleet", handlers });
  }

  async loadSnapshots(limit: number, dutId: string): Promise<SnapshotPayload[]> {
    if (dutId !== DEMO_DUT_ID) return [];
    const data = await this.load();
    const count = Math.min(limit, data.records.length);
    const snaps: SnapshotPayload[] = [];
    for (let back = count; back >= 1; back -= 1) {
      snaps.push(this.snapshotAt(data, this.cursor - back, this.now() - back * data.interval_ms));
    }
    return snaps;
  }

  async loadConsoleTail(limit: number, dutId: string): Promise<string[]> {
    if (dutId !== DEMO_DUT_ID) return [];
    const data = await this.load();
    const lines: string[] = [];
    for (let back = data.records.length; back >= 1 && lines.length < limit * 2; back -= 1) {
      const index = this.cursor - back;
      const snap = this.snapshotAt(data, index, this.now() - back * data.interval_ms);
      lines.push(...consoleBlock(snap), ...eventsAfter(data, index));
    }
    return lines.slice(-limit);
  }

  private add(subscriber: Subscriber): DashboardSocket {
    this.subscribers.add(subscriber);
    void this.load().then(
      () => {
        if (!this.subscribers.has(subscriber)) return;
        subscriber.handlers.onOpen?.();
        this.start();
      },
      () => {
        // Logged once by load(); the dashboard stays "offline", which is true.
      },
    );
    return {
      close: () => {
        this.subscribers.delete(subscriber);
        if (this.subscribers.size === 0) this.stop();
      },
    };
  }

  private load(): Promise<ReplayDataset> {
    if (this.dataset) return Promise.resolve(this.dataset);
    if (!this.loading) {
      this.loading = this.loader().then(
        (data) => {
          if (!Array.isArray(data.records) || data.records.length === 0) {
            throw new Error("demo dataset has no records");
          }
          this.dataset = data;
          return data;
        },
        (error: unknown) => {
          this.loading = null; // a later subscriber may retry
          console.error("[demo] could not load the replay dataset:", error);
          throw error;
        },
      );
    }
    return this.loading;
  }

  private start(): void {
    if (this.timer !== null || !this.dataset) return;
    const data = this.dataset;
    this.timer = setInterval(() => this.tick(data), data.interval_ms);
  }

  private stop(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private tick(data: ReplayDataset): void {
    const index = this.cursor;
    this.cursor += 1;
    const snapshot = this.snapshotAt(data, index, this.now());
    const lines = [...consoleBlock(snapshot), ...eventsAfter(data, index)];
    const events: DashboardEvent[] = [
      { type: "console_line_batch", lines },
      { type: "snapshot_update", snapshot },
    ];
    for (const subscriber of [...this.subscribers]) {
      for (const event of events) {
        if (subscriber.kind === "fleet") {
          subscriber.handlers.onEvent({ ...event, dut_id: DEMO_DUT_ID });
        } else if (subscriber.dutId === DEMO_DUT_ID) {
          subscriber.handlers.onEvent(event);
        }
      }
    }
  }

  private snapshotAt(data: ReplayDataset, index: number, atMs: number): SnapshotPayload {
    const length = data.records.length;
    const record = data.records[((index % length) + length) % length];
    return {
      test_count: FIRST_TEST_COUNT + index,
      device_ts: formatDeviceTs(atMs),
      device_id: DEMO_DEVICE_ID,
      cpu: record.cpu,
      memory: record.memory,
      wifi_clients: record.wifi_clients,
    };
  }
}

async function fetchDataset(): Promise<ReplayDataset> {
  const response = await fetch(demoAssetUrl("system-monitor.json"));
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} for ${response.url}`);
  }
  return (await response.json()) as ReplayDataset;
}

function eventsAfter(data: ReplayDataset, index: number): string[] {
  const length = data.records.length;
  const at = ((index % length) + length) % length;
  return (data.events ?? []).filter((event) => event.at === at).map((event) => event.line);
}

/** `YYYY-MM-DD HH:MM:SS` in local time — the format sysMon prints. */
export function formatDeviceTs(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * The console text sysMon prints for one snapshot, in the formats
 * `backend/app/parser/sysmon_parser.py` parses, so the Serial Console and the
 * charts in the demo describe the same reading.
 */
export function consoleBlock(snapshot: SnapshotPayload): string[] {
  const pct = (v: number) => `${v.toFixed(1).padStart(5)}%`;
  const lines = [`= Test Time: ${snapshot.test_count}, ${snapshot.device_ts} =`];
  for (const [core, c] of Object.entries(snapshot.cpu)) {
    lines.push(
      `CPU${core}: ${pct(c.usr)} usr ${pct(c.sys)} sys ${pct(c.nic)} nic ${pct(c.idle)} idle ` +
        `${pct(c.io)} io ${pct(c.irq)} irq ${pct(c.sirq)} sirq`,
    );
  }
  for (const [key, kb] of Object.entries(snapshot.memory ?? {})) {
    lines.push(`${`${key}:`.padEnd(16)}${String(kb).padStart(8)} kB`);
  }
  for (const [radio, payload] of Object.entries(snapshot.wifi_clients ?? {})) {
    lines.push(`--- CLIENTS Radio=${radio} ---`);
    lines.push(JSON.stringify({ data: { total_size: payload.total_size, client_list: payload.clients } }));
  }
  return lines;
}
