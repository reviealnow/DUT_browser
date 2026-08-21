import { useCallback, useState } from "react";

import {
  closeSerial,
  connectRemoteNode,
  disconnectRemoteNode,
  humanizeApiError,
  LastChannelRecommendationResult,
  openSerial,
  RemoteRssiResult,
} from "../api/rest";
import { ROLE_RANK, useAuth } from "../monitoring/AuthContext";
import { runConnectCaptures } from "../monitoring/siteSurveyStore";
import { DutStatus } from "../monitoring/useDutMonitor";
import { FleetEntry } from "../monitoring/useFleetMonitor";
import { RemoteRssiState } from "../monitoring/RemoteRssiContext";
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
 * One DUT's card, in both places the fleet is shown.
 *
 * `variant` changes the frame and how much of a capture is unfolded, never what
 * the card claims: the strip on Overview is a glance-and-switch row, and the
 * Fleet section has the width to show the measurement whole. Two components
 * would have been two accounts of the same DUT, and the next correction would
 * have landed in one of them.
 */
export default function FleetCard({
  entry,
  reco,
  rssiState,
  variant,
  onOpen,
  onConsole,
  onClosed,
}: {
  entry: FleetEntry;
  reco: LastChannelRecommendationResult | undefined;
  rssiState: RemoteRssiState;
  variant: "strip" | "grid";
  onOpen: () => void;
  onConsole: () => void;
  onClosed: () => Promise<void>;
}) {
  const { role } = useAuth();
  // Gate each control on the role its own route needs, not on one blanket
  // check: the serial router is engineer, every /api/fleet route is admin, and
  // the Serial Console section itself is engineer. A button that can only
  // answer 403 is worse than no button (the same call DutSwitcher makes).
  // This was already true of the strip before the Fleet section existed —
  // guests were being shown Connect, Close serial and Refresh RSSI.
  const canDrive = ROLE_RANK[role] >= ROLE_RANK[entry.remote ? "admin" : "engineer"];
  const canOpenConsole = ROLE_RANK[role] >= ROLE_RANK["engineer"];
  const [closing, setClosing] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rssi = rssiState.get(entry);
  const capturingRssi = rssiState.capturing(entry.id);
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
          return rssiState.refresh(entry);
        }
        void runConnectCaptures(entry.id);
      })
      .catch((e) => setError(humanizeApiError(e)))
      .finally(() => setConnecting(false));
  }, [entry.id, entry.remote, lastSerial, onClosed, rssiState]);

  const onRefreshRssi = useCallback(() => {
    setError(null);
    rssiState.refresh(entry).catch((e) => setError(humanizeApiError(e)));
  }, [entry, rssiState]);

  // Where a DUT's console is attached — this machine, or a remote Pi over SSH —
  // is the one distinction this strip exists to make, so the two get the
  // palette's two furthest-apart colours rather than a hue per id. A hashed hue
  // put "mesh1" and "ap2" on neighbouring purples; telling them apart at a
  // glance is the whole job, and it was not doing it.
  //
  // Note this is not a DUT's mesh role. Console location and mesh role are
  // independent: a mesh root can hang off a Pi and a mesh node can be cabled to
  // this machine. Whether a DUT has an uplink is measured, not inferred from
  // which card it is.
  const cardStyle = entry.remote
    ? { borderTopColor: "var(--ok, #0c7a43)" }
    : { borderTopColor: "var(--accent, #1565c0)" };

  return (
    <div className={`card fleet-card fleet-card--${variant}`} style={cardStyle}>
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
              {/* A root has no parent, and a capture that parsed its VAPs
                  established that. Saying "Not captured" there would report a
                  known state as a missing one. */}
              <dd className={rssi && (!rssi.applicable || rssi.role === "root") ? "fleet-rssi-na" : undefined}>
                {rssi && !rssi.applicable
                  ? "Not applicable"
                  : rssi && rssi.role === "root"
                    ? "None — this is the root"
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
                    : `${
                        rssi.downlink.peers.length === 0
                          ? "None"
                          : rssi.downlink.peers
                              .map((p) => (p.rssi === null ? "—" : `${p.rssi} dBm`))
                              .join(" · ")
                      }${
                        // An interface nobody verified is a backhaul gets said
                        // out loud, with the SSID it serves: on the bench a
                        // configured VAP was carrying an ordinary laptop.
                        rssi.downlink.source === "configured"
                          ? ` · ${rssi.downlink.iface} configured${
                              rssi.downlink.essid ? ` (${rssi.downlink.essid})` : ""
                            }`
                          : ""
                      }`}
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
      {variant === "grid" && entry.remote ? <FleetLinkDetail rssi={rssi} /> : null}
      <div className="fleet-card-actions">
        {canOpenConsole ? (
          <button
            type="button"
            className="btn"
            title={`Open ${entry.label} serial console`}
            onClick={onConsole}
          >
            Console
          </button>
        ) : null}
        {!canDrive ? null : entry.serialOpen ? (
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
        {entry.remote && canDrive ? (
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

/**
 * The capture, unfolded — every field the backend measured.
 *
 * The strip compresses a capture into two lines: the uplink to one number, and
 * every child's RSSI into one joined string. Three fields the DUT reports and
 * `/api/fleet/nodes/{id}/rssi` returns had no reader anywhere in the frontend —
 * the uplink's `snr`, `peer_mac` and `radio_band` — and per-child RSSI, the
 * measurement that says *which* child is hearing badly, was legible only for
 * one child. Both are why this section exists.
 */
function FleetLinkDetail({ rssi }: { rssi: RemoteRssiResult | null }) {
  if (!rssi || !rssi.applicable) {
    return null;
  }
  const uplink = rssi.uplink;
  const downlink = rssi.downlink;
  if (!uplink && !downlink) {
    return (
      <div className="fleet-detail fleet-detail-empty">
        No capture yet — press Refresh RSSI with the console open.
      </div>
    );
  }
  return (
    <div className="fleet-detail">
      <div className="fleet-detail-block">
        <div className="setting-label">Uplink to parent</div>
        {rssi.role === "root" ? (
          <div className="setting-hint">None — this is the root of the mesh.</div>
        ) : !uplink ? (
          <div className="setting-hint">Not captured.</div>
        ) : (
          <dl className="stat-list">
            <div className="stat-row">
              <dt>Interface</dt>
              <dd>{uplink.iface}</dd>
            </div>
            <div className="stat-row">
              <dt>Signal</dt>
              <dd>
                {uplink.rssi === null ? "—" : `${uplink.rssi} dBm`}
                {uplink.rssi_band ? ` · ${uplink.rssi_band}` : ""}
              </dd>
            </div>
            {/* Measured by the same command as the RSSI and carried in the same
                response; until now nothing rendered it. */}
            <div className="stat-row">
              <dt>SNR</dt>
              <dd>{uplink.snr === null ? "—" : `${uplink.snr} dB`}</dd>
            </div>
            <div className="stat-row">
              <dt>Band</dt>
              <dd>{uplink.radio_band ?? "—"}</dd>
            </div>
            <div className="stat-row">
              <dt>Backhaul SSID</dt>
              <dd>{uplink.essid ?? "—"}</dd>
            </div>
            <div className="stat-row">
              {/* The parent's BSSID. It is what identifies a root's backhaul
                  VAP, which the root cannot name from its own console — so it
                  is the field that ties two cards together. */}
              <dt>Parent BSSID</dt>
              <dd className="fleet-detail-mac">{uplink.peer_mac ?? "—"}</dd>
            </div>
          </dl>
        )}
      </div>

      <div className="fleet-detail-block">
        <div className="setting-label">Children on backhaul</div>
        {!downlink ? (
          <div className="setting-hint">Not captured.</div>
        ) : (
          <>
            <div className="setting-hint">
              {downlink.iface}
              {downlink.essid ? ` · ${downlink.essid}` : ""} ·{" "}
              {downlink.source === "detected"
                ? "detected backhaul"
                : "configured — nobody verified this interface is a backhaul"}
            </div>
            {downlink.peers.length === 0 ? (
              <div className="setting-hint">No children associated.</div>
            ) : (
              <table className="filetable fleet-peer-table">
                <thead>
                  <tr>
                    <th>CHILD BSSID</th>
                    <th>SIGNAL</th>
                    <th>QUALITY</th>
                  </tr>
                </thead>
                <tbody>
                  {downlink.peers.map((peer) => (
                    <tr key={peer.mac}>
                      <td className="fleet-detail-mac">{peer.mac}</td>
                      <td>{peer.rssi === null ? "—" : `${peer.rssi} dBm`}</td>
                      <td>{peer.rssi_band ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </div>
  );
}
