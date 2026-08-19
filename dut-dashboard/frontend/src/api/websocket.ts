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
    }
  | {
      // The serial device went away mid-session (adapter unplugged, DUT rebooted,
      // port re-enumerated). Pushed so the UI stops claiming Connected; the
      // backend never reconnects on its own, so a human must press Connect.
      type: "serial_disconnected";
      dut_id: string;
      detail: string;
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

export type FleetSocketHandlers = {
  onEvent: (event: DashboardEvent & { dut_id?: string }) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

const subscribers = new Set<FleetSocketHandlers>();
let sharedSocket: WebSocket | null = null;
let sharedReconnectTimer: number | null = null;
let sharedAttempt = 0;
let sharedConnected = false;

function scheduleSharedReconnect(): void {
  if (subscribers.size === 0 || sharedReconnectTimer !== null) return;
  const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** sharedAttempt);
  const jitter = Math.random() * 0.3 * delay;
  sharedAttempt += 1;
  sharedReconnectTimer = window.setTimeout(() => {
    sharedReconnectTimer = null;
    openSharedSocket();
  }, delay + jitter);
}

function openSharedSocket(): void {
  if (subscribers.size === 0 || sharedSocket !== null) return;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws`);
  sharedSocket = socket;
  // Every handler below starts with the same guard. A socket stays able to fire
  // after it has been superseded — a close is a handshake, not an instant — and
  // a superseded one must neither touch the shared state nor speak for the
  // transport: nulling `sharedSocket` would strand the live socket and schedule
  // a reconnect on top of it, and forwarding a frame that arrived during its
  // closing handshake delivers that event to every subscriber twice, since the
  // backend is broadcasting to the live socket as well.
  socket.onopen = () => {
    if (sharedSocket !== socket) return;
    sharedAttempt = 0;
    sharedConnected = true;
    subscribers.forEach((subscriber) => subscriber.onOpen?.());
  };
  socket.onmessage = (message: MessageEvent<string>) => {
    if (sharedSocket !== socket) return;
    try {
      const event = JSON.parse(message.data) as DashboardEvent & { dut_id?: string };
      if (event && typeof event === "object" && "type" in event) {
        subscribers.forEach((subscriber) => subscriber.onEvent(event));
      }
    } catch {
      // Ignore malformed messages.
    }
  };
  socket.onclose = () => {
    if (sharedSocket !== socket) return;
    sharedSocket = null;
    if (sharedConnected) {
      sharedConnected = false;
      subscribers.forEach((subscriber) => subscriber.onClose?.());
    }
    scheduleSharedReconnect();
  };
  socket.onerror = () => socket.close();
}

function subscribeSharedSocket(handlers: FleetSocketHandlers): DashboardSocket {
  subscribers.add(handlers);
  if (sharedConnected) handlers.onOpen?.();
  openSharedSocket();
  return {
    close: () => {
      subscribers.delete(handlers);
      if (subscribers.size !== 0) return;
      if (sharedReconnectTimer !== null) {
        window.clearTimeout(sharedReconnectTimer);
        sharedReconnectTimer = null;
      }
      const socket = sharedSocket;
      sharedSocket = null;
      sharedConnected = false;
      socket?.close();
    },
  };
}

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
  let latestSnapshot: SnapshotPayload | null = null;
  return subscribeSharedSocket({
    onEvent: (event) => {
      const eventDut = event.dut_id;
      if (eventDut && eventDut !== dutId) return;
      if (event.type === "snapshot_update") {
        latestSnapshot = event.snapshot;
        handlers.onEvent(event);
        return;
      }
      if (event.type === "snapshot_delta") {
        if (!latestSnapshot) return;
        latestSnapshot = applySnapshotDelta(latestSnapshot, event.delta);
        handlers.onEvent({ type: "snapshot_update", snapshot: latestSnapshot });
        return;
      }
      handlers.onEvent(event);
    },
    onOpen: () => {
      latestSnapshot = null;
      handlers.onOpen?.();
    },
    onClose: handlers.onClose,
  });
}

/**
 * Fleet subscription that does NOT filter by
 * dut_id and forwards every event raw (including `snapshot_delta`). The shared
 * `/ws` already broadcasts every DUT's events tagged with `dut_id`, so one
 * connection feeds the whole fleet and the selected-DUT monitor. The caller
 * demuxes by `dut_id` and keeps a per-DUT delta base (see useFleetMonitor).
 */
export function connectFleetWebSocket(handlers: FleetSocketHandlers): DashboardSocket {
  return subscribeSharedSocket(handlers);
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
