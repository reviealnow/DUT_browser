import { Fragment, useEffect, useState } from "react";

import {
  getAnalyzerDownloadUrl,
  getLogs,
  getLogTail,
  getSerialLogDownloadUrl,
  LogEntry,
  LogList,
} from "../api/rest";
import { Card, EmptyState } from "./shell/Card";

const TAIL_LINES = 200;

type TailCell = { loading: boolean; lines?: string[]; truncated?: boolean; error?: string };

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

/** Session-log table where each row expands to lazily peek the log's tail. */
function SessionLogTable({ rows }: { rows: LogEntry[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [tailByName, setTailByName] = useState<Record<string, TailCell>>({});

  function toggle(name: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
        // Lazy fetch once per file (cached across collapse/expand).
        if (!tailByName[name]) {
          setTailByName((s) => ({ ...s, [name]: { loading: true } }));
          getLogTail(name, TAIL_LINES)
            .then((r) =>
              setTailByName((s) => ({ ...s, [name]: { loading: false, lines: r.lines, truncated: r.truncated } })),
            )
            .catch((e) =>
              setTailByName((s) => ({
                ...s,
                [name]: { loading: false, error: e instanceof Error ? e.message : "Failed to load" },
              })),
            );
        }
      }
      return next;
    });
  }

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
          <Fragment key={row.name}>
            <tr>
              <td className="filetable-name">
                <button
                  className="btn"
                  onClick={() => toggle(row.name)}
                  title="Peek the last lines of this log"
                  style={{ padding: "0 6px", marginRight: 6 }}
                >
                  {expanded.has(row.name) ? "▾" : "▸"}
                </button>
                {row.name}
              </td>
              <td>{formatSize(row.size)}</td>
              <td>{formatTime(row.mtime)}</td>
              <td>
                <a className="btn" href={getSerialLogDownloadUrl(row.name)} download style={{ padding: "2px 10px" }}>
                  Download
                </a>
              </td>
            </tr>
            {expanded.has(row.name) ? (
              <tr>
                <td colSpan={4}>
                  <LogTailView cell={tailByName[row.name]} />
                </td>
              </tr>
            ) : null}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

function LogTailView({ cell }: { cell?: TailCell }) {
  if (!cell || cell.loading) return <div className="logtail-status">Loading…</div>;
  if (cell.error) return <div className="logtail-status">Could not load tail: {cell.error}</div>;
  const lines = cell.lines ?? [];
  if (lines.length === 0) return <div className="logtail-status">Log is empty.</div>;
  return (
    <div>
      {cell.truncated ? (
        <div className="logtail-status">Showing the last {lines.length} lines · older lines not shown — download for the full log.</div>
      ) : null}
      <pre className="logtail">{lines.join("\n")}</pre>
    </div>
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
          <SessionLogTable rows={sessions} />
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
