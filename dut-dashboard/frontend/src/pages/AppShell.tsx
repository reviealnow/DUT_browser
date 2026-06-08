import { useState } from "react";

import ChartData from "../components/charts/ChartData";
import Sparkline from "../components/charts/Sparkline";
import { Card, EmptyState, KpiCard } from "../components/shell/Card";
import Sidebar from "../components/shell/Sidebar";
import Topbar from "../components/shell/Topbar";
import { NAV_ITEMS, SectionId } from "../components/shell/navigation";
import { DutMonitorProvider } from "../monitoring/DutMonitorContext";
import { DutMonitorState, DutStatus, useDutMonitor } from "../monitoring/useDutMonitor";
import Dashboard from "./Dashboard";

const PHASE3_HINT = "Trend charts and live views arrive in Phase 3.";

/**
 * App shell.
 *
 * The shell renders the Luna-spacing design system around the EXISTING
 * Dashboard, which is embedded unchanged under "Serial Console" so all
 * current behavior (serial console, crash panel, log download) is preserved.
 *
 * Phase 2: the KPI row, the toolbar status pill, and the Overview "Serial
 * console status" card are wired to real data from the existing /ws stream
 * (see useDutMonitor). No backend changes; no metrics invented.
 */
export default function AppShell() {
  const [active, setActive] = useState<SectionId>("overview");
  const monitor = useDutMonitor();
  const current = NAV_ITEMS.find((item) => item.id === active) ?? NAV_ITEMS[0];

  return (
    <DutMonitorProvider value={monitor}>
      <div className="app">
        <Sidebar active={active} onSelect={setActive} />
        <div className="main">
          <Topbar
            title={current.title}
            subtitle={current.subtitle}
            search={active === "logs" || active === "downloads" ? <SearchBox /> : undefined}
            actions={<ToolbarActions status={monitor.status} onConnect={() => setActive("console")} />}
          />
          <main className="content">{renderSection(active, monitor)}</main>
        </div>
      </div>
    </DutMonitorProvider>
  );
}

function renderSection(active: SectionId, monitor: DutMonitorState) {
  switch (active) {
    case "overview":
      return <OverviewSection monitor={monitor} />;
    case "console":
      // Existing Dashboard, untouched — preserves all current behavior.
      return (
        <div className="embed">
          <Dashboard />
        </div>
      );
    case "cpu":
      return (
        <Card title="CPU trend" subtitle="Per-core busy % over time (memory is post-analysis only)">
          <CpuTrendBody monitor={monitor} />
        </Card>
      );
    case "wifi":
      return (
        <Card title="Wi-Fi clients" subtitle="Associated clients by radio">
          <WifiSummaryBody monitor={monitor} />
        </Card>
      );
    case "logs":
      return (
        <Card title="Logs / Crash events" subtitle="Critical crash + log event detection">
          <CrashEventsBody monitor={monitor} />
        </Card>
      );
    case "downloads":
      return (
        <Card title="Downloads" subtitle="Log bundles and analyzer artifacts">
          <EmptyState icon="⬇" message="No downloads yet" hint={PHASE3_HINT} />
        </Card>
      );
    case "settings":
      return (
        <Card title="Settings" subtitle="Dashboard configuration">
          <EmptyState icon="⚙" message="No settings yet" hint="Configuration options arrive in a later phase." />
        </Card>
      );
    default:
      return null;
  }
}

function OverviewSection({ monitor }: { monitor: DutMonitorState }) {
  const statusMeta = STATUS_META[monitor.status];
  const cpuValue = monitor.cpuBusyPct === null ? undefined : `${monitor.cpuBusyPct}%`;
  const cpuSub =
    monitor.cpuBusyPct === null
      ? "Awaiting snapshot"
      : `idle ${monitor.cpuIdlePct}% · ${monitor.coreCount} core${monitor.coreCount === 1 ? "" : "s"}`;
  const wifiValue = monitor.wifiClientTotal === null ? undefined : String(monitor.wifiClientTotal);
  const wifiSub = monitor.wifiClientTotal === null ? "Awaiting clients" : radioBreakdown(monitor.wifiByRadio);

  return (
    <>
      <div className="kpis">
        <KpiCard label="DUT Status" value={statusMeta.label} sub={statusMeta.sub} />
        <KpiCard label="Latest CPU" value={cpuValue} sub={cpuSub} />
        <KpiCard label="Wi-Fi Clients" value={wifiValue} sub={wifiSub} />
        <KpiCard label="Crash Events" value={String(monitor.crashCount)} sub="kernel panic / Q6 / watchdog" />
      </div>

      <div className="grid">
        <Card title="CPU trend" subtitle="Per-core busy % over time">
          <CpuTrendBody monitor={monitor} />
        </Card>
        <Card title="Memory trend" subtitle="From analyzer output">
          <EmptyState icon="🧠" message="No memory data yet" hint="Memory is post-analysis only (analyzer bundle)." />
        </Card>
        <Card title="Wi-Fi client summary" subtitle="Clients per radio">
          <WifiSummaryBody monitor={monitor} />
        </Card>
        <Card title="Critical crash / log events" subtitle="Live keyword detection">
          <CrashEventsBody monitor={monitor} />
        </Card>
        <Card title="Serial console status" subtitle="Connection + parser state">
          <ConsoleStatusBody monitor={monitor} />
        </Card>
        <Card title="Recent logs / downloads" subtitle="Latest artifacts">
          <EmptyState icon="🗂" message="No recent activity" hint={PHASE3_HINT} />
        </Card>
      </div>
    </>
  );
}

