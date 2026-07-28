import { lazy, Suspense, useEffect, useState } from "react";

import { getMemory, MemorySeries, WifiClientsResult } from "../api/rest";
import ChartData from "../components/charts/ChartData";
import Sparkline from "../components/charts/Sparkline";
import DutSwitcher from "../components/DutSwitcher";
import { DEFAULT_DUT_ID } from "../api/dut";
import { applyAccent, loadSettings } from "../monitoring/useSettings";
import { Card, EmptyState, KpiCard } from "../components/shell/Card";
import InviteRedeemDialog from "../components/InviteRedeemDialog";
import LoginDialog from "../components/LoginDialog";
import Sidebar from "../components/shell/Sidebar";
import Topbar from "../components/shell/Topbar";
import { canAccess, NAV_ITEMS, SectionId } from "../components/shell/navigation";
import { AuthProvider, useAuth } from "../monitoring/AuthContext";
import { useAppVersion } from "../monitoring/useAppVersion";
import { DutMonitorProvider } from "../monitoring/DutMonitorContext";
import { DutMonitorState, DutStatus, useDutMonitor } from "../monitoring/useDutMonitor";
import { useWifiScan, wifiScanForDut, WifiScanProvider } from "../monitoring/WifiScanContext";
import { runSurvey } from "../monitoring/siteSurveyStore";
import { OverviewBandReco } from "../components/BandRecoSummary";

// Heavy sections are loaded on demand so the initial bundle only carries the
// app shell + the default Overview (charts). Each becomes its own async chunk.
// The Serial Console (Dashboard) pulls in CodeMirror, so deferring it keeps the
// editor out of first paint; it stays mounted once first opened (see below).
const BulletinSection = lazy(() => import("../components/BulletinSection"));
const WorkspaceSearchResults = lazy(() => import("../components/WorkspaceSearchResults"));
const FleetStrip = lazy(() => import("../components/FleetStrip"));
const DownloadsSection = lazy(() => import("../components/DownloadsSection"));
const FilesSection = lazy(() => import("../components/FilesSection"));
const SettingsSection = lazy(() => import("../components/SettingsSection"));
const WifiClientsCard = lazy(() => import("../components/WifiClientsCard"));
const SsidCapabilityCard = lazy(() => import("../components/SsidCapabilityCard"));
const SiteSurveyCard = lazy(() => import("../components/SiteSurveyCard"));
const Dashboard = lazy(() => import("./Dashboard"));

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
  // Session state wraps the whole shell: Sidebar filters nav by role and the
  // toolbar shows the identity chip. Guest-by-default — no blocking gate.
  return (
    <AuthProvider>
      <AppShellInner />
    </AuthProvider>
  );
}

