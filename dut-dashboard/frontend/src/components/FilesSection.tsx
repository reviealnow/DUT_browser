import { useCallback, useEffect, useRef, useState } from "react";

import {
  deleteFile,
  getFileDownloadUrl,
  getFiles,
  FilesList,
  FilesStats,
  humanizeApiError,
  uploadFile,
  WorkspaceFile,
} from "../api/rest";
import { hashHue } from "../monitoring/authorColor";
import { useIdentity } from "../monitoring/useSettings";
import AuthorTag from "./AuthorTag";
import ChartData from "./charts/ChartData";
import Sparkline from "./charts/Sparkline";
import { Card, EmptyState, KpiCard } from "./shell/Card";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Known extensions get a branded chip colour (see .t-* in dashboard.css);
// anything else gets a stable hashed colour (see FileTypeChip).
const FTYPE_CLASS: Record<string, string> = {
  pdf: "t-pdf",
  pcap: "t-pcapng",
  pcapng: "t-pcapng",
  png: "t-png",
  jpg: "t-png",
  jpeg: "t-png",
  gif: "t-png",
  csv: "t-csv",
  log: "t-log",
  txt: "t-log",
  json: "t-csv",
};

function extOf(name: string): string {
  return name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
}

function FileTypeChip({ name }: { name: string }) {
  const ext = extOf(name);
  const label = (ext || "?").slice(0, 3).toUpperCase();
  const cls = FTYPE_CLASS[ext];
  // Unknown extensions hash to a stable hue (same djb2 trick as author tags);
  // lightness is fixed low enough that the chip's white text stays readable.
  // Extension-less files keep the neutral .ftype background.
  const style = !cls && ext ? { background: `hsl(${hashHue(ext)} 52% 42%)` } : undefined;
  return (
    <span className={`ftype ${cls ?? ""}`} style={style}>
      {label}
    </span>
  );
}

// Shared files accumulate like session logs; cap the visible height to ~5 rows
// and scroll for older. All rows stay in the DOM so search/filter still works.
const VISIBLE_FILE_ROWS = 5;

function SharedFilesTable({ rows, onDelete }: { rows: WorkspaceFile[]; onDelete: (file: WorkspaceFile) => void }) {
  return (
    <>
      {rows.length > VISIBLE_FILE_ROWS ? (
        <div className="logscroll-note">Showing newest first — scroll for older ({rows.length} loaded).</div>
      ) : null}
      <div className="logscroll">
        <table className="filetable">
          <thead>
            <tr>
              <th>Filename</th>
              <th>Size</th>
              <th>Uploader</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td className="filetable-name">
                  <FileTypeChip name={row.filename} />
                  {row.filename}
                </td>
                <td>{formatSize(row.size)}</td>
                <td><AuthorTag name={row.uploader} /></td>
                <td>
                  <div className="row-actions">
                    <a
                      className="icon-btn"
                      href={getFileDownloadUrl(row.id)}
                      download={row.filename}
                      title="Download"
                      aria-label={`Download ${row.filename}`}
                    >
                      ↓
                    </a>
                    <button
                      type="button"
                      className="icon-btn danger"
                      title="Delete"
                      aria-label={`Delete ${row.filename}`}
                      onClick={() => onDelete(row)}
                    >
                      🗑
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  // There's no login, so the uploader is the free-text display name. It defaults
  // to an IP-derived suggestion (useIdentity), is editable right here at upload
  // time, persisted, and shared with Bulletin + Settings; a name is required.
  const { displayName, suggested, effectiveName, setDisplayName } = useIdentity();

  const send = useCallback(
    async (file: File) => {
      if (!effectiveName) {
        setError("Enter your name first.");
        return;
      }
      setBusy(true);
      setError(null);
      try {
        await uploadFile(file, effectiveName);
        onUploaded();
      } catch (e) {
        setError(humanizeApiError(e));
      } finally {
        setBusy(false);
      }
    },
    [onUploaded, effectiveName],
  );

  return (
    <Card title="Upload" subtitle="Max 50 MB · pdf, png, csv, log, pcapng…">
      <label className="upload-name">
        <span>Your name</span>
        <input
          type="text"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder={suggested || "e.g. nelson — tags the uploader"}
          aria-label="Your name (uploader)"
          maxLength={40}
        />
        {effectiveName ? <AuthorTag name={effectiveName} /> : null}
      </label>
      <div
        className={`upload-drop${dragging ? " dragging" : ""}`}
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const file = e.dataTransfer.files?.[0];
          if (file) void send(file);
        }}
      >
        <div className="upload-drop-icon" aria-hidden="true">
          ⬆
        </div>
        <div className="upload-drop-main">
          {busy ? "Uploading…" : "Drag a file here, or click to browse"}
        </div>
        <div className="upload-drop-hint">Max 50 MB · pdf, png, csv, log, pcapng…</div>
      </div>
      <input
        ref={inputRef}
        type="file"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void send(file);
          e.target.value = "";
        }}
      />
      {error ? (
        <div className="flash" style={{ marginTop: "var(--space-3)", color: "var(--danger)" }}>
          {error}
        </div>
      ) : null}
    </Card>
  );
}

