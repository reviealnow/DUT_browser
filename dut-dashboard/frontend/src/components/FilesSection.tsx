import { useCallback, useEffect, useRef, useState } from "react";

import {
  deleteFile,
  getFileDownloadUrl,
  getFiles,
  FilesList,
  humanizeApiError,
  uploadFile,
  WorkspaceFile,
} from "../api/rest";
import { loadDisplayName } from "../monitoring/useSettings";
import { Card, EmptyState, KpiCard } from "./shell/Card";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Known extensions get a branded chip colour (see .t-* in dashboard.css);
// anything else falls back to the neutral .ftype background.
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
  return <span className={`ftype ${FTYPE_CLASS[ext] ?? ""}`}>{label}</span>;
}

function SharedFilesTable({ rows, onDelete }: { rows: WorkspaceFile[]; onDelete: (file: WorkspaceFile) => void }) {
  return (
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
            <td>{row.uploader ?? "—"}</td>
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
  );
}

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const send = useCallback(
    async (file: File) => {
      setBusy(true);
      setError(null);
      try {
        await uploadFile(file, loadDisplayName() || null);
        onUploaded();
      } catch (e) {
        setError(humanizeApiError(e));
      } finally {
        setBusy(false);
      }
    },
    [onUploaded],
  );

  return (
    <Card title="Upload" subtitle="Max 50 MB · pdf, png, csv, log, pcapng…">
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
        {busy ? "Uploading…" : "Drag a file here or click to browse"}
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

export default function FilesSection({ query = "" }: { query?: string }) {
  const [data, setData] = useState<FilesList | null>(null);
  const [failed, setFailed] = useState(false);

  const reload = useCallback(() => {
    setFailed(false);
    getFiles()
      .then(setData)
      .catch(() => setFailed(true));
  }, []);

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

      <div className="grid">
        <Card title="Shared Files" subtitle={`${data.files.length} files · newest first`}>
          {files.length > 0 ? (
            <SharedFilesTable rows={files} onDelete={onDelete} />
          ) : (
            <EmptyState
              icon="🗂"
              message={needle ? "No matching files" : "No files yet"}
              hint={needle ? "Try a different filter." : "Upload a file to get started."}
            />
          )}
        </Card>
        <UploadCard onUploaded={reload} />
      </div>
    </>
  );
}