function AppShellInner() {
  const [active, setActive] = useState<SectionId>("overview");
  const { user, role, logout } = useAuth();
  const [loginOpen, setLoginOpen] = useState(false);
  // Invite token lifted out of the URL on first render (see the effect below).
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  // Mobile nav drawer (off-canvas). Inert on desktop — the sidebar is always
  // visible there and the hamburger that toggles this is hidden via CSS.
  const [navOpen, setNavOpen] = useState(false);
  const [search, setSearch] = useState("");
  // Workspace tag search (P70): non-null while the combined Files+Bulletin
  // results panel is open. Set by submitting the search box on Files/Bulletin
  // or clicking any tag chip; cleared by Close or switching sections.
  const [wsSearch, setWsSearch] = useState<string | null>(null);
  const [selectedDut, setSelectedDut] = useState(DEFAULT_DUT_ID);
  // One monitor for the selected DUT drives everything: the sections, the topbar
  // status, and the Serial Console (via context) — all follow the switcher.
  const monitor = useDutMonitor(selectedDut);
  const current = NAV_ITEMS.find((item) => item.id === active) ?? NAV_ITEMS[0];
  // The Serial Console (Dashboard) is lazy-loaded, but must stay mounted once
  // opened so its serial session, port, and terminal state persist across nav.
  // Mount it on the first visit to the console, then keep it mounted (hidden
  // via display:none) — there is no serial session before that first visit.
  const [consoleLoaded, setConsoleLoaded] = useState(false);
  // Detect a backend redeploy from this open tab and offer a reload.
  const { updateAvailable, dismiss } = useAppVersion();

  // Apply the saved accent on load so the theme persists across reloads.
  useEffect(() => {
    applyAccent(loadSettings().accent);
  }, []);

  // Arriving from an invite QR: take the token into state and strip it from the
  // URL immediately, so a single-use credential does not linger in the address
  // bar, the history entry, or anything the user might screenshot or share.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get("invite");
    if (!token) {
      return;
    }
    setInviteToken(token);
    params.delete("invite");
    const query = params.toString();
    window.history.replaceState(
      {},
      "",
      window.location.pathname + (query ? `?${query}` : "") + window.location.hash,
    );
  }, []);

  useEffect(() => {
    if (active === "console") {
      setConsoleLoaded(true);
    }
  }, [active]);

  // Role guard: on logout/demotion, leave any section the new role cannot see
  // and unmount the Serial Console — it stays mounted across nav by design, so
  // without this a logged-out browser would keep holding the /ws/term socket.
  const activeAllowed = canAccess(NAV_ITEMS.find((item) => item.id === active) ?? NAV_ITEMS[0], role);
  useEffect(() => {
    if (!activeAllowed) {
      setActive("overview");
    }
    if (role === "guest") {
      setConsoleLoaded(false);
    }
  }, [activeAllowed, role]);

  return (
    <DutMonitorProvider value={monitor}>
      <WifiScanProvider>
      <div className="app">
        <Sidebar
          active={active}
          onSelect={(id) => {
            setActive(id);
            setNavOpen(false);
            setWsSearch(null);
          }}
          open={navOpen}
          onClose={() => setNavOpen(false)}
        />
        <div className="main">
          <Topbar
            title={current.title}
            subtitle={current.subtitle}
            onMenuClick={() => setNavOpen(true)}
            navOpen={navOpen}
            search={
              active === "logs" || active === "downloads" || active === "files" || active === "bulletin" ? (
                <SearchBox
                  value={search}
                  onChange={setSearch}
                  // Enter on Files/Bulletin opens the combined tag-search panel;
                  // live per-section substring filtering is unchanged.
                  onSubmit={
                    active === "files" || active === "bulletin"
                      ? (value) => setWsSearch(value.trim() || null)
                      : undefined
                  }
                />
              ) : undefined
            }
            actions={
            // Wrapper is transparent on desktop (display:contents) so the layout
            // is unchanged; under 720px it becomes the deliberate stacked column.
            <div className="toolbar-actions">
              <DutSwitcher selected={selectedDut} onSelect={setSelectedDut} />
              <ToolbarActions
                status={monitor.status}
                lastEventAgeSec={monitor.lastEventAgeSec}
                onConnect={() => setActive("console")}
              />
              {user ? (
                <span className="auth-chip">
                  <span className="auth-name" title={user.username}>
                    {user.display_name}
                  </span>
                  <span className={`pill role-${user.role}`}>{user.role}</span>
                  <button type="button" className="btn" onClick={() => void logout()}>
                    Logout
                  </button>
                </span>
              ) : (
                <button type="button" className="btn" onClick={() => setLoginOpen(true)}>
                  Login
                </button>
              )}
            </div>
          }
          />
          {inviteToken ? (
            <InviteRedeemDialog token={inviteToken} onClose={() => setInviteToken(null)} />
          ) : loginOpen ? (
            <LoginDialog onClose={() => setLoginOpen(false)} />
          ) : null}
          {updateAvailable ? (
            <UpdateBanner onReload={() => window.location.reload()} onDismiss={dismiss} />
          ) : null}
          <main className="content">
            {/* Serial Console stays mounted across nav so the serial session,
                selected port, log, and terminal state persist; it is only
                hidden when another section is active. Follows the selected DUT.
                Mounted lazily on first visit (defers the CodeMirror bundle). */}
            {consoleLoaded ? (
              <div className="embed" style={{ display: active === "console" ? "block" : "none" }}>
                <Suspense fallback={<SectionLoading />}>
                  <Dashboard
                    active={active === "console"}
                    dutId={selectedDut}
                    onSerialOpened={(id) => void runSurvey(id)}
                  />
                </Suspense>
              </div>
            ) : null}
            {active !== "console" ? (
              <Suspense fallback={<SectionLoading />}>
                {!activeAllowed ? (
                  // Transient guard (the effect above bounces to Overview next
                  // tick) and the honest answer if a section is ever reached
                  // above the viewer's role.
                  <EmptyState icon="🔒" message="Engineer login required" hint="Use Login in the toolbar." />
                ) : wsSearch !== null && (active === "files" || active === "bulletin") ? (
                  <WorkspaceSearchResults
                    query={wsSearch}
                    onTagClick={setWsSearch}
                    onClose={() => setWsSearch(null)}
                  />
                ) : (
                  renderSection(
                    active,
                    monitor,
                    search,
                    selectedDut,
                    setSelectedDut,
                    (id) => {
                      setSelectedDut(id);
                      setActive("console");
                    },
                    setActive,
                    setWsSearch,
                  )
                )}
              </Suspense>
            ) : null}
          </main>
        </div>
      </div>
      </WifiScanProvider>
    </DutMonitorProvider>
  );
}