function UploadsTrendBody({ stats }: { stats: FilesStats }) {
  const values = stats.uploads_per_day.map((d) => d.count);
  const total = values.reduce((sum, c) => sum + c, 0);
  // Counts are small integers; normalise to the series' own peak so the trend is
  // visible (Sparkline plots 0..max — the default 100 would flatten it).
  const max = Math.max(1, ...values);
  return (
    <div className="chart">
      <div className="chart-figure">
        <Sparkline values={values} max={max} ariaLabel="Uploads per day, last 14 days" />
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {total}
          <span className="unit">uploads · 14d</span>
        </div>
      </div>
      <ChartData id="uploads-trend-data" data={stats.uploads_per_day} />
    </div>
  );
}

function FilesByTypeBody({ stats }: { stats: FilesStats }) {
  const rows = stats.files_by_type;
  if (rows.length === 0) {
    return <EmptyState icon="🗂" message="No files yet" hint="Upload a file to see a type breakdown." />;
  }
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <div className="chart">
      <div className="barrows">
        {rows.map((row) => (
          <div className="barrow" key={row.ext}>
            <div className="barrow-label">{(row.ext || "?").toUpperCase()}</div>
            <div className="barrow-track">
              <div className="barrow-fill" style={{ width: `${(row.count / max) * 100}%` }} />
            </div>
            <div className="barrow-value">{row.count}</div>
          </div>
        ))}
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {rows.length}
          <span className="unit">type{rows.length === 1 ? "" : "s"}</span>
        </div>
      </div>
      <ChartData id="files-by-type-data" data={rows} />
    </div>
  );
}

function TopUploadersBody({ stats }: { stats: FilesStats }) {
  // Drop the anonymous "—" bucket: it's not a contributor, and when most uploads
  // are anonymous it dwarfs every named bar to invisibility. Named-only also
  // matches the "Contributors" KPI (backend excludes NULL uploaders) and the
  // foot count below.
  const rows = stats.top_uploaders.filter((r) => r.uploader !== "—");
  if (rows.length === 0) {
    return <EmptyState icon="👤" message="No named uploaders yet" hint="Set your name on upload to be credited here." />;
  }
  const max = Math.max(1, ...rows.map((r) => r.count));
  const named = rows.length;
  return (
    <div className="chart">
      <div className="barrows">
        {rows.map((row) => (
          <div className="barrow" key={row.uploader}>
            <div className="barrow-label"><AuthorTag name={row.uploader} /></div>
            <div className="barrow-track">
              <div className="barrow-fill" style={{ width: `${(row.count / max) * 100}%` }} />
            </div>
            <div className="barrow-value">{row.count}</div>
          </div>
        ))}
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {named}
          <span className="unit">contributor{named === 1 ? "" : "s"}</span>
        </div>
      </div>
      <ChartData id="top-uploaders-data" data={rows} />
    </div>
  );
}

// First page size; "Load more" appends another page (lists accumulate over
// months, so the view lazy-loads instead of fetching every row up front).
const PAGE_SIZE = 50;

