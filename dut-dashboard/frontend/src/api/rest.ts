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

/** Download URL for an analyzer artifact in logs/analyzer_output/. */
export function getAnalyzerDownloadUrl(fileName: string): string {
  return `/api/download/${encodeURIComponent(fileName)}`;
}

/** Parsed memory series from the latest analyzer run (post-analysis only). */
export async function getMemory(limit = 500): Promise<MemorySeries> {
  return get<MemorySeries>(`/api/analyzer/memory?limit=${limit}`);
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