/** Lightweight placeholder shown while a lazy section chunk loads. */
function SectionLoading() {
  return <EmptyState icon="⏳" message="Loading…" />;
}

/** Top banner shown when the backend has been redeployed under an open tab. */
function UpdateBanner({ onReload, onDismiss }: { onReload: () => void; onDismiss: () => void }) {
  return (
    <div className="update-banner" role="status">
      <span className="pill warn">
        <span className="dot" />
        New version available
      </span>
      <span className="update-banner-text">A newer dashboard was deployed — reload to get the latest.</span>
      <span className="update-banner-actions">
        <button type="button" className="btn primary" onClick={onReload}>
          Reload
        </button>
        <button type="button" className="btn" onClick={onDismiss} aria-label="Dismiss update notice">
          ✕
        </button>
      </span>
    </div>
  );
}

function renderSection(
  active: SectionId,
  monitor: DutMonitorState,
  search: string,
  selectedDut: string,
  onSelectDut: (dutId: string) => void,
  onOpenConsole: (dutId: string) => void,
  onNavigate: (id: SectionId) => void,
  onTagSearch: (tag: string) => void,
) {
  switch (active) {
    case "overview":
      return (
        <OverviewSection
          monitor={monitor}
          selectedDut={selectedDut}
          onSelectDut={onSelectDut}
          onOpenConsole={onOpenConsole}
          onOpenSiteSurvey={() => onNavigate("sitesurvey")}
        />
      );
    case "console":
      // Rendered separately (always mounted) so its session/state persists.
      return null;
    case "cpu":
      return (
        <div className="grid">
          <Card title="CPU trend" subtitle="Per-core busy % over time">
            <CpuTrendBody monitor={monitor} />
          </Card>
          <Card title="Per-core CPU" subtitle="Current busy % by core">
            <PerCoreCpuBody monitor={monitor} />
          </Card>
          <Card title="Memory trend" subtitle="Effective available — live or post-analysis">
            <MemoryTrendBody monitor={monitor} />
          </Card>
        </div>
      );
    case "wifi":
      // Scan-driven: WifiClientsCard auto-scans on entry and shows both the
      // per-band summary and the detail (one authoritative source). The live
      // sysMon WifiSummaryBody stays on the Overview card.
      return <WifiClientsCard dutId={selectedDut} />;
    case "ssid":
      return <SsidCapabilityCard dutId={selectedDut} />;
    case "sitesurvey":
      return <SiteSurveyCard dutId={selectedDut} />;
    case "logs":
      return (
        <Card title="Logs / Crash events" subtitle="Critical crash + log event detection">
          <CrashEventsBody monitor={monitor} />
        </Card>
      );
    case "downloads":
      return <DownloadsSection query={search} />;
    case "files":
      return <FilesSection query={search} onTagClick={onTagSearch} />;
    case "bulletin":
      return <BulletinSection query={search} onTagClick={onTagSearch} />;
    case "settings":
      return <SettingsSection />;
    default:
      return null;
  }
}

