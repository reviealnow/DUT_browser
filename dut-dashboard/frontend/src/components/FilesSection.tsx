import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import {
  deleteFile,
  FileSortKey,
  getFileDownloadUrl,
  getFilePreviewUrl,
  getFiles,
  getFileTextPreview,
  FilesList,
  FilesStats,
  humanizeApiError,
  setFileTags,
  SortOrder,
  TextPreview,
  uploadFile,
  WorkspaceFile,
} from "../api/rest";
import { hashHue } from "../monitoring/authorColor";
import { useIdentity } from "../monitoring/useSettings";
import AuthorTag from "./AuthorTag";
import { TagList } from "./TagChip";
import TagInput, { parseTags } from "./TagInput";
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

// Row-expand preview: images render inline, text files show their head
// (fetched lazily on expand); everything else only offers download.
const PREVIEW_IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif"]);
const PREVIEW_TEXT_EXTS = new Set(["log", "txt", "csv", "json"]);

function previewKind(name: string): "image" | "text" | null {
  const ext = extOf(name);
  if (PREVIEW_IMAGE_EXTS.has(ext)) return "image";
  if (PREVIEW_TEXT_EXTS.has(ext)) return "text";
  return null;
}

function TextPreviewBody({ file }: { file: WorkspaceFile }) {
  const [preview, setPreview] = useState<TextPreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getFileTextPreview(file.id)
      .then((p) => !cancelled && setPreview(p))
      .catch(() => !cancelled && setError("Could not load preview."));
    return () => {
      cancelled = true;
    };
  }, [file.id]);

  if (error) return <div className="preview-note">{error}</div>;
  if (preview === null) return <div className="preview-note">Loading preview…</div>;
  return (
    <>
      <pre className="preview-text">{preview.content}</pre>
      {preview.truncated ? (
        <div className="preview-note">Preview truncated — download for the full file.</div>
      ) : null}
    </>
  );
}

function FilePreviewRow({ file }: { file: WorkspaceFile }) {
  const kind = previewKind(file.filename);
  return (
    <tr className="preview-row">
      <td colSpan={4}>
        {kind === "image" ? (
          <img className="preview-image" src={getFilePreviewUrl(file.id)} alt={file.filename} />
        ) : kind === "text" ? (
          <TextPreviewBody file={file} />
        ) : (
          <div className="preview-note">No inline preview for this type — use download.</div>
        )}
      </td>
    </tr>
  );
}

// Default ordering (no header highlighted): newest upload first.
const DEFAULT_SORT: FileSortKey = "date";
const DEFAULT_ORDER: SortOrder = "desc";

function SortHeader({
  label,
  col,
  sort,
  order,
  onSort,
}: {
  label: string;
  col: FileSortKey;
  sort: FileSortKey;
  order: SortOrder;
  onSort: (col: FileSortKey) => void;
}) {
  const active = sort === col;
  return (
    <th aria-sort={active ? (order === "asc" ? "ascending" : "descending") : undefined}>
      <button type="button" className="th-sort" onClick={() => onSort(col)}>
        {label}
        {active ? <span aria-hidden="true">{order === "asc" ? "▲" : "▼"}</span> : null}
      </button>
    </th>
  );
}

