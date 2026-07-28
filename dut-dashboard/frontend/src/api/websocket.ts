import { DEFAULT_DUT_ID } from "./dut";

export type CpuCore = {
  usr: number;
  sys: number;
  nic: number;
  idle: number;
  io: number;
  irq: number;
  sirq: number;
};

export type WifiClient = {
  mac?: string;
  ip?: string;
  rssi?: number;
  snr?: number;
  [key: string]: unknown;
};

/** Selected /proc/meminfo keys (kB), streamed live inside each snapshot. */
export type MemoryInfo = Record<string, number>;

export type SnapshotPayload = {
  test_count: number;
  device_ts: string;
  cpu: Record<string, CpuCore>;
  memory?: MemoryInfo;
  wifi_clients?: Record<string, { total_size: number; clients: WifiClient[] }>;
};

export type SnapshotDelta = {
  test_count?: number;
  device_ts?: string;
  cpu?: Record<string, CpuCore>;
  cpu_removed?: string[];
  memory?: MemoryInfo;
  wifi_clients?: Record<string, { total_size: number; clients: WifiClient[] }>;
  wifi_clients_removed?: string[];
};

export type DashboardEvent =
  | { type: "console_line"; text: string }
  | { type: "console_line_batch"; lines: string[] }
  | {
      type: "snapshot_update";
      snapshot: SnapshotPayload;
    }
  | {
      type: "snapshot_delta";
      delta: SnapshotDelta;
    }
  | {
      type: "wifi_clients_update";
      radio: "2G" | "5G" | "6G";
      total_size: number;
      clients: WifiClient[];
    }
  | {
      type: "survey_progress";
      stage: "capabilities" | "scanning" | "done";
      iface: string | null;
      index: number;
      total: number;
    }
  | {
      type: "firmware_progress";
      stage: "verifying" | "connecting" | "uploading" | "applying" | "done";
      detail: string;
      dry_run: boolean;
    };
// Note: this is a closed discriminated union. Unknown runtime event types are
// parsed as DashboardEvent and fall through the type checks (ignored). A
// permissive `{ type: string; [k]: unknown }` member is intentionally omitted
// because it poisons discriminant narrowing on the members above.

export type DashboardSocket = { close: () => void };

export type DashboardSocketHandlers = {
  onEvent: (event: DashboardEvent) => void;
  /** Fired on every (re)connect once the socket is open. */
  onOpen?: () => void;
  /** Fired on every drop/close. */
  onClose?: () => void;
};

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 10_000;

/**
 * Self-reconnecting dashboard WebSocket. Reconnects with exponential backoff
 * (1s → cap 10s, + jitter) until close() is called, so a backend restart is
 * recovered automatically. `latestSnapshot` (the delta base) is reset on each
 * (re)connect; callers should re-run backfill in onOpen.
 */