function ConsoleStatusBody({ monitor }: { monitor: DutMonitorState }) {
  const statusMeta = STATUS_META[monitor.status];
  return (
    <div style={{ display: "grid", gap: "var(--space-3)" }}>
      <span className={`pill ${statusMeta.pill}`} style={{ alignSelf: "flex-start" }}>
        <span className="dot" />
        {statusMeta.label}
      </span>
      <dl className="stat-list">
        <div className="stat-row">
          <dt>Detected cores</dt>
          <dd>{monitor.coreCount > 0 ? monitor.coreCount : "—"}</dd>
        </div>
        <div className="stat-row">
          <dt>Last snapshot</dt>
          <dd>{monitor.lastSnapshotTs ?? "—"}</dd>
        </div>
        <div className="stat-row">
          <dt>Crash matches</dt>
          <dd>{monitor.crashCount}</dd>
        </div>
      </dl>
    </div>
  );
}

function OfflineState() {
  return <EmptyState icon="🔌" message="Backend not reachable" hint="Start the backend, then open a DUT." />;
}

function CpuTrendBody({ monitor }: { monitor: DutMonitorState }) {
  if (monitor.status === "offline" && monitor.cpuHistory.length === 0) {
    return <OfflineState />;
  }
  if (monitor.cpuHistory.length === 0) {
    return <EmptyState icon="📈" message="No snapshot data yet" hint="Open a DUT to stream CPU snapshots." />;
  }
  const values = monitor.cpuHistory.map((point) => point.busyPct);
  const cores = Object.entries(monitor.cpuPerCoreBusy).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="chart">
      <div className="chart-figure">
        <Sparkline values={values} ariaLabel="CPU busy percent trend" />
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {monitor.cpuBusyPct}
          <span className="unit">% busy</span>
        </div>
        <div className="chart-legend">
          {cores.map(([core, busy]) => (
            <span key={core}>
              <span className="swatch" />
              CPU{core} {busy}%
            </span>
          ))}
        </div>
      </div>
      <ChartData id="cpu-trend-data" data={monitor.cpuHistory} />
    </div>
  );
}

function WifiSummaryBody({ monitor }: { monitor: DutMonitorState }) {
  const entries = Object.entries(monitor.wifiByRadio).sort(([a], [b]) => a.localeCompare(b));
  if (monitor.status === "offline" && entries.length === 0) {
    return <OfflineState />;
  }
  if (monitor.wifiClientTotal === null || entries.length === 0) {
    return <EmptyState icon="📶" message="No associated clients yet" hint="Client counts appear once a snapshot reports radios." />;
  }
  const max = Math.max(1, ...entries.map(([, value]) => value));
  return (
    <div className="chart">
      <div className="barrows">
        {entries.map(([radio, count]) => (
          <div className="barrow" key={radio}>
            <div className="barrow-label">{radio}</div>
            <div className="barrow-track">
              <div className="barrow-fill" style={{ width: `${(count / max) * 100}%` }} />
            </div>
            <div className="barrow-value">{count}</div>
          </div>
        ))}
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {monitor.wifiClientTotal}
          <span className="unit">clients total</span>
        </div>
      </div>
      <ChartData id="wifi-summary-data" data={monitor.wifiByRadio} />
    </div>
  );
}

function CrashEventsBody({ monitor }: { monitor: DutMonitorState }) {
  if (monitor.status === "offline" && monitor.crashLines.length === 0) {
    return <OfflineState />;
  }
  if (monitor.crashLines.length === 0) {
    return (
      <EmptyState icon="⚠" message="No crash events detected" hint="Built-in: kernel panic / Q6 crash / watchdog." />
    );
  }
  const recent = monitor.crashLines.slice(-12).reverse();
  return (
    <div className="chart">
      <div className="chart-foot">
        <div className="chart-metric">
          {monitor.crashCount}
          <span className="unit">critical matches</span>
        </div>
      </div>
      <div className="feed">
        {recent.map((line, index) => (
          <div className="feed-item" key={`${index}-${line}`}>
            <span className="feed-dot" />
            {line}
          </div>
        ))}
      </div>
      <ChartData id="crash-events-data" data={{ count: monitor.crashCount, recent: monitor.crashLines }} />
    </div>
  );
}

function SearchBox() {
  // Phase 1: presentational only. Wired to real filtering in a later phase.
  return (
    <div className="search">
      <span aria-hidden>🔍</span>
      <input type="search" placeholder="Filter…" aria-label="Filter" />
    </div>
  );
}

function ToolbarActions({ status, onConnect }: { status: DutStatus; onConnect: () => void }) {
  const statusMeta = STATUS_META[status];
  return (
    <>
      <span className={`pill ${statusMeta.pill}`} title="Backend link + DUT stream status">
        <span className="dot" />
        {statusMeta.label}
      </span>
      <button type="button" className="btn primary" onClick={onConnect}>
        Connect DUT
      </button>
    </>
  );
}

type StatusMeta = { label: string; sub: string; pill: "ok" | "idle" | "danger" };

const STATUS_META: Record<DutStatus, StatusMeta> = {
  streaming: { label: "Streaming", sub: "Receiving DUT data", pill: "ok" },
  idle: { label: "No DUT", sub: "Backend up, no stream", pill: "idle" },
  offline: { label: "Offline", sub: "Backend not reachable", pill: "danger" },
};

function radioBreakdown(byRadio: Record<string, number>): string {
  const entries = Object.entries(byRadio);
  if (entries.length === 0) {
    return "No radios reported";
  }
  return entries
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([radio, count]) => `${radio}: ${count}`)
    .join(" · ");
}
