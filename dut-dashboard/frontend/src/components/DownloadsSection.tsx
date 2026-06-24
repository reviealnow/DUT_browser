import { Fragment, useCallback, useEffect, useState } from "react";

import {
  analyzeSessionLog,
  getAnalyzerDownloadUrl,
  getAnalyzerPreviewUrl,
  getLogs,
  getLogTail,
  getSerialLogDownloadUrl,
  humanizeApiError,
  LogEntry,
  LogList,
} from "../api/rest";
import { Card, EmptyState } from "./shell/Card";

const TAIL_LINES = 200;
// Session logs accumulate fast; cap the visible height to ~5 rows and scroll for older.
const VISIBLE_SESSION_ROWS = 5;

type TailCell = { loading: boolean; lines?: string[]; truncated?: boolean; error?: string };

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(iso: string): string {
  return iso.replace("T", " ");
}

const isPng = (name: string): boolean => name.toLowerCase().endsWith(".png");

/**
 * Artifact table. PNG rows expand (▸/▾) to a full-width inline plot preview —
 * the <img> is rendered only on expand, so the image is fetched lazily on first
 * open. Non-image rows (CSV/TXT) stay Download-only.
 */
function FileTable({ rows, hrefFor }: { rows: LogEntry[]; hrefFor: (name: string) => string }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  function toggle(name: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
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
        {rows.map((row) => {
          const png = isPng(row.name);
          return (
            <Fragment key={row.name}>
              <tr>
                <td className="filetable-name">
                  {png ? (
                    <button
                      className="btn"
                      onClick={() => toggle(row.name)}
                      title="Preview plot"
                      style={{ padding: "0 6px", marginRight: 6 }}
                    >
                      {expanded.has(row.name) ? "▾" : "▸"}
                    </button>
                  ) : null}
                  {row.name}
                </td>
                <td>{formatSize(row.size)}</td>
                <td>{formatTime(row.mtime)}</td>
                <td>
                  <a className="btn" href={hrefFor(row.name)} download style={{ padding: "2px 10px" }}>
                    Download
                  </a>
                </td>
              </tr>
              {png && expanded.has(row.name) ? (
                <tr>
                  <td colSpan={4}>
                    <img className="plot-preview" src={getAnalyzerPreviewUrl(row.name)} alt={row.name} loading="lazy" />
                  </td>
                </tr>
              ) : null}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

/** Session-log table where each row expands to lazily peek the log's tail. */
function SessionLogTable({ rows, onAnalyzed }: { rows: LogEntry[]; onAnalyzed: () => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [tailByName, setTailByName] = useState<Record<string, TailCell>>({});
  const [analyzing, setAnalyzing] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<{ tone: "ok" | "danger"; message: string } | null>(null);

  async function analyze(name: string) {
    setNotice(null);
    setAnalyzing((prev) => new Set(prev).add(name));
    try {
      const result = await analyzeSessionLog(name);
      setNotice({ tone: "ok", message: `Analyzed ${name} — ${result.files.length} output file(s) ready below.` });
      onAnalyzed(); // refresh the Analyzer outputs card
    } catch (e) {
      setNotice({ tone: "danger", message: humanizeApiError(e) });
    } finally {
      setAnalyzing((prev) => {
        const next = new Set(prev);
        next.delete(name);
        return next;
      });
    }
  }

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
    <>
    {notice ? (
      <div
        className="flash"
        style={{
          marginBottom: "var(--space-3)",
          color: notice.tone === "danger" ? "var(--danger)" : "var(--ok)",
        }}
      >
        {notice.message}
      </div>
    ) : null}
    {rows.length > VISIBLE_SESSION_ROWS ? (
      <div className="logscroll-note">Showing newest first — scroll for older ({rows.length} total).</div>
    ) : null}
    <div className="logscroll">
    <table className="filetable">
      <thead>
        <tr>
          <th>Name</th>
          <th>Size</th>
          <th>Modified</th>
          <th aria-label="actions" />
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
                <div style={{ display: "flex", gap: "var(--space-2)", justifyContent: "flex-end" }}>
                  <button
                    className="btn"
                    onClick={() => analyze(row.name)}
                    disabled={analyzing.has(row.name)}
                    title="Run the analyzer on this log → CSV/PNG outputs appear below"
                    style={{ padding: "2px 10px" }}
                  >
                    {analyzing.has(row.name) ? "Analyzing…" : "Analyze"}
                  </button>
                  <a className="btn" href={getSerialLogDownloadUrl(row.name)} download style={{ padding: "2px 10px" }}>
                    Download
                  </a>
                </div>
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
    </div>
    </>
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

  const reload = useCallback(() => {
    setFailed(false);
    getLogs()
      .then(setData)
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

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
      <Card title="Session logs" subtitle="Raw DUT serial logs — Analyze to publish CSV/PNG below, or Download the bundle">
        {sessions.length > 0 ? (
          <SessionLogTable rows={sessions} onAnalyzed={reload} />
        ) : (
          <EmptyState icon="🗂" message={query ? "No matching session logs" : "No session logs yet"} hint="Open a DUT to start recording." />
        )}
      </Card>
      <Card title="Analyzer outputs" subtitle="CPU/memory CSV + plots from the analyzer">
        {artifacts.length > 0 ? (
          <FileTable rows={artifacts} hrefFor={getAnalyzerDownloadUrl} />
        ) : (
          <EmptyState icon="📊" message={query ? "No matching artifacts" : "No analyzer outputs yet"} hint="Click Analyze on a session log above to generate these." />
        )}
      </Card>
    </>
  );
}
