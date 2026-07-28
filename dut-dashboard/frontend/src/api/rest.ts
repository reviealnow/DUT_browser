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

/**
 * Fired on any 401 so AuthContext can re-check /api/auth/me. A 401 alone does
 * NOT mean the session died — guest-visible sections legitimately receive 401
 * from engineer-gated endpoints — so listeners must confirm against /me before
 * downgrading, and 403 never triggers anything (the session is fine, the role
 * is just too low).
 */
export const AUTH_UNAUTHORIZED_EVENT = "dut:auth-unauthorized";

async function fail(response: Response): Promise<never> {
  if (response.status === 401) {
    window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
  }
  throw new Error(await response.text());
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    await fail(response);
  }
  return (await response.json()) as T;
}

async function get<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    await fail(response);
  }
  return (await response.json()) as T;
}

async function put<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    await fail(response);
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
/** A session log row also reports how many context files its own time window covers. */
export type SessionLogEntry = LogEntry & { context_count: number };
/** A connect-time context capture; `kind` fixes which directory serves it. */
export type ContextEntry = LogEntry & { kind: string };
export type LogList = {
  sessions: SessionLogEntry[];
  artifacts: LogEntry[];
  surveys: LogEntry[];
  context: ContextEntry[];
};

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

/** Download URL for a persisted site-survey snapshot (json/csv) in logs/site-surveys/. */
export function getSurveyDownloadUrl(fileName: string): string {
  return `/api/download/survey/${encodeURIComponent(fileName)}`;
}

/** Download URL for a connect-time context capture (json/csv), keyed by its kind. */
export function getContextDownloadUrl(kind: string, fileName: string): string {
  return `/api/download/context/${encodeURIComponent(kind)}/${encodeURIComponent(fileName)}`;
}

export type ContextCapture = { kind: string; ok: boolean; error: string | null; files: string[] };
export type ContextCaptureResult = { dut: string; captured_at: string; captures: ContextCapture[] };

/**
 * Persist the DUT's Wi-Fi clients and SSID capability as connect-time context.
 * Fired once on connect, after the site-survey prescan (the captures share one
 * serial gate, so they are sequenced rather than raced). Each kind reports its
 * own outcome — a failure here must never surface as a failed connect.
 */
export async function captureDutContext(dutId = DEFAULT_DUT_ID): Promise<ContextCaptureResult> {
  return post<ContextCaptureResult>(`/api/wifi/context-capture?dut=${dutId}`, {});
}

/** Parsed memory series from the latest analyzer run (post-analysis only). */
export async function getMemory(limit = 500): Promise<MemorySeries> {
  return get<MemorySeries>(`/api/analyzer/memory?limit=${limit}`);
}

/** Where an Analyze put the session's context, if that session captured any. */
export type AnalyzeContext = { dir: string | null; files: string[] };
export type AnalyzeResult = { ok: boolean; files: string[]; context: AnalyzeContext };

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
  /** Last successful serial-open params, remembered for one-click Connect. */
  last_serial: { port: string; baudrate: number } | null;
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
  tags?: string[];
  /** False when the name is unverified client-supplied text (pre-P71d rows). */
  uploader_verified?: boolean;
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

export type FileSortKey = "date" | "name" | "size" | "uploader";
export type SortOrder = "asc" | "desc";

export type FilesQuery = {
  limit?: number;
  offset?: number;
  q?: string;
  sort?: FileSortKey;
  order?: SortOrder;
};

/** Query-string suffix from defined, non-empty params; empty when none given. */
function listQuery(params: Record<string, string | number | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(v as string | number)}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

/** List shared files plus KPI aggregates, in one round-trip. Omitting `limit`
 * returns everything. `q` filters by filename substring server-side; `total`
 * counts the matches while `stats` stays workspace-wide. */
export async function getFiles(opts: FilesQuery = {}): Promise<FilesList> {
  return get<FilesList>(`/api/files${listQuery(opts)}`);
}