function OverviewSection({
  monitor,
  selectedDut,
  onSelectDut,
  onOpenConsole,
  onOpenSiteSurvey,
}: {
  monitor: DutMonitorState;
  selectedDut: string;
  onSelectDut: (dutId: string) => void;
  onOpenConsole: (dutId: string) => void;
  onOpenSiteSurvey: () => void;
}) {
  const statusMeta = STATUS_META[monitor.status];
  const cpuValue = monitor.cpuBusyPct === null ? undefined : `${monitor.cpuBusyPct}%`;
  const cpuSub =
    monitor.cpuBusyPct === null
      ? "Awaiting snapshot"
      : `idle ${monitor.cpuIdlePct}% · ${monitor.coreCount} core${monitor.coreCount === 1 ? "" : "s"}`;

  // Wi-Fi Clients reflects the real wlanconfig scan (clients.length, per band)
  // from the shared cache — not the passive sysMon total, which reads "—" on
  // many APs. The scan is on-demand: triggered by the "Scan" button below or by
  // entering the Wi-Fi section; never background-polled.
  const wifi = useWifiScan();
  const { result: wifiScan, loading: wifiLoading, error: wifiError } = wifiScanForDut(wifi, selectedDut);
  const wifiValue = wifiScan ? String(wifiScan.clients.length) : undefined;
  const wifiSub = wifiScan
    ? wifiBandSummary(wifiScan)
    : wifiLoading
    ? "Scanning…"
    : wifiError
    ? "Needs open serial"
    : "Press Scan to count";

  return (
    <>
      {/* Phase 69: the fleet strip lives at the top of Overview (its own nav
          section was removed). Its own Suspense with a null fallback keeps the
          rest of Overview painting immediately; the strip hides itself when the
          fleet has one DUT or fewer. */}
      <Suspense fallback={null}>
        <FleetStrip onSelectDut={onSelectDut} onOpenConsole={onOpenConsole} />
      </Suspense>

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
        <Card title="Memory trend" subtitle="Effective available — live or post-analysis">
          <MemoryTrendBody monitor={monitor} />
        </Card>
        <Card
          title="Wi-Fi client summary"
          subtitle="Associated clients per band (wlanconfig)"
          actions={
            <button type="button" className="btn" onClick={() => void wifi.scan(selectedDut)} disabled={wifiLoading}>
              {wifiLoading ? "Scanning…" : "Scan"}
            </button>
          }
        >
          <WifiSummaryBody result={wifiScan} loading={wifiLoading} error={wifiError} status={monitor.status} />
        </Card>
        <Card
          title="Channel recommendation"
          subtitle="Least-occupied channel per band (last survey)"
          actions={
            <button type="button" className="btn" onClick={onOpenSiteSurvey}>
              Site Survey
            </button>
          }
        >
          <OverviewBandReco dutId={selectedDut} />
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
          <dt>Last event</dt>
          <dd>{monitor.lastEventAgeSec === null ? "—" : `${monitor.lastEventAgeSec}s ago`}</dd>
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

const toMb = (kb: number) => (kb / 1024).toFixed(0);

function MemoryTrendBody({ monitor }: { monitor: DutMonitorState }) {
  // Prefer live /proc/meminfo parsed from the stream (updates once per snapshot,
  // like CPU); fall back to the post-analysis CSV when the DUT isn't streaming
  // memory or before the first sample arrives.
  if (monitor.memoryLive && monitor.memoryHistory.length > 0) {
    return <LiveMemoryBody monitor={monitor} />;
  }
  return <PostAnalysisMemoryBody />;
}

function LiveMemoryBody({ monitor }: { monitor: DutMonitorState }) {
  const latest = monitor.memoryLive!;
  const effective = monitor.memoryHistory
    .map((point) => point.effectiveKb)
    .filter((value): value is number => value !== null);
  // Normalise to the series' own range so the trend is visible (Sparkline plots
  // 0..max); absolute MB values are shown in the footer.
  const min = Math.min(...effective);
  const max = Math.max(...effective);
  const span = max - min || 1;
  const normalised = effective.map((value) => ((value - min) / span) * 100);

  return (
    <div className="chart">
      <div className="chart-figure">
        <Sparkline values={normalised} max={100} ariaLabel="Live effective available memory trend" />
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {latest.effectiveKb === null ? "—" : toMb(latest.effectiveKb)}
          <span className="unit">MB effective avail · live</span>
        </div>
        <div className="chart-legend">
          {latest.memAvailableKb !== null ? (
            <span>
              <span className="swatch" />
              MemAvail {toMb(latest.memAvailableKb)} MB
            </span>
          ) : null}
          {latest.slabKb !== null ? <span>Slab {toMb(latest.slabKb)} MB</span> : null}
          <span style={{ color: "var(--faint)" }}>live · per snapshot</span>
        </div>
      </div>
      <ChartData id="memory-trend-data" data={monitor.memoryHistory} />
    </div>
  );
}

function PostAnalysisMemoryBody() {
  const [series, setSeries] = useState<MemorySeries | null>(null);
  const [failed, setFailed] = useState(false);
  const { role } = useAuth();

  const load = () => {
    setFailed(false);
    getMemory(500)
      .then(setSeries)
      .catch(() => setFailed(true));
  };
  useEffect(() => {
    load();
  }, []);

  if (failed) {
    // The analyzer feed is engineer-gated, so for a guest the failure is the
    // expected 403/401 — say so instead of blaming the backend.
    return role === "guest" ? (
      <EmptyState icon="🔒" message="Post-analysis memory needs an engineer login" hint="Live memory still streams here while the DUT runs sysMon." />
    ) : (
      <EmptyState icon="🧠" message="Could not load memory data" hint="Is the backend reachable?" />
    );
  }
  if (!series) {
    return <EmptyState icon="🧠" message="Loading…" />;
  }
  if (!series.available || series.points.length === 0) {
    return (
      <EmptyState
        icon="🧠"
        message="No memory data yet"
        hint="Post-analysis only — download a DUT log (or run the analyzer) to populate this."
      />
    );
  }

  // Memory values are large; normalise the effective-available series to its own
  // range so the leak trend is visible (Sparkline plots 0..max). Absolute values
  // are shown in the footer; raw points go in the JSON blob.
  const effective = series.points.map((point) => point.effectiveKb);
  const min = Math.min(...effective);
  const max = Math.max(...effective);
  const span = max - min || 1;
  const normalised = effective.map((value) => ((value - min) / span) * 100);
  const latest = series.points[series.points.length - 1];

  return (
    <div className="chart">
      <div className="chart-figure">
        <Sparkline values={normalised} max={100} ariaLabel="Effective available memory trend" />
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {toMb(latest.effectiveKb)}
          <span className="unit">MB effective avail · post-analysis</span>
        </div>
        <div className="chart-legend">
          <span>
            <span className="swatch" />
            MemAvail {toMb(latest.memAvailableKb)} MB
          </span>
          <span>Slab {toMb(latest.slabKb)} MB</span>
          {series.generated_at ? (
            <span style={{ color: "var(--faint)" }}>as of {series.generated_at}</span>
          ) : null}
          <button type="button" className="btn" style={{ padding: "2px 8px" }} onClick={load} title="Refresh from analyzer output">
            ↻
          </button>
        </div>
      </div>
      <ChartData id="memory-trend-data" data={series.points} />
    </div>
  );
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

function PerCoreCpuBody({ monitor }: { monitor: DutMonitorState }) {
  const cores = Object.entries(monitor.cpuPerCoreBusy).sort(([a], [b]) => a.localeCompare(b));
  if (monitor.status === "offline" && cores.length === 0) {
    return <OfflineState />;
  }
  if (cores.length === 0) {
    return <EmptyState icon="📈" message="No snapshot data yet" hint="Open a DUT to stream CPU snapshots." />;
  }
  return (
    <div className="chart">
      <div className="barrows">
        {cores.map(([core, busy]) => (
          <div className="barrow" key={core}>
            <div className="barrow-label">CPU{core}</div>
            <div className="barrow-track">
              <div className="barrow-fill" style={{ width: `${Math.min(100, Math.max(0, busy))}%` }} />
            </div>
            <div className="barrow-value">{busy}%</div>
          </div>
        ))}
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {monitor.coreCount}
          <span className="unit">core{monitor.coreCount === 1 ? "" : "s"}</span>
        </div>
      </div>
    </div>
  );
}

const WIFI_BAND_ORDER: Record<string, number> = { "2.4G": 0, "5G": 1, "6G": 2 };

/** Per-band client counts from a scan result, in 2.4→5→6 (then other) order. */
function wifiBandCounts(result: WifiClientsResult): [string, number][] {
  const counts: Record<string, number> = {};
  for (const client of result.clients) {
    counts[client.band] = (counts[client.band] ?? 0) + 1;
  }
  return Object.entries(counts).sort(
    ([a], [b]) => (WIFI_BAND_ORDER[a] ?? 99) - (WIFI_BAND_ORDER[b] ?? 99) || a.localeCompare(b),
  );
}

/** Compact "2.4G:1 · 6G:1" summary for the KPI sub-line. */
function wifiBandSummary(result: WifiClientsResult): string {
  const counts = wifiBandCounts(result);
  if (counts.length === 0) {
    return "0 clients";
  }
  return counts.map(([band, count]) => `${band}: ${count}`).join(" · ");
}

/**
 * Scan-driven Wi-Fi summary: real associated-client counts per band from the
 * shared `wlanconfig` scan cache (not the passive sysMon total). On-demand only.
 */
function WifiSummaryBody({
  result,
  loading,
  error,
  status,
}: {
  result: WifiClientsResult | null;
  loading: boolean;
  error: string;
  status: DutStatus;
}) {
  if (result === null) {
    if (loading) {
      return <EmptyState icon="📶" message="Scanning clients…" hint="Running wlanconfig on each active VAP." />;
    }
    if (error) {
      return <EmptyState icon="📶" message="No client scan" hint={error} />;
    }
    if (status === "offline") {
      return <OfflineState />;
    }
    return <EmptyState icon="📶" message="No scan yet" hint="Press Scan to count associated clients (real DUT serial only)." />;
  }
  const bands = wifiBandCounts(result);
  if (bands.length === 0) {
    return (
      <EmptyState
        icon="📶"
        message="No associated clients"
        hint={`Scanned ${result.vaps.length} VAP(s) at ${result.captured_at}.`}
      />
    );
  }
  const max = Math.max(1, ...bands.map(([, count]) => count));
  return (
    <div className="chart">
      <div className="barrows">
        {bands.map(([band, count]) => (
          <div className="barrow" key={band}>
            <div className="barrow-label">{band}</div>
            <div className="barrow-track">
              <div className="barrow-fill" style={{ width: `${(count / max) * 100}%` }} />
            </div>
            <div className="barrow-value">{count}</div>
          </div>
        ))}
      </div>
      <div className="chart-foot">
        <div className="chart-metric">
          {result.clients.length}
          <span className="unit">clients · scanned {result.captured_at}</span>
        </div>
      </div>
      <ChartData id="wifi-summary-data" data={{ bands, captured_at: result.captured_at }} />
    </div>
  );
}

/** Copy text to the clipboard; falls back to execCommand because LAN access
 * over plain http://<ip> is not a secure context (no navigator.clipboard). */
async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the legacy path.
    }
  }
  const holder = document.createElement("textarea");
  holder.value = text;
  holder.style.position = "fixed";
  holder.style.opacity = "0";
  document.body.appendChild(holder);
  holder.select();
  const ok = document.execCommand("copy");
  holder.remove();
  return ok;
}