/** Inline tag editor rendered as an extra row (same idiom as the preview row). */
function FileTagsEditRow({ file, onSaved, onCancel }: { file: WorkspaceFile; onSaved: () => void; onCancel: () => void }) {
  const [text, setText] = useState((file.tags ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = () => {
    setBusy(true);
    setError(null);
    setFileTags(file.id, parseTags(text))
      .then(onSaved)
      .catch((e) => {
        setError(humanizeApiError(e));
        setBusy(false);
      });
  };

  return (
    <tr className="tag-edit-row">
      <td colSpan={4}>
        <div className="tag-edit-form">
          <TagInput value={text} onChange={setText} ariaLabel={`Tags for ${file.filename}`} />
          <button type="button" className="btn" disabled={busy} onClick={save}>
            {busy ? "…" : "Save"}
          </button>
          <button type="button" className="btn" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          {error ? <span className="flash" style={{ color: "var(--danger)" }}>{error}</span> : null}
        </div>
      </td>
    </tr>
  );
}

function SharedFilesTable({
  rows,
  onDelete,
  sort,
  order,
  onSort,
  expandedId,
  onToggleExpand,
  onTagClick,
  tagEditId,
  onToggleTagEdit,
  onTagsSaved,
}: {
  rows: WorkspaceFile[];
  onDelete: (file: WorkspaceFile) => void;
  sort: FileSortKey;
  order: SortOrder;
  onSort: (col: FileSortKey) => void;
  expandedId: number | null;
  onToggleExpand: (id: number) => void;
  onTagClick?: (tag: string) => void;
  tagEditId: number | null;
  onToggleTagEdit: (id: number | null) => void;
  onTagsSaved: () => void;
}) {
  return (
    <>
      {rows.length > VISIBLE_FILE_ROWS ? (
        <div className="logscroll-note">Scroll for more ({rows.length} loaded).</div>
      ) : null}
      <div className="logscroll">
        <table className="filetable">
          <thead>
            <tr>
              <SortHeader label="Filename" col="name" sort={sort} order={order} onSort={onSort} />
              <SortHeader label="Size" col="size" sort={sort} order={order} onSort={onSort} />
              <SortHeader label="Uploader" col="uploader" sort={sort} order={order} onSort={onSort} />
              <th>SHA-256</th>
              <th aria-label="actions" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <Fragment key={row.id}>
                <tr>
                  <td className="filetable-name">
                    <FileTypeChip name={row.filename} />
                    {row.filename}
                    <TagList tags={row.tags} onTagClick={onTagClick} />
                  </td>
                  <td>{formatSize(row.size)}</td>
                  <td><AuthorTag name={row.uploader} verified={row.uploader_verified} /></td>
                  <td className="file-sha" title={row.sha256 ?? "no checksum recorded"}>{row.sha256 ? row.sha256.slice(0, 12) : "—"}</td>
                  <td className="filetable-actions">
                    <div className="row-actions">
                      <button
                        type="button"
                        className="icon-btn"
                        title={tagEditId === row.id ? "Close tag editor" : "Edit tags"}
                        aria-label={`Edit tags of ${row.filename}`}
                        aria-expanded={tagEditId === row.id}
                        onClick={() => onToggleTagEdit(tagEditId === row.id ? null : row.id)}
                      >
                        🏷
                      </button>
                      {previewKind(row.filename) ? (
                        <button
                          type="button"
                          className="icon-btn"
                          title={expandedId === row.id ? "Hide preview" : "Preview"}
                          aria-label={`${expandedId === row.id ? "Hide preview of" : "Preview"} ${row.filename}`}
                          aria-expanded={expandedId === row.id}
                          onClick={() => onToggleExpand(row.id)}
                        >
                          {expandedId === row.id ? "▴" : "👁"}
                        </button>
                      ) : null}
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
                {expandedId === row.id ? <FilePreviewRow file={row} /> : null}
                {tagEditId === row.id ? (
                  <FileTagsEditRow
                    file={row}
                    onSaved={() => {
                      onToggleTagEdit(null);
                      onTagsSaved();
                    }}
                    onCancel={() => onToggleTagEdit(null)}
                  />
                ) : null}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

type UploadResult = { name: string; error: string | null };

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [results, setResults] = useState<UploadResult[]>([]);
  const [dragging, setDragging] = useState(false);
  // Optional comma-separated tags, applied to every file in the batch.
  const [tagsText, setTagsText] = useState("");
  // There's no login, so the uploader is the free-text display name. It defaults
  // to an IP-derived suggestion (useIdentity), is editable right here at upload
  // time, persisted, and shared with Bulletin + Settings; a name is required.
  const { displayName, suggested, effectiveName, setDisplayName } = useIdentity();

  // Uploads run sequentially: one failure (bad type, size cap) doesn't stop the
  // rest, and the shared endpoint isn't hammered with parallel writes.
  const send = useCallback(
    async (list: FileList | File[]) => {
      const files = [...list];
      if (files.length === 0) return;
      if (!effectiveName) {
        setResults([{ name: "", error: "Enter your name first." }]);
        return;
      }
      setBusy(true);
      setResults([]);
      const tags = parseTags(tagsText);
      const outcome: UploadResult[] = [];
      for (let i = 0; i < files.length; i += 1) {
        setProgress({ done: i, total: files.length });
        try {
          await uploadFile(files[i], effectiveName, tags);
          outcome.push({ name: files[i].name, error: null });
        } catch (e) {
          outcome.push({ name: files[i].name, error: humanizeApiError(e) });
        }
      }
      setProgress(null);
      setResults(outcome);
      setBusy(false);
      if (outcome.some((r) => r.error === null)) {
        setTagsText("");
        onUploaded();
      }
    },
    [onUploaded, effectiveName, tagsText],
  );

  return (
    <Card title="Upload" subtitle="Max 50 MB each · pdf, png, csv, log, pcapng…">
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
      <label className="upload-name">
        <span>Tags</span>
        <TagInput value={tagsText} onChange={setTagsText} ariaLabel="Tags for the upload" />
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
          if (e.dataTransfer.files?.length) void send(e.dataTransfer.files);
        }}
      >
        <div className="upload-drop-icon" aria-hidden="true">
          ⬆
        </div>
        <div className="upload-drop-main">
          {busy && progress
            ? `Uploading ${progress.done + 1}/${progress.total}…`
            : "Drag files here, or click to browse"}
        </div>
        <div className="upload-drop-hint">Max 50 MB each · pdf, png, csv, log, pcapng…</div>
      </div>
      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          if (e.target.files?.length) void send(e.target.files);
          e.target.value = "";
        }}
      />
      {results.length > 0 ? (
        <div className="upload-results">
          {results.filter((r) => r.error === null).length > 0 ? (
            <div className="upload-result">
              ✓ {results.filter((r) => r.error === null).length} of {results.length} uploaded
            </div>
          ) : null}
          {results
            .filter((r) => r.error !== null)
            .map((r, i) => (
              <div className="upload-result upload-result-fail" key={`${r.name}-${i}`}>
                ✗ {r.name ? `${r.name} — ` : ""}{r.error}
              </div>
            ))}
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

export default function FilesSection({
  query = "",
  onTagClick,
}: {
  query?: string;
  onTagClick?: (tag: string) => void;
}) {
  const [data, setData] = useState<FilesList | null>(null);
  const [failed, setFailed] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Server-side search: the topbar query is debounced into `q` so we don't hit
  // the API on every keystroke, then the list refetches from page 1.
  const [q, setQ] = useState("");
  const [{ sort, order }, setSorting] = useState<{ sort: FileSortKey; order: SortOrder }>({
    sort: DEFAULT_SORT,
    order: DEFAULT_ORDER,
  });
  // One preview open at a time; toggling another row swaps it.
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const onToggleExpand = useCallback((id: number) => {
    setExpandedId((prev) => (prev === id ? null : id));
  }, []);
  // One inline tag editor open at a time (same idiom as the preview).
  const [tagEditId, setTagEditId] = useState<number | null>(null);
  // Rows currently on screen: reload refetches that many from offset 0 so an
  // upload/delete refresh doesn't collapse the list back to the first page.
  const loadedRef = useRef(PAGE_SIZE);

  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 300);
    return () => clearTimeout(t);
  }, [query]);

  const reload = useCallback(
    (limit?: number) => {
      setFailed(false);
      getFiles({
        limit: limit ?? Math.max(PAGE_SIZE, loadedRef.current),
        offset: 0,
        q: q || undefined,
        sort,
        order,
      })
        .then((next) => {
          loadedRef.current = next.files.length;
          setData(next);
        })
        .catch(() => setFailed(true));
    },
    [q, sort, order],
  );

  const loadMore = useCallback(() => {
    if (!data) return;
    setLoadingMore(true);
    getFiles({ limit: PAGE_SIZE, offset: data.files.length, q: q || undefined, sort, order })
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
  }, [data, q, sort, order]);

  // Runs on mount and whenever q/sort/order change identity via `reload`;
  // a new search or ordering starts back at the first page.
  useEffect(() => {
    loadedRef.current = PAGE_SIZE;
    reload(PAGE_SIZE);
  }, [reload]);

  // Header click cycle: other column -> asc; asc -> desc; desc -> back to default.
  const onSort = useCallback((col: FileSortKey) => {
    setSorting((prev) => {
      if (prev.sort !== col) return { sort: col, order: "asc" };
      if (prev.order === "asc") return { sort: col, order: "desc" };
      return { sort: DEFAULT_SORT, order: DEFAULT_ORDER };
    });
  }, []);

  const onDelete = useCallback(
    (file: WorkspaceFile) => {
      if (!window.confirm(`Delete "${file.filename}"? This cannot be undone.`)) {
        return;
      }
      deleteFile(file.id)
        .then(() => reload())
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

  const { stats, files } = data;
  const searching = q.length > 0;
  const orderLabel =
    sort === "date"
      ? order === "desc"
        ? "newest first"
        : "oldest first"
      : `by ${sort} (${order})`;
  const noun = `${searching ? "matching " : ""}file${data.total === 1 ? "" : "s"}`;
  const subtitle =
    files.length < data.total
      ? `${files.length} of ${data.total} ${noun} · ${orderLabel}`
      : `${data.total} ${noun} · ${orderLabel}`;

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
        <Card title="Shared Files" subtitle={subtitle}>
          {files.length > 0 ? (
            <SharedFilesTable
              rows={files}
              onDelete={onDelete}
              sort={sort}
              order={order}
              onSort={onSort}
              expandedId={expandedId}
              onToggleExpand={onToggleExpand}
              onTagClick={onTagClick}
              tagEditId={tagEditId}
              onToggleTagEdit={setTagEditId}
              onTagsSaved={reload}
            />
          ) : (
            <EmptyState
              icon="🗂"
              message={searching ? "No matching files" : "No files yet"}
              hint={searching ? "Search covers all files — try a different term." : "Upload a file to get started."}
            />
          )}
          {files.length < data.total ? (
            <div style={{ marginTop: "var(--space-3)" }}>
              <button type="button" className="btn" disabled={loadingMore} onClick={loadMore}>
                {loadingMore ? "Loading…" : `Load more (${data.total - files.length} more)`}
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