/** Upload a file. `uploader` is the optional free-text display name;
 * `tags` is an optional tag-name list. */
export async function uploadFile(
  file: File,
  uploader?: string | null,
  tags?: string[],
): Promise<WorkspaceFile> {
  const form = new FormData();
  form.append("file", file);
  if (uploader) {
    form.append("uploader", uploader);
  }
  if (tags && tags.length) {
    form.append("tags", tags.join(","));
  }
  const response = await fetch("/api/files", { method: "POST", body: form });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as WorkspaceFile;
}

/** Replace the tag set of an existing file. Returns the stored tag names. */
export async function setFileTags(id: number, tags: string[]): Promise<string[]> {
  const result = await put<{ tags: string[] }>(`/api/files/${id}/tags`, { tags });
  return result.tags;
}

/** Direct download URL for a shared file (by id). */
export function getFileDownloadUrl(id: number): string {
  return `/api/files/${id}/download`;
}

/** Preview URL for a shared file — images stream with their real content type,
 * so this can be used directly as an <img> src. */
export function getFilePreviewUrl(id: number): string {
  return `/api/files/${id}/preview`;
}

export type TextPreview = { content: string; truncated: boolean };

/** First chunk of a text file (log/txt/csv/json) for the row-expand preview. */
export async function getFileTextPreview(id: number): Promise<TextPreview> {
  const response = await fetch(getFilePreviewUrl(id));
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return {
    content: await response.text(),
    truncated: response.headers.get("X-Preview-Truncated") === "1",
  };
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
  edited_at: string | null;
  author_verified?: boolean;
  replies: BulletinComment[];
};

export type BulletinPost = {
  id: number;
  title: string;
  body: string;
  author: string | null;
  created_at: string;
  edited_at: string | null;
  author_verified?: boolean;
  comments: BulletinComment[];
  tags?: string[];
};

export type BulletinPostsPage = { posts: BulletinPost[]; total: number };

export type BulletinQuery = { limit?: number; offset?: number; q?: string };

/** List bulletin posts (newest first) with nested comments. Omitting `limit`
 * returns everything. `q` filters by title/body substring server-side;
 * `total` counts the matches. */
export async function getBulletinPosts(opts: BulletinQuery = {}): Promise<BulletinPostsPage> {
  return get<BulletinPostsPage>(`/api/bulletin/posts${listQuery(opts)}`);
}

/** Create a bulletin post. `author` is the optional free-text display name. */
export async function createBulletinPost(
  title: string,
  body: string,
  author?: string | null,
  tags?: string[],
): Promise<{ id: number }> {
  return post<{ id: number }>("/api/bulletin/posts", { title, body, author, tags });
}

/** Edit a bulletin post's title/body. `tags` undefined leaves tags unchanged;
 * an array (even empty) replaces them. No owner check — shared-trust model. */
export async function updateBulletinPost(
  id: number,
  title: string,
  body: string,
  tags?: string[],
): Promise<void> {
  await put<{ ok: boolean }>(`/api/bulletin/posts/${id}`, { title, body, tags });
}

/** Replace the tag set of an existing post. Returns the stored tag names. */
export async function setPostTags(id: number, tags: string[]): Promise<string[]> {
  const result = await put<{ tags: string[] }>(`/api/bulletin/posts/${id}/tags`, { tags });
  return result.tags;
}

// ---------------------------------------------------------------------------
// Workspace: shared tags + fuzzy tag search across files and bulletin posts
// ---------------------------------------------------------------------------

export type WorkspaceTag = { name: string; file_count: number; post_count: number };

export type WorkspaceSearchResult = {
  query: string;
  matched_tags: { name: string; score: number }[];
  files: WorkspaceFile[];
  posts: (Omit<BulletinPost, "comments"> & { comments?: BulletinComment[] })[];
};

/** All tags with usage counts (feeds the tag-input suggestion datalist). */
export async function getWorkspaceTags(): Promise<WorkspaceTag[]> {
  const result = await get<{ tags: WorkspaceTag[] }>("/api/workspace/tags");
  return result.tags;
}