function downloadTextFile(filename: string, mime: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function crashCsv(lines: string[]): string {
  const rows = lines.map((line) => `"${line.replace(/"/g, '""')}"`);
  return ["line", ...rows].join("\r\n") + "\r\n";
}

function crashExportName(ext: string): string {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15);
  return `crash-events-${stamp}.${ext}`;
}

function CrashEventsBody({ monitor }: { monitor: DutMonitorState }) {
  const [copyState, setCopyState] = useState<"idle" | "ok" | "fail">("idle");
  if (monitor.status === "offline" && monitor.crashLines.length === 0) {
    return <OfflineState />;
  }
  if (monitor.crashLines.length === 0) {
    return (
      <EmptyState icon="⚠" message="No crash events detected" hint="Built-in: kernel panic / Q6 crash / watchdog." />
    );
  }
  // Exports carry ALL matched lines, not just the 12 rendered below.
  const all = monitor.crashLines;
  const recent = all.slice(-12).reverse();
  const copy = () => {
    void copyToClipboard(all.join("\n")).then((ok) => {
      setCopyState(ok ? "ok" : "fail");
      setTimeout(() => setCopyState("idle"), 2000);
    });
  };
  return (
    <div className="chart">
      <div className="chart-foot">
        <div className="chart-metric">
          {monitor.crashCount}
          <span className="unit">critical matches</span>
        </div>
        <div className="row-actions">
          <button type="button" className="btn" title={`Copy all ${all.length} lines`} onClick={copy}>
            {copyState === "ok" ? "Copied ✓" : copyState === "fail" ? "Copy failed" : "Copy"}
          </button>
          <button
            type="button"
            className="btn"
            title={`Download all ${all.length} lines as .txt`}
            onClick={() => downloadTextFile(crashExportName("txt"), "text/plain", all.join("\n") + "\n")}
          >
            .txt
          </button>
          <button
            type="button"
            className="btn"
            title={`Download all ${all.length} lines as .csv`}
            onClick={() => downloadTextFile(crashExportName("csv"), "text/csv", crashCsv(all))}
          >
            .csv
          </button>
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

function SearchBox({
  value,
  onChange,
  onSubmit,
}: {
  value: string;
  onChange: (next: string) => void;
  onSubmit?: (value: string) => void;
}) {
  return (
    <form
      className="search"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit?.(value);
      }}
    >
      <span aria-hidden>🔍</span>
      <input
        type="search"
        placeholder={onSubmit ? "Filter… (Enter = tag search)" : "Filter…"}
        aria-label="Filter"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </form>
  );
}

