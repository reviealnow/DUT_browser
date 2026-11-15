import { useEffect, useMemo, useRef, useState } from "react";

import {
  getFiles,
  getPacketExportUrl,
  getPacketFrame,
  humanizeApiError,
  PacketColumn,
  PacketFilterCheck,
  PacketHost,
  PacketSeverity,
  validatePacketFilter,
  WorkspaceFile,
} from "../api/rest";
import { usePacketAnalysis } from "../monitoring/PacketAnalysisContext";
import { Card, EmptyState } from "./shell/Card";
import { PacketDetailPanel } from "./PacketDetailPanel";
import { PacketTable } from "./PacketTable";

/**
 * Packet Monitor (docs/design-packet-monitor.md Part B, §B11).
 *
 * Pick a capture from the Files workspace and a Host an admin has connected,
 * pick a Test Mode, optionally narrow it with a display filter, and run. The
 * capture is analysed by tshark on the Host, never on this server; the table,
 * findings and frame detail come back over REST.
 */
export const CAPTURE_RE = /\.(pcap|pcapng|cap)$/i;
export const FILTER_DEBOUNCE_MS = 600;
/** `/api/files` serves at most this many rows per request. */
export const FILES_PAGE = 500;
/** Pages read per search before the list says it may be incomplete. */
export const FILES_MAX_PAGES = 10;
/**
 * Filename searches that, between them, find every name CAPTURE_RE accepts:
 * ".pcap" also matches ".pcapng". The server's match is a case-insensitive
 * substring, so the results are filtered by CAPTURE_RE afterwards.
 */
const CAPTURE_SEARCHES = [".pcap", ".cap"];
const HISTORY_KEY = "packetMonitor.filterHistory";
const HIDDEN_KEY = "packetMonitor.hiddenColumns";
const HISTORY_MAX = 10;
const SEVERITY_ORDER: PacketSeverity[] = ["fail", "warn", "info"];

function readStore<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeStore(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // A private window or full storage: the history is a convenience only.
  }
}

/**
 * Every capture in the Files workspace, newest first, using the paging and
 * filename search `/api/files` already has. `complete` is false when a search
 * stopped at FILES_MAX_PAGES before reaching its total.
 */
export async function listCaptures(): Promise<{ files: WorkspaceFile[]; complete: boolean }> {
  const found = new Map<number, WorkspaceFile>();
  let complete = true;
  for (const q of CAPTURE_SEARCHES) {
    for (let page = 0; ; page += 1) {
      if (page === FILES_MAX_PAGES) {
        complete = false;
        break;
      }
      const list = await getFiles({ q, limit: FILES_PAGE, offset: page * FILES_PAGE });
      for (const f of list.files) {
        if (CAPTURE_RE.test(f.filename)) found.set(f.id, f);
      }
      if (list.files.length < FILES_PAGE || (page + 1) * FILES_PAGE >= list.total) break;
    }
  }
  const files = [...found.values()].sort(
    (a, b) => b.uploaded_at.localeCompare(a.uploaded_at) || b.id - a.id,
  );
  return { files, complete };
}

/** Why a Host cannot be picked, in the words the operator needs, or "" if it can. */
export function hostBlocker(host: PacketHost): string {
  if (!host.connected) return "not connected — ask an admin to connect it";
  if (!host.probe) return "not probed yet";
  if (host.probe.problems.length) {
    const hint = host.probe.install_hint ? ` (${host.probe.install_hint})` : "";
    return host.probe.problems.join("; ") + hint;
  }
  return "";
}