/** Fuzzy tag search over files and bulletin posts ("ui" matches "usage_insight"). */
export async function searchWorkspace(q: string): Promise<WorkspaceSearchResult> {
  return get<WorkspaceSearchResult>(`/api/workspace/search${listQuery({ q })}`);
}

/** Edit a comment or nested reply. No owner check — shared-trust model. */
export async function updateBulletinComment(id: number, body: string): Promise<void> {
  await put<{ ok: boolean }>(`/api/bulletin/comments/${id}`, { body });
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

// --- Auth (P71b) -----------------------------------------------------------

export type Role = "guest" | "engineer" | "admin";

export type AuthUser = {
  username: string;
  display_name: string;
  role: Role;
};

export type RegisterParams = {
  username: string;
  display_name?: string;
  role: Role;
  passcode?: string;
};

/**
 * The current session, or null when there is none. An anonymous browser is a
 * normal state (it browses as guest), so "no session" is a value here — not an
 * error — and this deliberately bypasses `get()` so a plain 401 does not fire
 * the unauthorized event at every anonymous page load.
 */
export async function getMe(): Promise<AuthUser | null> {
  const response = await fetch("/api/auth/me");
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as AuthUser;
}

/** Register (or re-register to change role); the session cookie rides the response. */
export async function registerAuth(params: RegisterParams): Promise<AuthUser> {
  return post<AuthUser>("/api/auth/register", params);
}

export async function logoutAuth(): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>("/api/auth/logout", {});
}

// --- Invite links (P71c) ---------------------------------------------------

export type InviteParams = {
  role: Role;
  label?: string;
  /** Hours until expiry; null means the invite never expires. */
  expires_in_hours?: number | null;
  max_uses?: number;
};

/** The create response — the ONLY place `token`/`url_path`/`qr_svg` ever exist. */
export type CreatedInvite = {
  id: number;
  role: Role;
  label: string | null;
  token: string;
  url_path: string;
  qr_svg: string | null;
  expires_at: string | null;
  max_uses: number;
};

/** List shape: no token, no hash — an issued invite can never be re-read. */
export type InviteSummary = {
  id: number;
  role: Role;
  label: string | null;
  created_by: string | null;
  created_at: string;
  expires_at: string | null;
  max_uses: number;
  used_count: number;
  revoked: boolean;
  exhausted: boolean;
};

// --- Audit (P71d) ----------------------------------------------------------

export type UserRecord = {
  id: number;
  username: string;
  display_name: string | null;
  role: Role;
  created_at: string;
  updated_at: string | null;
  last_seen_at: string | null;
};

export type RoleChange = {
  id: number;
  username: string;
  from_role: Role | null;
  to_role: Role;
  via: "register" | "invite" | "cli";
  invite_id: number | null;
  changed_at: string;
};

export async function listUsers(): Promise<UserRecord[]> {
  const result = await get<{ users: UserRecord[] }>("/api/auth/users");
  return result.users;
}

export async function listRoleChanges(limit = 100): Promise<RoleChange[]> {
  const result = await get<{ changes: RoleChange[] }>(`/api/auth/role-changes?limit=${limit}`);
  return result.changes;
}

export async function createInvite(params: InviteParams): Promise<CreatedInvite> {
  return post<CreatedInvite>("/api/auth/invites", params);
}

export async function listInvites(): Promise<InviteSummary[]> {
  const result = await get<{ invites: InviteSummary[] }>("/api/auth/invites");
  return result.invites;
}

export async function revokeInvite(id: number): Promise<void> {
  const response = await fetch(`/api/auth/invites/${id}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
}

/** Trade an invite token for a session. The role comes from the invite, so it
 * is only known once this resolves — there is no way to inspect one first. */
export async function redeemInvite(
  token: string,
  username: string,
  displayName?: string,
): Promise<AuthUser> {
  return post<AuthUser>("/api/auth/redeem", {
    token,
    username,
    display_name: displayName,
  });
}