function ToolbarActions({
  status,
  lastEventAgeSec,
  onConnect,
}: {
  status: DutStatus;
  lastEventAgeSec: number | null;
  onConnect: () => void;
}) {
  const statusMeta = STATUS_META[status];
  const age = formatEventAge(lastEventAgeSec);
  return (
    <>
      {/* Status cluster is transparent on desktop (display:contents); under 720px
          it becomes one row (age left, pill right) above the Connect button. */}
      <div className="toolbar-status">
        {age && status !== "offline" ? <span className="toolbar-sub">{age}</span> : null}
        <span className={`pill ${statusMeta.pill}`} title="Backend link + DUT stream status">
          <span className="dot" />
          {statusMeta.label}
        </span>
      </div>
      <button type="button" className="btn primary" onClick={onConnect}>
        Connect DUT
      </button>
    </>
  );
}

function formatEventAge(seconds: number | null): string | null {
  if (seconds === null) {
    return null;
  }
  if (seconds < 60) {
    return `last event ${seconds}s ago`;
  }
  return `last event ${Math.floor(seconds / 60)}m ago`;
}

type StatusMeta = { label: string; sub: string; pill: "ok" | "idle" | "danger" };

const STATUS_META: Record<DutStatus, StatusMeta> = {
  streaming: { label: "Streaming", sub: "Receiving DUT data", pill: "ok" },
  idle: { label: "No DUT", sub: "Backend up, no stream", pill: "idle" },
  offline: { label: "Offline", sub: "Backend not reachable", pill: "danger" },
};

