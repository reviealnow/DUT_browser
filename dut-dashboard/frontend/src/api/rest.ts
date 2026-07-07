import { DEFAULT_DUT_ID } from "./dut";
import { SnapshotPayload } from "./websocket";

export type OpenSerialParams = {
  port: string;
  baudrate: number;
  mode?: "serial" | "replay";
  replay_path?: string;
  replay_interval_ms?: number;
  /** Free-text DUT label woven into the session-log filename (sanitized backend-side). */
  session_label?: string;
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

// ---------------------------------------------------------------------------
// Workspace: shared files (LAN File Server, shared-trust model — no auth)
// ---------------------------------------------------------------------------

export type WorkspaceFile = {
  id: number;
  filename: string;
  size: number;
  uploader: string | null;
  uploaded_at: string;
};

export type FilesStats = {
  total: number;
  total_size: number;
  contributors: number;
  this_week: number;
  uploads_per_day: { date: string; label: string; count: number }[];
  files_by_type: { ext: string; count: number; size: number }[];
  top_uploaders: { uploader: string; count: number }[];
};

export type FilesList = { files: WorkspaceFile[]; stats: FilesStats; total: number };

/** Query-string suffix for paginated list endpoints; empty when no limit given. */
function pageQuery(limit?: number, offset?: number): string {
  return limit == null ? "" : `?limit=${limit}&offset=${offset ?? 0}`;
}

/** List shared files (newest first) plus KPI aggregates, in one round-trip.
 * Omitting `limit` returns everything; `total` always counts every file. */
export async function getFiles(limit?: number, offset?: number): Promise<FilesList> {
  return get<FilesList>(`/api/files${pageQuery(limit, offset)}`);
}

/** Upload a file. `uploader` is the optional free-text display name. */
export async function uploadFile(file: File, uploader?: string | null): Promise<WorkspaceFile> {
  const form = new FormData();
  form.append("file", file);
  if (uploader) {
    form.append("uploader", uploader);
  }
  const response = await fetch("/api/files", { method: "POST", body: form });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as WorkspaceFile;
}

/** Direct download URL for a shared file (by id). */
export function getFileDownloadUrl(id: number): string {
  return `/api/files/${id}/download`;
}

/** Delete a shared file (no owner check — shared-trust model). */
export async function deleteFile(id: number): Promise<void> {
  const response = await fetch(`/api/files/${id}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

// ---------------------------------------------------------------------------
// Workspace: bulletin board (posts with one level of nested replies)
// ---------------------------------------------------------------------------

export type BulletinComment = {
  id: number;
  post_id: number;
  parent_comment_id: number | null;
  body: string;
  author: string | null;
  created_at: string;
  replies: BulletinComment[];
};

export type BulletinPost = {
  id: number;
  title: string;
  body: string;
  author: string | null;
  created_at: string;
  comments: BulletinComment[];
};

export type BulletinPostsPage = { posts: BulletinPost[]; total: number };

/** List bulletin posts (newest first) with nested comments.
 * Omitting `limit` returns everything; `total` always counts every post. */
export async function getBulletinPosts(limit?: number, offset?: number): Promise<BulletinPostsPage> {
  return get<BulletinPostsPage>(`/api/bulletin/posts${pageQuery(limit, offset)}`);
}

/** Create a bulletin post. `author` is the optional free-text display name. */
export async function createBulletinPost(
  title: string,
  body: string,
  author?: string | null,
): Promise<{ id: number }> {
  return post<{ id: number }>("/api/bulletin/posts", { title, body, author });
}

/** Delete a bulletin post (its comments cascade). No owner check — shared-trust model. */
export async function deleteBulletinPost(id: number): Promise<void> {
  const response = await fetch(`/api/bulletin/posts/${id}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

/** Add a comment (or threaded reply) to a post. */
export async function createBulletinComment(
  postId: number,
  body: string,
  author?: string | null,
  parentCommentId?: number | null,
): Promise<{ id: number }> {
  return post<{ id: number }>(`/api/bulletin/posts/${postId}/comments`, {
    body,
    author,
    parent_comment_id: parentCommentId ?? null,
  });
}

export type VersionInfo = { version: string; built_at: string };

/** Current backend build version, used to detect a redeploy from an open tab. */
export async function getVersion(): Promise<VersionInfo> {
  return get<VersionInfo>("/api/version");
}

export type WhoAmI = { ip: string; name: string };

/** The caller's IP + a suggested display name, used to pre-fill Workspace identity. */
export async function getWhoami(): Promise<WhoAmI> {
  return get<WhoAmI>("/api/whoami");
}

export type CrashKeywordsResponse = { keywords: string[] };

export async function getCrashKeywords(): Promise<string[]> {
  const r = await get<CrashKeywordsResponse>("/api/settings/crash-keywords");
  return r.keywords;
}

export async function putCrashKeywords(keywords: string[]): Promise<string[]> {
  const r = await fetch("/api/settings/crash-keywords", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keywords }),
  });
  if (!r.ok) throw new Error(await r.text());
  const data: CrashKeywordsResponse = await r.json();
  return data.keywords;
}

