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

export async function openSerial(params: OpenSerialParams): Promise<OpenSerialResponse> {
  return post<OpenSerialResponse>("/api/serial/open", params);
}

export async function closeSerial(): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>("/api/serial/close", {});
}

export async function sendSerial(text: string): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>("/api/serial/send", { text });
}

export async function listSerialPorts(): Promise<SerialPortInfo[]> {
  const result = await get<{ ports: SerialPortInfo[] }>("/api/serial/ports");
  return result.ports;
}

export function getSerialLogDownloadUrl(fileName: string): string {
  return `/api/serial/logs/${encodeURIComponent(fileName)}`;
}

/** Recent full snapshots for instant chart backfill on (re)connect. */
export async function getSnapshots(limit = 120): Promise<SnapshotPayload[]> {
  const result = await get<{ snapshots: SnapshotPayload[] }>(`/api/snapshots?limit=${limit}`);
  return result.snapshots;
}

/** Recent console lines so the Serial Console seeds instantly on (re)load. */
export async function getConsoleTail(limit = 500): Promise<string[]> {
  const result = await get<{ lines: string[] }>(`/api/console/tail?limit=${limit}`);
  return result.lines;
}

/** Switch the serial reader into raw interactive terminal mode (monitoring pauses). */
export async function enterTerminal(): Promise<void> {
  const response = await fetch("/api/serial/terminal/enter", { method: "POST" });
  if (!response.ok) {
    throw new Error((await response.json().catch(() => ({}))).detail || "Failed to enter terminal mode");
  }
}

/** Resume sysmon monitoring. */
export async function exitTerminal(): Promise<void> {
  await fetch("/api/serial/terminal/exit", { method: "POST" }).catch(() => undefined);
}