export default function PacketMonitorSection() {
  const state = usePacketAnalysis();
  const { hosts, modes, analysis, load, form, setForm } = state;
  // The form lives in the context so it survives leaving the section (#231 review, 7).
  const { fileId, hostId, modeId, filter, extra, frame, byBss } = form;
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [filesComplete, setFilesComplete] = useState(true);
  const [filesError, setFilesError] = useState("");
  // Filter checks by `${hostId}\n${text}`: a result only ever describes the
  // text it was asked about, so neither a stale nor a late answer can enable
  // or block Run for what is in the box now.
  const [checks, setChecks] = useState<Record<string, PacketFilterCheck>>({});
  const [history, setHistory] = useState<string[]>(() => readStore<string[]>(HISTORY_KEY, []));
  const [hidden, setHidden] = useState<Record<string, string[]>>(() => readStore(HIDDEN_KEY, {}));
  const [severity, setSeverity] = useState<PacketSeverity | null>(null);
  const [tree, setTree] = useState<unknown>(null);
  const [treeLoading, setTreeLoading] = useState(false);
  const [treeError, setTreeError] = useState("");
  // Bumped by every frame request and every Run: only the latest may fill the panel.
  const frameRequest = useRef(0);

  // On entry, once: the Hosts, the Test Modes and the captures to choose from.
  useEffect(() => {
    void load();
    listCaptures()
      .then((list) => {
        setFiles(list.files);
        setFilesComplete(list.complete);
      })
      .catch((e: unknown) => setFilesError(humanizeApiError(e)));
  }, [load]);

  useEffect(() => {
    if (!modeId && modes.length) setForm({ modeId: modes[0].id });
  }, [modes, modeId, setForm]);

  // Validate as the user types: debounced, and only for a filter not checked yet.
  const filterText = filter.trim();
  const filterKey = hostId && filterText ? `${hostId}\n${filterText}` : "";
  const check: PacketFilterCheck | null = filterKey ? (checks[filterKey] ?? null) : null;
  const checking = filterKey !== "" && check === null;
  useEffect(() => {
    if (!filterKey || filterKey in checks) return;
    const timer = window.setTimeout(() => {
      const record = (result: PacketFilterCheck) => setChecks((prev) => ({ ...prev, [filterKey]: result }));
      validatePacketFilter(hostId, filterText)
        .then(record)
        .catch((e: unknown) => record({ ok: null, error: humanizeApiError(e) }));
    }, FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // `checks` is read, not watched: a result arriving must not restart the timer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey]);

  const mode = modes.find((m) => m.id === modeId) ?? null;
  const host = hosts.find((h) => h.id === hostId) ?? null;
  const blocker = host ? hostBlocker(host) : "";
  const running = analysis !== null && (analysis.status === "queued" || analysis.status === "running");
  const extraFields = extra.split(/[\s,]+/).filter(Boolean);
  // A filter must have been checked as it reads now; "could not check" (ok:
  // null) still lets the user run, since the Host will say if it is wrong.
  const filterReady = !filterKey || (check !== null && check.ok !== false);
  const canRun =
    fileId !== null && host !== null && !blocker && mode !== null && !running && !state.starting && filterReady;

  const run = async () => {
    if (!canRun || fileId === null || !mode) return;
    const text = filterText;
    if (text) {
      const next = [text, ...history.filter((h) => h !== text)].slice(0, HISTORY_MAX);
      setHistory(next);
      writeStore(HISTORY_KEY, next);
    }
    frameRequest.current += 1;
    setForm({ frame: null, byBss: false });
    setTree(null);
    setTreeLoading(false);
    setSeverity(null);
    await state.start({
      file_id: fileId,
      collector_id: hostId,
      mode_id: mode.id,
      display_filter: text,
      extra_fields: extraFields,
    });
  };

  // Columns of the finished analysis (the ones its Host resolved), minus the
  // ones the user hid for this mode.
  const allColumns: PacketColumn[] = (analysis?.columns ?? []).filter((c) => !c.hidden);
  const hiddenHere = new Set(hidden[analysis?.mode_id ?? ""] ?? []);
  const shownColumns = allColumns.filter((c) => !hiddenHere.has(c.key));
  const toggleColumn = (key: string) => {
    const modeKey = analysis?.mode_id ?? "";
    const current = new Set(hidden[modeKey] ?? []);
    if (current.has(key)) current.delete(key);
    else current.add(key);
    const next = { ...hidden, [modeKey]: [...current] };
    setHidden(next);
    writeStore(HIDDEN_KEY, next);
  };

  const severityByFrame = useMemo(() => {
    const worst = new Map<number, PacketSeverity>();
    for (const finding of state.findings) {
      for (const f of finding.frames) {
        const had = worst.get(f);
        if (!had || SEVERITY_ORDER.indexOf(finding.severity) < SEVERITY_ORDER.indexOf(had)) {
          worst.set(f, finding.severity);
        }
      }
    }
    return worst;
  }, [state.findings]);

  const shownFindings = severity ? state.findings.filter((f) => f.severity === severity) : state.findings;

  const openFrame = async (number: number) => {
    if (!analysis) return;
    const request = ++frameRequest.current;
    setForm({ frame: number });
    setTree(null);
    setTreeError("");
    setTreeLoading(true);
    try {
      const next = await getPacketFrame(analysis.id, number);
      if (request === frameRequest.current) setTree(next);
    } catch (e) {
      if (request === frameRequest.current) setTreeError(humanizeApiError(e));
    } finally {
      if (request === frameRequest.current) setTreeLoading(false);
    }
  };

  const closeFrame = () => {
    frameRequest.current += 1;
    setForm({ frame: null });
  };

  // Back in the section with a frame still open: its tree was not kept, so
  // ask for it again. Once, on entry.
  useEffect(() => {
    if (frame !== null && analysis?.status === "done") void openFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const done = analysis?.status === "done";

  return (
    <div className="grid pkt-section">
      <Card title="Capture" subtitle="tshark runs on the Host, not on this server">
        {state.error ? <div className="pkt-error">{state.error}</div> : null}
        {filesError ? <div className="pkt-error">{filesError}</div> : null}
        {!filesComplete ? (
          <div className="pkt-blocker" role="status">
            More than {FILES_MAX_PAGES * FILES_PAGE} files in Files look like captures; only the newest are listed
            here.
          </div>
        ) : null}
        <div className="pkt-controls">
          <label className="modal-label">
            Capture file
            <select
              className="input"
              value={fileId ?? ""}
              onChange={(e) => setForm({ fileId: e.target.value ? Number(e.target.value) : null })}
              aria-label="Capture file"
            >
              <option value="">{files.length ? "Choose a capture" : "No pcap / pcapng in Files"}</option>
              {files.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.filename}
                </option>
              ))}
            </select>
          </label>
          <label className="modal-label">
            Host
            <select
              className="input"
              value={hostId}
              onChange={(e) => setForm({ hostId: e.target.value })}
              aria-label="Host"
            >
              <option value="">{hosts.length ? "Choose a Host" : "No Hosts registered"}</option>
              {hosts.map((h) => (
                <option key={h.id} value={h.id} disabled={!h.connected}>
                  {(hostBlocker(h) ? "○ " : "● ") + h.label}
                  {h.probe?.tshark_version ? ` — tshark ${h.probe.tshark_version}` : ""}
                </option>
              ))}
            </select>
          </label>
          {host && host.connected ? (
            <button
              type="button"
              className="btn"
              disabled={state.probing === host.id}
              onClick={() => void state.probe(host.id)}
            >
              {state.probing === host.id ? "Checking…" : "Check Host"}
            </button>
          ) : null}
        </div>
        {host && blocker ? (
          <div className="pkt-blocker" role="status">
            {host.label}: {blocker}
          </div>
        ) : null}

        <div className="pkt-controls">
          <label className="modal-label">
            Test Mode
            <select
              className="input"
              value={modeId}
              onChange={(e) => setForm({ modeId: e.target.value })}
              aria-label="Test Mode"
            >
              {modes.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <label className="modal-label pkt-filter">
            Display filter
            <input
              className="input"
              value={filter}
              onChange={(e) => setForm({ filter: e.target.value })}
              placeholder={mode?.display_filter ? `in addition to ${mode.display_filter}` : "e.g. wlan.ssid == \"lab\""}
              list="packet-filter-history"
              maxLength={1024}
              aria-label="Display filter"
              aria-invalid={check?.ok === false}
            />
            <datalist id="packet-filter-history">
              {history.map((h) => (
                <option key={h} value={h} />
              ))}
            </datalist>
          </label>
          <label className="modal-label">
            Extra fields
            <input
              className="input"
              value={extra}
              onChange={(e) => setForm({ extra: e.target.value })}
              placeholder="wlan.fixed.beacon, …"
              aria-label="Extra fields"
            />
          </label>
        </div>
        <FilterStatus check={check} checking={checking} hasFilter={filterKey !== ""} />
        {mode ? <div className="pkt-muted">{mode.description}</div> : null}

        <div className="pkt-actions">
          <button type="button" className="btn primary" disabled={!canRun} onClick={() => void run()}>
            {state.starting ? "Starting…" : "Run"}
          </button>
          {running ? (
            <button type="button" className="btn" onClick={() => void state.cancel()}>
              Cancel
            </button>
          ) : null}
          <AnalysisStatus />
        </div>
      </Card>

      {done && analysis ? (
        <Card
          title="Findings"
          subtitle={`${analysis.frames_shown ?? 0} frames · tshark ${analysis.tshark_version ?? "?"}`}
          className="col-span-2"
          actions={
            <a className="btn" href={getPacketExportUrl(analysis.id)} download>
              Export CSV
            </a>
          }
        >
          <div className="pkt-findings-bar" role="group" aria-label="Findings by severity">
            {SEVERITY_ORDER.map((level) => (
              <button
                key={level}
                type="button"
                className={`pill ${PILL[level]}${severity === level ? " pkt-active" : ""}`}
                aria-pressed={severity === level}
                onClick={() => setSeverity(severity === level ? null : level)}
              >
                {analysis.findings_summary[level]} {level}
              </button>
            ))}
          </div>
          {shownFindings.length === 0 ? (
            <div className="pkt-muted">{state.findings.length ? "None at this severity." : "No findings."}</div>
          ) : (
            <ul className="pkt-findings">
              {shownFindings.map((f, i) => (
                <li key={`${f.rule}-${i}`} className={`pkt-finding pkt-${f.severity}`}>
                  <span className={`pill ${PILL[f.severity]}`}>{f.severity}</span> {f.message}
                  {f.group !== null && f.group !== undefined ? <span className="pkt-muted"> — {String(f.group)}</span> : null}
                  <span className="pkt-frames">
                    {f.frames.slice(0, 12).map((n) => (
                      <button key={n} type="button" className="pkt-frame-link" onClick={() => void openFrame(n)}>
                        #{n}
                      </button>
                    ))}
                    {f.frames.length > 12 ? <span className="pkt-muted"> +{f.frames.length - 12}</span> : null}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      ) : null}

      {done && analysis ? (
        <Card
          title="Frames"
          className="col-span-2"
          actions={
            <>
              {state.groups.length ? (
                <label className="pkt-toggle">
                  <input type="checkbox" checked={byBss} onChange={(e) => setForm({ byBss: e.target.checked })} /> One row per BSS
                </label>
              ) : null}
              <ColumnChooser columns={allColumns} hidden={hiddenHere} onToggle={toggleColumn} />
            </>
          }
        >
          <div className={frame !== null ? "pkt-split" : undefined}>
            <div className="pkt-main">
              {byBss && state.groups.length ? (
                <GroupsTable columns={shownColumns} />
              ) : (
                <PacketTable
                  total={analysis.frames_shown ?? 0}
                  columns={shownColumns}
                  page={state.page}
                  severityByFrame={severityByFrame}
                  selectedFrame={frame}
                  onOpen={(n) => void openFrame(n)}
                />
              )}
            </div>
            {frame !== null ? (
              <PacketDetailPanel
                frame={frame}
                tree={tree}
                loading={treeLoading}
                error={treeError}
                onClose={closeFrame}
              />
            ) : null}
          </div>
        </Card>
      ) : null}

      {!analysis && !state.loading && hosts.length === 0 ? (
        <Card title="Frames" className="col-span-2">
          <EmptyState icon="📦" message="No Hosts yet" hint="An admin connects a Host under Fleet → Hosts." />
        </Card>
      ) : null}
    </div>
  );
}

const PILL: Record<PacketSeverity, string> = { fail: "danger", warn: "warn", info: "idle" };

function FilterStatus({ check, checking, hasFilter }: { check: PacketFilterCheck | null; checking: boolean; hasFilter: boolean }) {
  if (!hasFilter) return null;
  if (checking) return <div className="pkt-muted">Checking the filter…</div>;
  if (!check) return null;
  if (check.ok === true) return <div className="pkt-filter-ok">Filter is valid.</div>;
  if (check.ok === false) return <div className="pkt-error">{check.error}</div>;
  return <div className="pkt-muted">Filter not checked: {check.error}</div>;
}

function AnalysisStatus() {
  const { analysis, analysisError } = usePacketAnalysis();
  // The error sits beside the status, never in place of it: a failed poll or
  // Cancel must not hide "Done" or a truncation warning.
  const error = analysisError ? <span className="pkt-error">{analysisError}</span> : null;
  if (!analysis) return error;
  const parts: string[] = [];
  switch (analysis.status) {
    case "queued":
      parts.push("Queued behind another analysis on this Host…");
      break;
    case "running":
      parts.push("Running on the Host…");
      break;
    case "failed":
      parts.push(`Failed: ${analysis.error ?? "unknown error"}`);
      break;
    case "cancelled":
      parts.push("Cancelled.");
      break;
    case "done":
      parts.push(`Done: ${analysis.frames_shown ?? 0} frames.`);
      break;
  }
  return (
    <span className="pkt-status" role="status">
      {parts.join(" ")}
      {analysis.warning ? <span className="pill warn">{analysis.warning}</span> : null}
      {analysis.rows_dropped ? (
        <span className="pill warn">{analysis.rows_dropped} rows dropped (a tab inside a value)</span>
      ) : null}
      {error}
    </span>
  );
}

function ColumnChooser({
  columns,
  hidden,
  onToggle,
}: {
  columns: PacketColumn[];
  hidden: Set<string>;
  onToggle: (key: string) => void;
}) {
  if (!columns.length) return null;
  return (
    <details className="pkt-columns">
      <summary className="btn">Columns</summary>
      <div className="pkt-columns-menu">
        {columns.map((c) => (
          <label key={c.key}>
            <input type="checkbox" checked={!hidden.has(c.key)} onChange={() => onToggle(c.key)} /> {c.label}
          </label>
        ))}
      </div>
    </details>
  );
}

function GroupsTable({ columns }: { columns: PacketColumn[] }) {
  const { groups } = usePacketAnalysis();
  return (
    <div className="pkt-window">
      <table className="filetable pkt-table">
        <thead>
          <tr>
            <th>Frames</th>
            {columns.map((c) => (
              <th key={c.key}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={String(g.group)} className="pkt-row">
              <td>{g.frames}</td>
              {columns.map((c) => (
                <td key={c.key}>{c.key in g.display ? g.display[c.key] : ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
