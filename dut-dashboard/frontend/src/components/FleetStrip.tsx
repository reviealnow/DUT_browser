import { useCallback, useState } from "react";

import {
  captureRemoteRssi,
  closeSerial,
  connectRemoteNode,
  disconnectRemoteNode,
  humanizeApiError,
  LastChannelRecommendationResult,
  openSerial,
  RemoteRssiResult,
} from "../api/rest";
import { hashHue } from "../monitoring/authorColor";
import { runConnectCaptures } from "../monitoring/siteSurveyStore";
import { DutStatus } from "../monitoring/useDutMonitor";
import { FleetEntry, useFleetMonitor } from "../monitoring/useFleetMonitor";
import { useFleetRecommendations } from "../monitoring/useLastRecommendation";
import { FleetBandBadge } from "./BandRecoSummary";

type StatusMeta = { label: string; pill: "ok" | "idle" | "danger" };

const STATUS_META: Record<DutStatus, StatusMeta> = {
  streaming: { label: "Streaming", pill: "ok" },
  idle: { label: "No DUT", pill: "idle" },
  offline: { label: "Offline", pill: "danger" },
};

function formatEventAge(seconds: number | null): string {
  if (seconds === null) {
    return "—";
  }
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  return `${Math.floor(seconds / 60)}m ago`;
}

/**
 * Phase 37 / 69: all registered DUTs side-by-side. Each card shows status /
 * latest CPU / crash count / last-event age from a single demuxed `/ws` (see
 * useFleetMonitor). Wi-Fi is intentionally absent — its serial scan is heavy and
 * stays single-DUT on-demand. Phase 66: per-card quick actions (jump to Serial
 * Console; close an open serial session); Phase 67: Connect a remembered DUT.
 *
 * Phase 69: rendered as a horizontal strip at the top of Overview (the dedicated
 * Fleet nav section was removed). Clicking a card selects that DUT — the caller
 * is already on Overview, so no navigation is needed. Hidden entirely when the
 * fleet has one DUT or fewer (nothing to switch between).
 */
export default function FleetStrip({
  onSelectDut,
  onOpenConsole,
}: {
  onSelectDut: (dutId: string) => void;
  onOpenConsole: (dutId: string) => void;
}) {
  const { fleet, refreshRegistry } = useFleetMonitor();
  // Per-DUT last-survey band recommendation, polled from the read-only cache
  // (no scan). Drives the compact per-card band badge.
  const recos = useFleetRecommendations(fleet.map((e) => e.id));

  // Single-DUT (or empty) users have nothing to switch between — hide the strip
  // so Overview isn't cluttered with a redundant one-card row.
  if (fleet.length <= 1) {
    return null;
  }

  return (
    <div className="fleet-strip">
      {fleet.map((entry) => (
        <FleetCard
          key={entry.id}
          entry={entry}
          reco={recos.get(entry.id)}
          onOpen={() => onSelectDut(entry.id)}
          onConsole={() => onOpenConsole(entry.id)}
          onClosed={refreshRegistry}
        />
      ))}
    </div>
  );
}

