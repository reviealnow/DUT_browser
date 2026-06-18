import { DEFAULT_DUT_ID } from "./dut";
import { SnapshotPayload } from "./websocket";

export type OpenSerialParams = {
  port: string;
  baudrate: number;
  mode?: "serial" | "replay";
  replay_path?: string;
  replay_interval_ms?: number;
};

export type OpenSerialResponse = {
  ok: boolean;
  mode: "serial" | "replay";
  log_path?: string | null;
};

export type SerialPortInfo = {
  device: string;
  description: string;
  hwid: string;
};

/**
 * Turn a backend error (thrown by `post`/`get` as `new Error(response.text())`,
 * whose message is usually a JSON body like `{"detail":"..."}`) into friendly,
 * user-facing copy. Never surfaces raw JSON.
 */
export function humanizeApiError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  let detail = raw;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.detail === "string") {
      detail = parsed.detail;
    }
  } catch {
    // Not JSON — keep the raw message as the fallback detail.
  }
  if (detail.includes("Serial port is not open")) {
    return "Not connected — select a serial port above and click Open first.";
  }
  if (!detail.trim()) {
    return "Something went wrong. Please try again.";
  }
  return detail;
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as T;
}

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as T;
}

export async function openSerial(
  params: OpenSerialParams,
  dutId = DEFAULT_DUT_ID,
): Promise<OpenSerialResponse> {
  return post<OpenSerialResponse>(`/api/serial/open?dut=${dutId}`, params);
}

export async function closeSerial(dutId = DEFAULT_DUT_ID): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(`/api/serial/close?dut=${dutId}`, {});
}

export async function sendSerial(text: string, dutId = DEFAULT_DUT_ID): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>(`/api/serial/send?dut=${dutId}`, { text });
}

export async function listSerialPorts(): Promise<SerialPortInfo[]> {
  const result = await get<{ ports: SerialPortInfo[] }>("/api/serial/ports");
  return result.ports;
}

export function getSerialLogDownloadUrl(fileName: string): string {
  return `/api/serial/logs/${encodeURIComponent(fileName)}`;
}

/** Recent full snapshots for instant chart backfill on (re)connect. */
export async function getSnapshots(limit = 120, dutId = DEFAULT_DUT_ID): Promise<SnapshotPayload[]> {
  const result = await get<{ snapshots: SnapshotPayload[] }>(
    `/api/snapshots?limit=${limit}&dut=${dutId}`,
  );
  return result.snapshots;
}

/** Recent console lines so the Serial Console seeds instantly on (re)load. */
export async function getConsoleTail(limit = 500, dutId = DEFAULT_DUT_ID): Promise<string[]> {
  const result = await get<{ lines: string[] }>(
    `/api/console/tail?limit=${limit}&dut=${dutId}`,
  );
  return result.lines;
}

export type MemoryPoint = {
  ts: string;
  memAvailableKb: number;
  slabKb: number;
  sunreclaimKb: number;
  effectiveKb: number;
};

export type MemorySeries = {
  available: boolean;
  generated_at?: string | null;
  version?: string | null;
  points: MemoryPoint[];
};

export type LogEntry = { name: string; size: number; mtime: string };
export type LogList = { sessions: LogEntry[]; artifacts: LogEntry[] };

/** List saved DUT session logs and analyzer artifacts (read-only browse). */
export async function getLogs(): Promise<LogList> {
  return get<LogList>("/api/logs");
}

export type LogTail = { name: string; lines: string[]; truncated: boolean };

/** Last `lines` lines of a session log, for an in-place peek in Downloads. */
export async function getLogTail(name: string, lines = 200): Promise<LogTail> {
  return get<LogTail>(`/api/logs/tail?name=${encodeURIComponent(name)}&lines=${lines}`);
}

/** Download URL for an analyzer artifact in logs/analyzer_output/. */
export function getAnalyzerDownloadUrl(fileName: string): string {
  return `/api/download/${encodeURIComponent(fileName)}`;
}

/** Inline image URL for an analyzer PNG plot (renders in an <img>, not a download). */
export function getAnalyzerPreviewUrl(fileName: string): string {
  return `/api/download/preview/${encodeURIComponent(fileName)}`;
}

/** Parsed memory series from the latest analyzer run (post-analysis only). */
export async function getMemory(limit = 500): Promise<MemorySeries> {
  return get<MemorySeries>(`/api/analyzer/memory?limit=${limit}`);
}