export function connectDashboardWebSocket(
  handlers: DashboardSocketHandlers,
  dutId = DEFAULT_DUT_ID,
): DashboardSocket {
  const { onEvent, onOpen, onClose } = handlers;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${window.location.host}/ws?dut=${dutId}`;

  let ws: WebSocket | null = null;
  let closedByCaller = false;
  let reconnectTimer: number | null = null;
  let attempt = 0;

  const scheduleReconnect = () => {
    if (closedByCaller) {
      return;
    }
    const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
    const jitter = Math.random() * 0.3 * delay;
    attempt += 1;
    reconnectTimer = window.setTimeout(open, delay + jitter);
  };

  function open() {
    let latestSnapshot: SnapshotPayload | null = null;
    const socket = new WebSocket(url);
    ws = socket;

    socket.onopen = () => {
      attempt = 0;
      onOpen?.();
    };

    socket.onmessage = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as DashboardEvent;
        if (event && typeof event === "object" && "type" in event) {
          // The shared /ws carries every DUT's events tagged with dut_id; keep
          // only this connection's DUT so the delta-base below stays single-DUT.
          // (Events without a dut_id are treated as a match for back-compat.)
          const eventDut = (event as { dut_id?: string }).dut_id;
          if (eventDut && eventDut !== dutId) {
            return;
          }
          if (event.type === "snapshot_update") {
            latestSnapshot = event.snapshot;
            onEvent(event);
            return;
          }
          if (event.type === "snapshot_delta") {
            if (!latestSnapshot) {
              return;
            }
            latestSnapshot = applySnapshotDelta(latestSnapshot, event.delta);
            onEvent({ type: "snapshot_update", snapshot: latestSnapshot });
            return;
          }
          onEvent(event);
        }
      } catch {
        // Ignore malformed messages.
      }
    };

    socket.onclose = () => {
      onClose?.();
      scheduleReconnect();
    };

    socket.onerror = () => {
      socket.close(); // triggers onclose -> reconnect
    };
  }

  open();

  return {
    close: () => {
      closedByCaller = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      ws?.close();
    },
  };
}

/**
 * Fleet variant: a single self-reconnecting socket that does NOT filter by
 * dut_id and forwards every event raw (including `snapshot_delta`). The shared
 * `/ws` already broadcasts every DUT's events tagged with `dut_id`, so one
 * connection feeds the whole fleet; the caller demuxes by `dut_id` and keeps a
 * per-DUT delta base (see useFleetMonitor). The single-DUT
 * `connectDashboardWebSocket` above is intentionally left untouched.
 */
export type FleetSocketHandlers = {
  onEvent: (event: DashboardEvent & { dut_id?: string }) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

export function connectFleetWebSocket(handlers: FleetSocketHandlers): DashboardSocket {
  const { onEvent, onOpen, onClose } = handlers;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${window.location.host}/ws`;

  let ws: WebSocket | null = null;
  let closedByCaller = false;
  let reconnectTimer: number | null = null;
  let attempt = 0;

  const scheduleReconnect = () => {
    if (closedByCaller) {
      return;
    }
    const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
    const jitter = Math.random() * 0.3 * delay;
    attempt += 1;
    reconnectTimer = window.setTimeout(open, delay + jitter);
  };

  function open() {
    const socket = new WebSocket(url);
    ws = socket;

    socket.onopen = () => {
      attempt = 0;
      onOpen?.();
    };

    socket.onmessage = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as DashboardEvent & { dut_id?: string };
        if (event && typeof event === "object" && "type" in event) {
          onEvent(event);
        }
      } catch {
        // Ignore malformed messages.
      }
    };

    socket.onclose = () => {
      onClose?.();
      scheduleReconnect();
    };

    socket.onerror = () => {
      socket.close(); // triggers onclose -> reconnect
    };
  }

  open();

  return {
    close: () => {
      closedByCaller = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      ws?.close();
    },
  };
}

export function applySnapshotDelta(base: SnapshotPayload, delta: SnapshotDelta): SnapshotPayload {
  const nextCpu = { ...base.cpu };
  if (delta.cpu_removed) {
    for (const coreId of delta.cpu_removed) {
      delete nextCpu[coreId];
    }
  }
  if (delta.cpu) {
    Object.assign(nextCpu, delta.cpu);
  }

  const nextMemory = { ...(base.memory ?? {}) };
  if (delta.memory) {
    Object.assign(nextMemory, delta.memory);
  }

  const nextWifi = { ...(base.wifi_clients ?? {}) };
  if (delta.wifi_clients_removed) {
    for (const radio of delta.wifi_clients_removed) {
      delete nextWifi[radio];
    }
  }
  if (delta.wifi_clients) {
    Object.assign(nextWifi, delta.wifi_clients);
  }

  return {
    test_count: delta.test_count ?? base.test_count,
    device_ts: delta.device_ts ?? base.device_ts,
    cpu: nextCpu,
    memory: nextMemory,
    wifi_clients: nextWifi,
  };
}