function FleetCard({
  entry,
  reco,
  onOpen,
  onConsole,
  onClosed,
}: {
  entry: FleetEntry;
  reco: LastChannelRecommendationResult | undefined;
  onOpen: () => void;
  onConsole: () => void;
  onClosed: () => Promise<void>;
}) {
  const [closing, setClosing] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rssi, setRssi] = useState<RemoteRssiResult | null>(() => entry.remote ? {
    dut: entry.id,
    applicable: entry.remote.isMesh,
    uplink: entry.remote.uplink,
    downlink: entry.remote.downlink,
  } : null);
  const [capturingRssi, setCapturingRssi] = useState(false);
  const meta = STATUS_META[entry.status];
  const cpu = entry.cpuBusyPct === null ? "—" : `${entry.cpuBusyPct}%`;
  const cpuSub =
    entry.cpuBusyPct === null
      ? "Awaiting snapshot"
      : `busy · ${entry.coreCount} core${entry.coreCount === 1 ? "" : "s"}`;

  const onCloseSerial = useCallback(() => {
    if (!window.confirm(`Close the serial session on ${entry.label}?`)) {
      return;
    }
    setClosing(true);
    setError(null);
    (entry.remote ? disconnectRemoteNode(entry.id) : closeSerial(entry.id))
      .then(() => onClosed())
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setClosing(false));
  }, [entry.id, entry.label, onClosed]);

  const lastSerial = entry.lastSerial;
  const onConnect = useCallback(() => {
    if (!lastSerial && !entry.remote) {
      return;
    }
    setConnecting(true);
    setError(null);
    // Reopen with the remembered params. On success refresh the registry (so the
    // card flips to the open/Close state) and kick the connect-time captures
    // (P58 prescan + P73 context), exactly like a console-driven open.
    const connect = entry.remote
      ? connectRemoteNode(entry.id)
      : openSerial({ port: lastSerial!.port, baudrate: lastSerial!.baudrate, mode: "serial" }, entry.id).then(() => undefined);
    connect
      .then(() => onClosed())
      .then(() => {
        if (entry.remote) {
          setCapturingRssi(true);
          return captureRemoteRssi(entry.id)
            .then((result) => { if (result.dut === entry.id) setRssi(result); })
            .finally(() => setCapturingRssi(false));
        }
        void runConnectCaptures(entry.id);
      })
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setConnecting(false));
  }, [entry.id, entry.remote, lastSerial, onClosed]);

  const onRefreshRssi = useCallback(() => {
    setCapturingRssi(true);
    setError(null);
    captureRemoteRssi(entry.id)
      .then((result) => { if (result.dut === entry.id) setRssi(result); })
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setCapturingRssi(false));
  }, [entry.id]);

  const cardStyle = entry.remote
    ? { borderTopColor: `hsl(${hashHue(entry.id)} 58% 46%)` }
    : { borderTopColor: "var(--ok, #22c55e)" };

  return (
    <div className="card fleet-card fleet-card--strip" style={cardStyle}>
      <button
        type="button"
        className="fleet-card-main"
        onClick={onOpen}
        title={`Select ${entry.label}`}
      >
        <div className="fleet-card-head">
          <div className="fleet-card-titles">
            <div className="card-title">{entry.label}</div>
            <div className="card-sub">{entry.id}</div>
            <div className="card-sub">
              {entry.remote ? `Remote via ${entry.remote.host}:${entry.remote.port}` : "Mother server"}
            </div>
          </div>
          <span className={`pill ${meta.pill}`}>
            <span className="dot" />
            {meta.label}
          </span>
        </div>
        <div className="fleet-card-cpu">
          <div className="kpi-value">{cpu}</div>
          <div className="kpi-sub">{cpuSub}</div>
        </div>
        <FleetBandBadge reco={reco} />
        {entry.remote ? (
          <dl className="stat-list fleet-remote-facts">
            <div className="stat-row">
              {/* Nothing here probes the Pi: this is whether the backend is
                  holding an SSH console open, so it must not be worded as
                  reachability, and "not connected" is a resting state rather
                  than a fault. Whether bytes are actually arriving is the
                  console row below. */}
              <dt>SSH session</dt>
              <dd className={entry.serialOpen ? "fleet-fact-ok" : "fleet-fact-idle"}>
                {entry.serialOpen ? "Connected" : "Not connected"}
              </dd>
            </div>
            <div className="stat-row">
              <dt>Node console</dt>
              <dd>{meta.label}</dd>
            </div>
            {/* Two directions, two rows, because they answer different
                questions and come from different commands. One "Backhaul RSSI"
                number could not say which way it pointed. */}
            <div className="stat-row">
              <dt>Uplink to parent</dt>
              <dd className={rssi && !rssi.applicable ? "fleet-rssi-na" : undefined}>
                {rssi && !rssi.applicable
                  ? "Not applicable"
                  : !rssi || !rssi.uplink || rssi.uplink.rssi === null
                    ? "Not captured"
                    : `${rssi.uplink.rssi} dBm · ${rssi.uplink.rssi_band}`}
              </dd>
            </div>
            <div className="stat-row">
              <dt>Children on backhaul</dt>
              <dd className={rssi && !rssi.applicable ? "fleet-rssi-na" : undefined}>
                {rssi && !rssi.applicable
                  ? "Not applicable"
                  : !rssi || !rssi.downlink
                    ? "Not captured"
                    : rssi.downlink.peers.length === 0
                      ? "None"
                      : rssi.downlink.peers
                          .map((p) => `${p.rssi === null ? "—" : `${p.rssi} dBm`}`)
                          .join(" · ")}
              </dd>
            </div>
          </dl>
        ) : null}
        <dl className="stat-list">
          <div className="stat-row">
            <dt>Crash events</dt>
            <dd>{entry.crashCount}{entry.crashCount > 0 ? " · since open" : ""}</dd>
          </div>
          <div className="stat-row">
            <dt>Last event</dt>
            <dd>{formatEventAge(entry.lastEventAgeSec)}</dd>
          </div>
          <div className="stat-row">
            <dt>Last snapshot</dt>
            <dd>{entry.lastSnapshotTs ?? "—"}</dd>
          </div>
        </dl>
      </button>
      <div className="fleet-card-actions">
        <button
          type="button"
          className="btn"
          title={`Open ${entry.label} serial console`}
          onClick={onConsole}
        >
          Console
        </button>
        {entry.serialOpen ? (
          <button
            type="button"
            className="btn"
            disabled={closing}
            title={`Close the serial session on ${entry.label}`}
            onClick={onCloseSerial}
          >
            {closing ? "Closing…" : "Close serial"}
          </button>
        ) : lastSerial || entry.remote ? (
          <button
            type="button"
            className="btn"
            disabled={connecting}
            title={entry.remote
              ? `Connect ${entry.label} through ${entry.remote.host}`
              : `Connect ${entry.label} on ${lastSerial!.port} @ ${lastSerial!.baudrate}`}
            onClick={onConnect}
          >
            {connecting ? "Connecting…" : "Connect"}
          </button>
        ) : (
          <button
            type="button"
            className="btn"
            disabled
            title="No remembered serial parameters — open this DUT once from the Serial Console."
          >
            Connect
          </button>
        )}
        {entry.remote ? (
          <button
            type="button"
            className="btn"
            disabled={!entry.serialOpen || capturingRssi || !entry.remote.isMesh}
            onClick={onRefreshRssi}
            title={entry.remote.isMesh ? "Capture backhaul RSSI now" : "Standalone AP has no mesh backhaul RSSI"}
          >
            {capturingRssi ? "Reading…" : "Refresh RSSI"}
          </button>
        ) : null}
        {error ? <span className="flash" style={{ color: "var(--danger)" }}>{error}</span> : null}
      </div>
    </div>
  );
}
