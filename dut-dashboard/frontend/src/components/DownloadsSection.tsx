import { Fragment, useCallback, useEffect, useState } from "react";

import {
  analyzeSessionLog,
  ContextEntry,
  getAnalyzerDownloadUrl,
  getAnalyzerPreviewUrl,
  getContextDownloadUrl,
  getLogs,
  getLogTail,
  getSerialLogDownloadUrl,
  getSurveyDownloadUrl,
  humanizeApiError,
  LogEntry,
  LogList,
  SessionLogEntry,
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
                <td className="filetable-actions">
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

const NO_CONTEXT_HINT = "no context captured during this session";

/**
 * Context capture table. Same shape as the artifact table but the download URL
 * needs the capture's kind, which fixes the directory it is served from.
 */
function ContextTable({ rows }: { rows: ContextEntry[] }) {
  return (
    <table className="filetable">
      <thead>
        <tr>
          <th>Name</th>
          <th>Kind</th>
          <th>Size</th>
          <th>Modified</th>
          <th aria-label="download" />
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={`${row.kind}/${row.name}`}>
            <td className="filetable-name">{row.name}</td>
            <td>{row.kind}</td>
            <td>{formatSize(row.size)}</td>
            <td>{formatTime(row.mtime)}</td>
            <td className="filetable-actions">
              <a
                className="btn"
                href={getContextDownloadUrl(row.kind, row.name)}
                download
                style={{ padding: "2px 10px" }}
              >
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
function SessionLogTable({ rows, onAnalyzed }: { rows: SessionLogEntry[]; onAnalyzed: () => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [tailByName, setTailByName] = useState<Record<string, TailCell>>({});
  const [analyzing, setAnalyzing] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<{ tone: "ok" | "danger"; message: string } | null>(null);

  async function analyze(name: string) {
    setNotice(null);
    setAnalyzing((prev) => new Set(prev).add(name));
    try {
      const result = await analyzeSessionLog(name);
      const context = result.context?.files.length
        ? `${result.context.files.length} context file(s) bundled alongside.`
        : `No context was captured during that session.`;
      setNotice({
        tone: "ok",
        message: `Analyzed ${name} — ${result.files.length} output file(s) ready below. ${context}`,
      });
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
                <span className="context-note">
                  {row.context.length > 0
                    ? `${row.context.length} context file(s) from this session — expand to open`
                    : NO_CONTEXT_HINT}
                </span>
              </td>
              <td>{formatSize(row.size)}</td>
              <td>{formatTime(row.mtime)}</td>
              <td className="filetable-actions">
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
                  <SessionContext files={row.context} />
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

const CONTEXT_KIND_LABELS: Record<string, string> = {
  "site-survey": "Site Survey",
  "wifi-clients": "Wi-Fi Clients",
  "ssid-capability": "SSID Capability",
};

/** The captures taken while this session was recording, grouped back into the
 * json+csv pairs they are written as.
 *
 * Named and openable here rather than only counted: these are the arrival
 * picture of the site, which is what has to answer "why was this channel
 * chosen". The flat tables lower down mix every session and DUT together, so
 * matching a capture to its session used to be manual timestamp arithmetic. */
function SessionContext({ files }: { files: ContextEntry[] }) {
  if (files.length === 0) return null;

  // `<kind>-<dut>-<ts>.<ext>` — pair on everything but the extension.
  const pairs = new Map<string, { kind: string; stem: string; files: ContextEntry[] }>();
  for (const file of files) {
    const stem = file.name.replace(/\.(json|csv)$/i, "");
    const existing = pairs.get(stem);
    if (existing) existing.files.push(file);
    else pairs.set(stem, { kind: file.kind, stem, files: [file] });
  }

  return (
    <div className="session-context">
      <div className="logtail-status">
        Captured while this session was recording — the site as it looked on arrival.
      </div>
      <ul className="session-context-list">
        {[...pairs.values()].map(({ kind, stem, files: pair }) => (
          <li key={stem}>
            <span className="pill">{CONTEXT_KIND_LABELS[kind] ?? kind}</span>
            <code>{stem}</code>
            {pair
              .slice()
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((file) => (
                <a
                  key={file.name}
                  className="btn"
                  href={getContextDownloadUrl(file.kind, file.name)}
                  download
                  title={`${file.name} · ${formatSize(file.size)}`}
                >
                  {file.name.toLowerCase().endsWith(".csv") ? "CSV" : "JSON"}
                </a>
              ))}
          </li>
        ))}
      </ul>
    </div>
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
  const match = <T extends LogEntry>(rows: T[]): T[] =>
    needle ? rows.filter((r) => r.name.toLowerCase().includes(needle)) : rows;
  const sessions = match(data.sessions);
  const artifacts = match(data.artifacts);
  const surveys = match(data.surveys);
  const context = match(data.context ?? []);

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
      <Card title="Site surveys" subtitle="Persisted channel scans — JSON + neighbor CSV per scan">
        {surveys.length > 0 ? (
          <FileTable rows={surveys} hrefFor={getSurveyDownloadUrl} />
        ) : (
          <EmptyState icon="📡" message={query ? "No matching surveys" : "No site surveys yet"} hint="Run a Site Survey on a connected DUT to persist one." />
        )}
      </Card>
      <Card
        title="Connect-time context"
        subtitle="Wi-Fi clients and SSID capability as captured when each DUT was connected"
      >
        {context.length > 0 ? (
          <ContextTable rows={context} />
        ) : (
          <EmptyState
            icon="📍"
            message={query ? "No matching context captures" : "No context captured yet"}
            hint="Connecting a DUT captures its clients and SSID capability automatically."
          />
        )}
      </Card>
    </>
  );
}
