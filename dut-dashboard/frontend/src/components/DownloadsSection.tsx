import { useEffect, useState } from "react";

import { getAnalyzerDownloadUrl, getLogs, getSerialLogDownloadUrl, LogEntry, LogList } from "../api/rest";
import { Card, EmptyState } from "./shell/Card";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(iso: string): string {
  return iso.replace("T", " ");
}

function FileTable({ rows, hrefFor }: { rows: LogEntry[]; hrefFor: (name: string) => string }) {
  return (
    <table className="filetable">
      <thead>
        <tr>
          <th>Name</th>
          <th>Size</th>
          <th>Modified</th>
          <th aria-label="download" />
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.name}>
            <td className="filetable-name">{row.name}</td>
            <td>{formatSize(row.size)}</td>
            <td>{formatTime(row.mtime)}</td>
            <td>
              <a className="btn" href={hrefFor(row.name)} download style={{ padding: "2px 10px" }}>
                Download
              </a>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function DownloadsSection({ query = "" }: { query?: string }) {
  const [data, setData] = useState<LogList | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
    getLogs()
      .then(setData)
      .catch(() => setFailed(true));
  }, []);

  if (failed) {
    return (
      <Card title="Downloads" subtitle="Log bundles and analyzer artifacts">
        <EmptyState icon="⬇" message="Could not load the file list" hint="Is the backend reachable?" />
      </Card>
    );
  }
  if (!data) {
    return (
      <Card title="Downloads" subtitle="Log bundles and analyzer artifacts">
        <EmptyState icon="⬇" message="Loading…" />
      </Card>
    );
  }

  const needle = query.trim().toLowerCase();
  const match = (rows: LogEntry[]) => (needle ? rows.filter((r) => r.name.toLowerCase().includes(needle)) : rows);
  const sessions = match(data.sessions);
  const artifacts = match(data.artifacts);

  return (
    <>
      <Card title="Session logs" subtitle="Raw DUT serial logs — download runs the analyzer for long logs">
        {sessions.length > 0 ? (
          <FileTable rows={sessions} hrefFor={getSerialLogDownloadUrl} />
        ) : (
          <EmptyState icon="🗂" message={query ? "No matching session logs" : "No session logs yet"} hint="Open a DUT to start recording." />
        )}
      </Card>
      <Card title="Analyzer outputs" subtitle="CPU/memory CSV + plots from the latest analyzer run">
        {artifacts.length > 0 ? (
          <FileTable rows={artifacts} hrefFor={getAnalyzerDownloadUrl} />
        ) : (
          <EmptyState icon="📊" message={query ? "No matching artifacts" : "No analyzer outputs yet"} hint="Run the analyzer (download a long DUT log)." />
        )}
      </Card>
    </>
  );
}