export default function FilesSection({ query = "" }: { query?: string }) {
  const [data, setData] = useState<FilesList | null>(null);
  const [failed, setFailed] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Rows currently on screen: reload refetches that many from offset 0 so an
  // upload/delete refresh doesn't collapse the list back to the first page.
  const loadedRef = useRef(PAGE_SIZE);

  const reload = useCallback(() => {
    setFailed(false);
    getFiles(Math.max(PAGE_SIZE, loadedRef.current), 0)
      .then((next) => {
        loadedRef.current = next.files.length;
        setData(next);
      })
      .catch(() => setFailed(true));
  }, []);

  const loadMore = useCallback(() => {
    if (!data) return;
    setLoadingMore(true);
    getFiles(PAGE_SIZE, data.files.length)
      .then((next) => {
        setData((prev) => {
          if (!prev) return next;
          // Dedupe on id: an upload landing between page fetches shifts offsets.
          const known = new Set(prev.files.map((f) => f.id));
          const files = [...prev.files, ...next.files.filter((f) => !known.has(f.id))];
          loadedRef.current = files.length;
          return { files, stats: next.stats, total: next.total };
        });
      })
      .catch(() => undefined) // keep the loaded rows; the button stays for retry
      .finally(() => setLoadingMore(false));
  }, [data]);

  useEffect(() => {
    reload();
  }, [reload]);

  const onDelete = useCallback(
    (file: WorkspaceFile) => {
      if (!window.confirm(`Delete "${file.filename}"? This cannot be undone.`)) {
        return;
      }
      deleteFile(file.id)
        .then(reload)
        .catch(() => reload());
    },
    [reload],
  );

  if (failed) {
    return (
      <Card title="Shared Files" subtitle="Shared file workspace">
        <EmptyState icon="🗂" message="Could not load files" hint="Is the backend reachable?" />
      </Card>
    );
  }
  if (!data) {
    return (
      <Card title="Shared Files" subtitle="Shared file workspace">
        <EmptyState icon="🗂" message="Loading…" />
      </Card>
    );
  }

  const { stats } = data;
  const needle = query.trim().toLowerCase();
  const files = needle
    ? data.files.filter((f) => f.filename.toLowerCase().includes(needle))
    : data.files;

  return (
    <>
      <div className="kpis">
        <KpiCard label="Total Files" value={String(stats.total)} sub="in workspace" />
        <KpiCard label="Storage Used" value={formatSize(stats.total_size)} sub={`across ${stats.total} files`} />
        <KpiCard label="Contributors" value={String(stats.contributors)} sub="uploaders" />
        <KpiCard label="This Week" value={String(stats.this_week)} sub="new uploads" />
      </div>

      {/* Shared Files + Upload sit directly under the KPIs so uploading and
          downloading are the first thing the user reaches; the stats charts
          (analytics) follow below. */}
      <div className="grid">
        <Card
          title="Shared Files"
          subtitle={
            data.files.length < data.total
              ? `${data.files.length} of ${data.total} files · newest first`
              : `${data.total} files · newest first`
          }
        >
          {files.length > 0 ? (
            <SharedFilesTable rows={files} onDelete={onDelete} />
          ) : (
            <EmptyState
              icon="🗂"
              message={needle ? "No matching files" : "No files yet"}
              hint={
                needle
                  ? data.files.length < data.total
                    ? "No match in the loaded rows — try Load more or a different filter."
                    : "Try a different filter."
                  : "Upload a file to get started."
              }
            />
          )}
          {data.files.length < data.total ? (
            <div style={{ marginTop: "var(--space-3)" }}>
              <button type="button" className="btn" disabled={loadingMore} onClick={loadMore}>
                {loadingMore ? "Loading…" : `Load more (${data.total - data.files.length} older)`}
              </button>
            </div>
          ) : null}
        </Card>
        <UploadCard onUploaded={reload} />
      </div>

      <div className="grid">
        <Card className="col-span-2" title="Uploads (14 days)" subtitle="Daily upload activity">
          <UploadsTrendBody stats={stats} />
        </Card>
        <Card title="By file type" subtitle="Files grouped by extension">
          <FilesByTypeBody stats={stats} />
        </Card>
        <Card title="Top uploaders" subtitle="Contributors by file count">
          <TopUploadersBody stats={stats} />
        </Card>
      </div>
    </>
  );
}
