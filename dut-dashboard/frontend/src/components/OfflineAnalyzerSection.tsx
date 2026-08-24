import { ChangeEvent, DragEvent, useEffect, useMemo, useState } from "react";

import { Card, EmptyState, KpiCard } from "./shell/Card";
import ChartData from "./charts/ChartData";
import { ImportPlan, ParsedLogFile, planImport } from "../offline/importPlan";
import { FieldKey, LOG_FIELDS, NUMERIC_LOG_FIELDS, parseLog } from "../offline/logParser";
import { loadOfflineDuts, OfflineDutRecord, removeOfflineDut, saveOfflineDut } from "../offline/offlineDb";

type StoredDut = OfflineDutRecord;

type ChartSeries = { id: string; name: string; values: Array<{ x: number; y: number }> };

const COLORS = ["#1565c0", "#0c7a43", "#b3261e", "#8b4acb", "#d66b0d", "#087ea4"];

function OfflineChart({ series, unit }: { series: ChartSeries[]; unit: string }) {
  const points = series.flatMap((item) => item.values);
  if (points.length === 0) {
    return <EmptyState icon="⌁" message="No chart data" hint="Select a DUT and a metric with valid samples." />;
  }
  const minX = Math.min(...points.map((point) => point.x));
  const maxX = Math.max(...points.map((point) => point.x));
  const minY = Math.min(...points.map((point) => point.y));
  const maxY = Math.max(...points.map((point) => point.y));
  const xSpan = maxX - minX || 1;
  const ySpan = maxY - minY || 1;
  const x = (value: number) => 56 + ((value - minX) / xSpan) * 716;
  const y = (value: number) => 18 + ((maxY - value) / ySpan) * 214;

  return (
    <div className="offline-chart-wrap">
      <svg className="offline-chart" viewBox="0 0 800 270" role="img" aria-label="Selected DUT metric comparison">
        {[0, 1, 2, 3, 4].map((tick) => {
          const tickY = 18 + tick * 53.5;
          const value = maxY - (ySpan * tick) / 4;
          return (
            <g key={tick}>
              <line x1="56" y1={tickY} x2="772" y2={tickY} className="offline-chart-grid" />
              <text x="48" y={tickY + 4} textAnchor="end" className="offline-chart-label">
                {Math.abs(value) >= 1000 ? Math.round(value).toLocaleString() : value.toFixed(1)}
              </text>
            </g>
          );
        })}
        {series.map((item, index) => (
          <polyline
            key={item.id}
            points={item.values.map((point) => `${x(point.x)},${y(point.y)}`).join(" ")}
            fill="none"
            stroke={COLORS[index % COLORS.length]}
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        <text x="414" y="262" textAnchor="middle" className="offline-chart-label">Test Times</text>
        {unit ? <text x="8" y="14" className="offline-chart-label">{unit}</text> : null}
      </svg>
      <div className="offline-chart-legend">
        {series.map((item, index) => (
          <span key={item.id}><i style={{ background: COLORS[index % COLORS.length] }} />{item.name}</span>
        ))}
      </div>
      {/* Every hand-rendered chart here also publishes its source data, so a
          later move to a charting library needs no change to how the data is
          produced (`dut-dashboard/CLAUDE.md`, the offline-first constraint).
          The shared component does it; this is not a second way of doing it. */}
      <ChartData id="offline-analyzer-chart-data" data={{ unit, series }} />
    </div>
  );
}

export default function OfflineAnalyzerSection() {
  const [duts, setDuts] = useState<StoredDut[]>([]);
  const [activeId, setActiveId] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [metric, setMetric] = useState<FieldKey>("cpu0");
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadOfflineDuts()
      .then((saved) => {
        setDuts(saved);
        setActiveId(saved[0]?.id ?? "");
        setSelectedIds(saved.map((dut) => dut.id));
      })
      .catch(() => setNotice("Saved logs could not be opened in this browser."))
      .finally(() => setLoading(false));
  }, []);

  const activeDut = duts.find((dut) => dut.id === activeId) ?? duts[0];
  const metricField = NUMERIC_LOG_FIELDS.find((field) => field.key === metric) ?? NUMERIC_LOG_FIELDS[0];
  const series = useMemo<ChartSeries[]>(
    () => duts.filter((dut) => selectedIds.includes(dut.id)).map((dut) => ({
      id: dut.id,
      name: dut.name,
      values: dut.rows.flatMap((row) => {
        const x = row.testNumber;
        const y = row[metric];
        return typeof x === "number" && typeof y === "number" ? [{ x, y }] : [];
      }),
    })).filter((item) => item.values.length > 0),
    [duts, metric, selectedIds],
  );

  async function importFiles(files: File[]) {
    const accepted = files.filter((file) => /\.(log|txt)$/i.test(file.name) || file.type === "text/plain");
    if (accepted.length === 0) {
      setNotice("Choose one or more .log or .txt files.");
      return;
    }
    // Which of these files become DUTs, and what to say about the ones that do
    // not, is decided in `offline/importPlan.ts` — a plain function a test can
    // call. Here: read the files, save what the plan chose, move the selection.
    let plan: ImportPlan;
    try {
      const parsed: ParsedLogFile[] = [];
      for (const file of accepted) {
        parsed.push({ name: file.name, result: parseLog(await file.text()) });
      }
      plan = planImport(parsed, duts, { id: () => crypto.randomUUID(), now: () => Date.now() });
      for (const dut of plan.records) {
        await saveOfflineDut(dut);
      }
    } catch {
      setNotice("The logs could not be saved. Check this browser's storage permission and available space.");
      return;
    }
    setNotice(plan.notice);
    if (plan.records.length === 0) return;
    setDuts([...duts, ...plan.records]);
    setSelectedIds((ids) => [...ids, ...plan.records.map((dut) => dut.id)]);
    setActiveId(plan.records[plan.records.length - 1].id);
  }

  function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    void importFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void importFiles(Array.from(event.dataTransfer.files));
  }

  function renameActive(name: string) {
    if (!activeDut) return;
    const renamed = { ...activeDut, name };
    setDuts((items) => items.map((dut) => dut.id === activeDut.id ? renamed : dut));
  }

  function saveActiveName(name: string) {
    if (!activeDut) return;
    void saveOfflineDut({ ...activeDut, name }).catch(() => setNotice("The DUT name could not be saved."));
  }

  async function deleteActive() {
    if (!activeDut || !window.confirm(`Delete ${activeDut.name}?`)) return;
    try {
      await removeOfflineDut(activeDut.id);
    } catch {
      // The record is still there, so the list must still show it. Dropping it
      // from the view would report a deletion that did not happen, and the DUT
      // would be back on the next reload with no explanation.
      setNotice(`${activeDut.name} could not be deleted from this browser's storage.`);
      return;
    }
    const next = duts.filter((dut) => dut.id !== activeDut.id);
    setDuts(next);
    setSelectedIds((ids) => ids.filter((id) => id !== activeDut.id));
    setActiveId(next[0]?.id ?? "");
  }

  if (loading) {
    return <EmptyState icon="⌁" message="Opening saved logs…" />;
  }

  if (duts.length === 0) {
    return (
      <div
        className={`offline-dropzone${dragging ? " dragging" : ""}`}
        onDragEnter={() => setDragging(true)}
        onDragLeave={() => setDragging(false)}
        onDragOver={(event) => event.preventDefault()}
        onDrop={handleDrop}
      >
        <div className="offline-drop-icon">⇧</div>
        <h2>Import DUT logs</h2>
        <p>Drop one or more .log or .txt files here. Processing stays on this device.</p>
        <label className="btn primary offline-file-button">Choose logs<input type="file" accept=".log,.txt,text/plain" multiple onChange={handleFiles} /></label>
        {notice ? <p className="offline-notice" role="status">{notice}</p> : null}
      </div>
    );
  }

  return (
    <div className="offline-analyzer">
      <div className="offline-summary">
        <div>
          <span className="pill ok"><span className="dot" />Local only</span>
          <h2>Offline log workspace</h2>
          <p>Compare saved DUT logs without changing the live monitoring session.</p>
        </div>
        <label className="btn primary offline-file-button">Import logs<input type="file" accept=".log,.txt,text/plain" multiple onChange={handleFiles} /></label>
      </div>
      {notice ? <div className="offline-notice" role="status">{notice}</div> : null}
      <div className="kpis offline-kpis">
        <KpiCard label="Saved DUTs" value={String(duts.length)} sub="Stored in this browser" />
        <KpiCard label="Active samples" value={String(activeDut?.rows.length ?? 0)} sub={activeDut?.sourceFile ?? "No file"} />
        <KpiCard label="Missing values" value={String(activeDut?.missing ?? 0)} sub="Displayed as N/A" />
        <KpiCard label="Compared DUTs" value={String(selectedIds.length)} sub={metricField.label} />
      </div>
      <Card title="DUT logs" subtitle="Select a log to inspect or update its display name">
        <div className="offline-tabs" role="tablist" aria-label="Imported DUT logs">
          {duts.map((dut) => <button type="button" role="tab" aria-selected={dut.id === activeDut?.id} className={dut.id === activeDut?.id ? "active" : ""} key={dut.id} onClick={() => setActiveId(dut.id)}>{dut.name}</button>)}
        </div>
        {activeDut ? <div className="offline-name-row"><label>DUT name<input value={activeDut.name} maxLength={80} onChange={(event) => renameActive(event.target.value)} onBlur={(event) => saveActiveName(event.currentTarget.value)} /></label><button type="button" className="btn danger-btn" onClick={() => void deleteActive()}>Delete DUT</button></div> : null}
      </Card>
      <Card title="DUT comparison" subtitle="Overlay one metric across selected DUT logs">
        <div className="offline-chart-controls">
          <label>Metric<select value={metric} onChange={(event) => setMetric(event.target.value as FieldKey)}>{NUMERIC_LOG_FIELDS.map((field) => <option key={field.key} value={field.key}>{field.label} ({field.unit})</option>)}</select></label>
          <fieldset><legend>Compare DUTs</legend>{duts.map((dut) => <label key={dut.id}><input type="checkbox" checked={selectedIds.includes(dut.id)} onChange={(event) => setSelectedIds((ids) => event.target.checked ? [...ids, dut.id] : ids.filter((id) => id !== dut.id))} />{dut.name}</label>)}</fieldset>
        </div>
        <OfflineChart series={series} unit={metricField.unit} />
      </Card>
      <Card title="Parsed samples" subtitle={`${activeDut?.sourceFile ?? ""} · ${activeDut?.rows.length ?? 0} rows`}>
        <div className="offline-table-wrap"><table className="filetable offline-table"><thead><tr>{LOG_FIELDS.map((field) => <th key={field.key}>{field.label}{field.unit ? ` (${field.unit})` : ""}</th>)}</tr></thead><tbody>{activeDut?.rows.map((row, index) => <tr key={index}>{LOG_FIELDS.map((field) => <td className={row[field.key] === null ? "na" : ""} key={field.key}>{row[field.key] ?? "N/A"}</td>)}</tr>)}</tbody></table></div>
      </Card>
    </div>
  );
}