export type AnalyzeResult = { ok: boolean; files: string[] };

/**
 * Run the offline analyzer on a saved session log (by name) and publish its
 * CSV/PNG outputs to logs/analyzer_output/ (browsable in Downloads → Analyzer
 * outputs). Blocking — the analyzer runs synchronously, a few seconds.
 */
export async function analyzeSessionLog(name: string): Promise<AnalyzeResult> {
  return post<AnalyzeResult>("/api/analyzer/run-session", { name });
}

/** Switch the serial reader into raw interactive terminal mode (monitoring pauses). */
export async function enterTerminal(dutId = DEFAULT_DUT_ID): Promise<void> {
  const response = await fetch(`/api/serial/terminal/enter?dut=${dutId}`, { method: "POST" });
  if (!response.ok) {
    throw new Error((await response.json().catch(() => ({}))).detail || "Failed to enter terminal mode");
  }
}

/** Resume sysmon monitoring. */
export async function exitTerminal(dutId = DEFAULT_DUT_ID): Promise<void> {
  await fetch(`/api/serial/terminal/exit?dut=${dutId}`, { method: "POST" }).catch(() => undefined);
}

/**
 * Tell the DUT shell the terminal size (and optionally TERM) so vi/nano render
 * correctly. Best-effort — a failed resize must never break the terminal view.
 */
export async function resizeTerminal(
  rows: number,
  cols: number,
  term?: string,
  dutId = DEFAULT_DUT_ID,
): Promise<void> {
  await post(`/api/serial/terminal/resize?dut=${dutId}`, { rows, cols, term }).catch(() => undefined);
}

export type WifiClientRow = {
  iface: string;
  band: string;
  ssid?: string;
  mac: string;
  vendor: string;
  aid: number | null;
  channel: number | null;
  txrate: string | null;
  rxrate: string | null;
  rssi: number | null;
  signal_pct: number | null;
  snr: number | null;
  assoc_time: string | null;
  phymode: string | null;
  width: string | null;
  rxnss: number | null;
  txnss: number | null;
};

export type WifiVap = { iface: string; ssid: string; band: string; channel: number | null };
export type WifiClientsResult = { clients: WifiClientRow[]; vaps: WifiVap[]; captured_at: string };

/** On-demand scan of associated Wi-Fi clients (wlanconfig per active VAP). */
export async function getWifiClients(dutId = DEFAULT_DUT_ID): Promise<WifiClientsResult> {
  return get<WifiClientsResult>(`/api/wifi/clients?dut=${dutId}`);
}

/** Disassociate a Wi-Fi client (wlanconfig kickmac). */
export async function kickWifiClient(iface: string, mac: string, dutId = DEFAULT_DUT_ID): Promise<void> {
  await post(`/api/serial/wifi/kick?dut=${dutId}`, { iface, mac });
}

export type WifiClientStats = {
  tx_bytes: number | null;
  rx_bytes: number | null;
  avg_tx_kbps: number | null;
  avg_rx_kbps: number | null;
  tx_bytes_1s: number | null;
  rx_bytes_1s: number | null;
  band_width: number | null;
  rx_rssi: number | null;
  per: number | null;
  tx_nss: number | null;
  rx_nss: number | null;
};
export type WifiClientStatsResult = { mac: string; stats: WifiClientStats; captured_at: string };

/** On-demand deep stats for one client (apstats). One serial command per call. */
export async function getWifiClientStats(mac: string, dutId = DEFAULT_DUT_ID): Promise<WifiClientStatsResult> {
  return get<WifiClientStatsResult>(`/api/wifi/client-stats?dut=${dutId}&mac=${encodeURIComponent(mac)}`);
}

export type DutInfo = {
  id: string;
  label: string;
  mode: "serial" | "replay" | null;
  serial_open: boolean;
  log_path: string | null;
  removable: boolean;
};

/** List the registered DUTs (for the switcher). */
export async function getDuts(): Promise<DutInfo[]> {
  const result = await get<{ duts: DutInfo[] }>("/api/duts");
  return result.duts;
}

/** Register a new DUT at runtime. */
export async function addDut(id: string, label?: string): Promise<void> {
  await post("/api/duts", { id, label });
}

/** Remove a DUT (frees its serial port). The default DUT cannot be removed. */
export async function removeDut(id: string): Promise<void> {
  const response = await fetch(`/api/duts/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error((await response.json().catch(() => ({}))).detail || "Failed to remove DUT");
  }
}