// ---------------------------------------------------------------------------
// SSID Capability
// ---------------------------------------------------------------------------

export type SsidCapability = {
  iface: string;
  bssid: string | null;
  ssid: string | null;
  band: string | null;
  freq_mhz: number | null;
  channel: number | null;
  channel_width: string | null;
  generation: string | null;
  security: string | null;
  category: string | null;
  akm: string[];
  pairwise_cipher: string[];
  group_mgmt_cipher: string | null;
  pmf: string | null;
  dot11k: boolean | null;
  dot11v: boolean | null;
  dot11r: boolean | null;
};

export type SsidCapabilityResult = {
  ssids: SsidCapability[];
  captured_at: string;
};

export type CapabilityDiff = {
  field: string;
  label: string;
  config: unknown;
  observed: unknown;
};

export type CapabilityRow = {
  iface: string;
  bssid: string | null;
  ssid: string | null;
  band: string | null;
  freq_mhz: number | null;
  channel: number | null;
  channel_width: string | null;
  config_generation: string | null;
  config_security: string | null;
  config_pmf: string | null;
  config_dot11k: boolean | null;
  config_dot11v: boolean | null;
  config_dot11r: boolean | null;
  observed_generation: string | null;
  observed_security: string | null;
  observed_pmf: string | null;
  observed_dot11k: boolean | null;
  observed_dot11v: boolean | null;
  observed_dot11r: boolean | null;
  observed_signal_dbm: number | null;
  match: boolean;
  diffs: CapabilityDiff[];
  caveat: string | null;
};

export type CapabilityReport = {
  available_b: boolean;
  scannable_bands: string[];
  captured_at_a: string;
  captured_at_b: string | null;
  rows: CapabilityRow[];
};

export async function getSsidCapabilities(dutId = DEFAULT_DUT_ID): Promise<SsidCapabilityResult> {
  return get<SsidCapabilityResult>(`/api/wifi/capabilities?dut=${dutId}`);
}

export async function getWifiSurvey(): Promise<{ available: boolean; bss: unknown[]; reason?: string }> {
  return get<{ available: boolean; bss: unknown[]; reason?: string }>("/api/wifi/survey");
}

export async function getCapabilityReport(dutId = DEFAULT_DUT_ID): Promise<CapabilityReport> {
  return get<CapabilityReport>(`/api/wifi/capability-report?dut=${dutId}`);
}

// ---------------------------------------------------------------------------
// Site Survey / Channel Recommendation
// ---------------------------------------------------------------------------

export type SurveyVap = { iface: string; ssid: string; band: string; channel: number | null; mode: string | null };

export type ObservedNeighbor = {
  iface: string;
  bssid: string;
  ssid: string | null;
  band: string | null;
  freq_mhz: number | null;
  channel: number | null;
  signal_dbm: number | null;
  generation: string | null;
  security: string | null;
  category: string | null;
  pmf: string | null;
};

export type SiteSurveyResult = {
  vaps: SurveyVap[];
  neighbors: ObservedNeighbor[];
  captured_at: string;
};

export type ChannelRecommendation = {
  band: string;
  iface: string;
  current_channel: number;
  recommended_channel: number;
  score: number;
  occupancy: Record<string, number>;
  reasoning: string;
  caveat: string | null;
};

export type ChannelRecommendationResult = {
  recommendations: ChannelRecommendation[];
  neighbors: ObservedNeighbor[];
  survey_vaps: SurveyVap[];
  captured_at: string;
};

export async function getSiteSurvey(dutId = DEFAULT_DUT_ID): Promise<SiteSurveyResult> {
  return get<SiteSurveyResult>(`/api/wifi/site-survey?dut=${dutId}`);
}

export async function getChannelRecommendation(dutId = DEFAULT_DUT_ID): Promise<ChannelRecommendationResult> {
  return get<ChannelRecommendationResult>(`/api/wifi/channel-recommendation?dut=${dutId}`);
}

// Read-only cached last recommendation — no scan, no serial gate. Populated by
// getChannelRecommendation (the connect-time prescan or a manual Re-scan). Used
// by the Overview mini-card and Fleet grid; `cached: false` means never surveyed.
export type LastChannelRecommendationResult = {
  recommendations: ChannelRecommendation[];
  captured_at: string | null;
  cached: boolean;
};

export async function getLastChannelRecommendation(
  dutId = DEFAULT_DUT_ID,
): Promise<LastChannelRecommendationResult> {
  return get<LastChannelRecommendationResult>(`/api/wifi/channel-recommendation/last?dut=${dutId}`);
}
